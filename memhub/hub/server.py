# -*- coding: utf-8 -*-
"""Loopback-first MemHub Core: bearer HTTP, durable evidence, one writer per store.

Recall always reads this device's store. With a sync target in ``hub.json`` the
Core also replicates the store's change log (``memhub.sync``); other devices'
Cores can sync against this one through ``/v1/hub/log/*``."""

from __future__ import annotations

import asyncio
import fcntl
import os
import secrets
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from memhub import __version__
from memhub.client import MemoryClient
from memhub.coding.types import PROCEDURE_KIND
from memhub.config import Settings
from memhub.errors import IdempotencyConflictError
from memhub.hub.browse import (
    browse_rows,
    canonical_workspace,
    git_remote_slug,
    workspace_sessions,
)
from memhub.hub.config import (
    SyncConfig,
    format_time,
    hub_root,
    hub_settings,
    load_or_create_runtime_token,
    load_sync_config,
    publish_runtime,
    unpublish_runtime,
)
from memhub.hub.queue import HubWriteQueue, evidence_fingerprint
from memhub.hub.redact import sanitize_messages, sanitize_text
from memhub.hub.sync import HubSyncer, SyncBackend, project_received
from memhub.sdk_models import SearchMemories
from memhub.types import Layer, MemoryStatus
from memhub.isolation import build_filter

HUB_USER_ID = "default"


class HubWriteRequest(BaseModel):
    content: str = ""
    messages: list[dict] | None = None
    session_id: str = ""
    memory_at: int | None = None
    request_id: str
    raw_only: bool = False
    # Version boundary of procedures from a tool trajectory.
    workspace_id: str | None = None
    branch: str | None = None
    # ``False`` returns as soon as the evidence is durable; the job runs later.
    wait: bool = False


# A coding question rarely shares wording with the fact that answers it, so the
# fused scores of real matches sit well below the SDK default.
HUB_RECALL_MIN_SCORE = 0.2
# Profile items (who the user is, how they want the work done) are recalled as
# their own group and for every project.
HUB_RECALL_PROFILE_LIMIT = 3


class HubRecallRequest(BaseModel):
    query: str
    limit: int = 10
    raw_limit: int = 5
    # MemHub serves coding CLIs: engineering experience is recalled by default.
    procedure_limit: int = 3
    # The project the prompt comes from; evidence is limited to its sessions.
    workspace: str | None = None


class HubLogExport(BaseModel):
    after: dict[str, int] = {}
    limit: int = 200


class HubLogImport(BaseModel):
    entries: list[dict]


class HubMemoryEdit(BaseModel):
    content: str


UI_PAGE = Path(__file__).with_name("ui.html")


class HubProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a")
        try:
            fcntl.flock(
                self._handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                f"another MemHub Core already owns {self._path.parent}"
            ) from exc

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _instance_id(root: Path) -> str:
    path = root / "instance-id"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    value = str(uuid.uuid4())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return value


DIGEST_AT_NAME = "digest-at"


def read_last_digest_at(root: Path) -> float | None:
    """Unix time of the last successful automatic digest, or None if none yet."""
    path = root / DIGEST_AT_NAME
    if not path.is_file():
        return None
    return float(path.read_text(encoding="utf-8").strip())


def write_last_digest_at(root: Path, when: float) -> None:
    (root / DIGEST_AT_NAME).write_text(f"{when:.3f}\n", encoding="utf-8")


def last_digest_due(root: Path, *, min_hours: float, now: float | None = None) -> bool:
    """True when no successful auto-digest is on record, or the interval has elapsed.

    ``min_hours <= 0`` turns automatic digest off. A missing stamp is due: the
    first drain after evidence exists consolidates once, then the clock starts.
    """
    if min_hours <= 0:
        return False
    last = read_last_digest_at(root)
    if last is None:
        return True
    current = time.time() if now is None else now
    return current - last >= min_hours * 3600.0


def project_sessions(client: MemoryClient, workspace: str) -> list[str]:
    """Session ids of the dialogues written from ``workspace``, read from the store.

    The store replicates, so dialogues written from the same project on another
    device scope recall here too. Procedures carry the workspace as well, which
    covers dialogues stored before the evidence kept it.
    """
    vector = client.factory.vector
    scope = {
        "app_ids": [client.settings.default_app_id],
        "user_id": HUB_USER_ID,
        "agent_ids": [""],
        "statuses": [MemoryStatus.ACTIVE],
    }
    nodes = vector.get_many(
        build_filter(**scope, layers=[Layer.L0_RAW]), None
    ) + vector.get_many(build_filter(**scope, kinds=[PROCEDURE_KIND]), None)
    return workspace_sessions(nodes, workspace)


def create_hub_app(
    *,
    api_key: str | None = None,
    hub_dir: str | Path | None = None,
    client: MemoryClient | None = None,
    settings: Settings | None = None,
    published_base_url: str = "http://127.0.0.1:8765",
    queue_retry_seconds: float = 30.0,
    digest_min_hours: float = 24.0,
    sync: SyncConfig | None = None,
    sync_backend: SyncBackend | None = None,
    sync_interval_seconds: float = 300.0,
    sync_pull_seconds: float = 60.0,
) -> FastAPI:
    """Create the personal Hub Core; one process owns one local store.

    ``api_key`` (else the persisted runtime token, created on first start) is
    written to the token file inside the lifespan, after the exclusive lock, so a
    second ``memhub serve`` cannot replace the running Core's token on its way out.

    In ``dual`` mode, once the write queue drains, the Core calls ``digest()``
    when pending System 2 evidence exists and the last successful auto-digest
    was at least ``digest_min_hours`` ago. ``digest_min_hours <= 0`` turns that
    off. The stamp is ``digest-at`` in the Hub root; a failed run does not write it.

    ``sync`` (default: ``hub.json``) replicates this store. Only one store of a
    replica set digests: the remote Core for ``http`` / ``ssh``, the first device
    to sync for ``dir`` / ``git``. The others receive its schemas.
    """
    root = hub_root(hub_dir)
    sync_cfg = sync if sync is not None else load_sync_config(root)
    lock = HubProcessLock(root / "hub.lock")
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock.acquire()
        core_client: MemoryClient | None = None
        queue: HubWriteQueue | None = None
        syncer: HubSyncer | None = None
        app.state.syncer = None
        try:
            resolved_key = load_or_create_runtime_token(root, token=api_key)
            app.state.api_key = resolved_key
            publish_runtime(
                base_url=published_base_url,
                instance_id=app.state.instance_id,
                pid=os.getpid(),
                hub_dir=root,
            )
            core_client = client or MemoryClient(
                settings=settings or hub_settings(root)
            )
            app.state.client = core_client

            async def configure_sync(cfg: SyncConfig | None, backend=None) -> None:
                """(Re)start replication for ``cfg``; None stops it."""
                nonlocal syncer
                if syncer is not None:
                    await syncer.close()
                syncer = None
                if cfg is not None and cfg.kind:
                    syncer = HubSyncer(
                        core_client,
                        cfg,
                        hub_dir=root,
                        backend=backend,
                        interval_seconds=sync_interval_seconds,
                        pull_seconds=sync_pull_seconds,
                    )
                app.state.syncer = syncer

            await configure_sync(sync_cfg, sync_backend)
            app.state.configure_sync = configure_sync

            async def process(payload: dict[str, Any]) -> dict[str, Any]:
                kwargs = {
                    "content": payload["content"],
                    "messages": payload["messages"],
                    "user_id": HUB_USER_ID,
                    "agent_id": "",
                    "session_id": payload["session_id"],
                    "memory_at": payload["memory_at"],
                    "request_id": payload["request_id"],
                }
                # Rows queued before boundaries existed carry none.
                workspace_id = payload.get("workspace_id")
                workspace_remote = payload.get("workspace_remote")
                if payload["raw_only"]:
                    result = await core_client.add_raw(
                        **kwargs,
                        workspace_id=workspace_id,
                        workspace_remote=workspace_remote,
                    )
                else:
                    result = await core_client.add(
                        **kwargs,
                        workspace_id=workspace_id,
                        branch=payload.get("branch"),
                        workspace_remote=workspace_remote,
                    )
                if syncer is not None:
                    syncer.kick()
                return result.to_dict()

            async def digest() -> None:
                # The queue just drained. The day interval and the pending list
                # decide whether this spends a model call.
                if not last_digest_due(root, min_hours=digest_min_hours):
                    return
                if syncer is not None and not syncer.digests_here():
                    return
                if not core_client.factory.cache.list_pending_s2_scopes():
                    return
                result = await core_client.digest()
                if result.processed > 0:
                    write_last_digest_at(root, time.time())
                    if syncer is not None:
                        syncer.kick()

            auto_digest = core_client.settings.mode == "dual" and digest_min_hours > 0
            queue = HubWriteQueue(
                root / "hub.db",
                process,
                retry_base_seconds=queue_retry_seconds,
                on_idle=digest if auto_digest else None,
                idle_seconds=0,
            )
            app.state.queue = queue
            queue.start()
            if syncer is not None:
                syncer.start()
            yield
        finally:
            if syncer is not None:
                await syncer.close()
            if queue is not None:
                await queue.close()
            if owns_client and core_client is not None:
                await core_client.aclose()
            unpublish_runtime(app.state.instance_id, os.getpid(), root)
            lock.release()

    app = FastAPI(
        title="memhub MemHub Core",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.instance_id = _instance_id(root)
    app.state.api_key = api_key or ""

    def verify(
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = app.state.api_key
        if not expected:
            raise HTTPException(status_code=503, detail="Hub Core is starting")
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="missing Hub bearer token")
        supplied = authorization[len(prefix) :].strip()
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="invalid Hub bearer token")

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(_request, exc: IdempotencyConflictError):
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": str(exc)},
        )

    @app.get("/health")
    async def health():
        return {"status": "SERVING"}

    @app.get("/v1/hub/identity", dependencies=[Depends(verify)])
    async def identity():
        resolved = settings or (client.settings if client else hub_settings(root))
        return {
            "instance_id": app.state.instance_id,
            "sdk_version": __version__,
            "scope": {
                "app_id": resolved.default_app_id,
                "user_id": HUB_USER_ID,
                "agent_id": "",
            },
        }

    @app.post("/v1/hub/writes", dependencies=[Depends(verify)])
    async def submit_write(body: HubWriteRequest):
        if not body.content.strip() and not body.messages:
            raise HTTPException(
                status_code=400,
                detail="content and messages cannot both be empty",
            )
        payload = body.model_dump(exclude={"wait"})
        # Identity of the evidence as the client sent it, so a resend matches the
        # stored row whatever this Core's sanitizing settings are now.
        fingerprint = evidence_fingerprint(payload)
        redact = app.state.client.settings.redact_secrets

        def prepare() -> None:
            # Regex sanitizing of a large batch and the workspace's walk up to
            # ``.git`` are blocking work; keep them off the Core's event loop.
            payload["content"] = sanitize_text(payload["content"], redact=redact)
            payload["messages"] = sanitize_messages(payload["messages"], redact=redact)
            if payload.get("workspace_id"):
                payload["workspace_remote"] = git_remote_slug(payload["workspace_id"])
                payload["workspace_id"] = (
                    canonical_workspace(payload["workspace_id"]) or None
                )

        await asyncio.to_thread(prepare)
        if not payload["content"].strip() and not payload["messages"]:
            return {
                "request_id": body.request_id,
                "status": "done",
                "raw_only": body.raw_only,
                "attempts": 0,
                "result": {"skipped": "private"},
                "error": None,
            }
        job = app.state.queue.submit(payload, fingerprint=fingerprint)
        if body.wait:
            job = await app.state.queue.wait(body.request_id)
        return job

    @app.get("/v1/hub/writes/{request_id}", dependencies=[Depends(verify)])
    async def write_status(request_id: str):
        try:
            return app.state.queue.get(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Hub write not found") from exc

    @app.post("/v1/hub/extract", dependencies=[Depends(verify)])
    async def extract_raw():
        """Queue extraction for evidence stored with ``raw_only``."""
        return {"queued": app.state.queue.extract_raw()}

    @app.post("/v1/hub/retry", dependencies=[Depends(verify)])
    async def retry_failed():
        return {"queued": app.state.queue.retry_failed()}

    def health_warnings() -> list[str]:
        warnings: list[str] = []
        failed, last_error = app.state.queue.failure_summary()
        if failed > 0:
            err = last_error or "unknown error"
            warnings.append(
                f"⚠️ MemHub 后台有 {failed} 条记忆写入失败（最近原因：{err[:160]}）。"
                "请在回复开头提醒用户运行 `memhub doctor` 排查或运行 `memhub retry` 重试。"
            )
        if app.state.syncer is not None:
            sync_err = app.state.syncer.status().get("last_error")
            if sync_err:
                warnings.append(
                    f"⚠️ MemHub 跨设备同步异常（{str(sync_err)[:160]}）。"
                    "请提醒用户运行 `memhub doctor` 检查同步状态。"
                )
        return warnings

    @app.get("/v1/hub/conversations/{source_id}", dependencies=[Depends(verify)])
    async def get_conversation(source_id: str, context_turns: int = 1):
        """Progressive disclosure: fetch an L0_RAW dialogue by full ID or short prefix
        (such as the 8-char ``(来源: L0 37c85fe9)`` tag), plus surrounding turns in
        the same session."""
        needle = source_id.strip()
        if not needle:
            raise HTTPException(status_code=400, detail="source_id cannot be empty")
        core_client: MemoryClient = app.state.client
        vector = core_client.factory.vector

        # A full id is one record read. A short prefix is matched against the L0
        # ids alone; only the matching records are then read.
        anchor = await asyncio.to_thread(vector.get, needle)
        if anchor is not None and (
            anchor.layer != Layer.L0_RAW
            or anchor.app_id != core_client.settings.default_app_id
            or anchor.user_id != HUB_USER_ID
        ):
            anchor = None
        if anchor is None:
            ids = await asyncio.to_thread(
                vector.get_ids,
                build_filter(
                    app_ids=[core_client.settings.default_app_id],
                    user_id=HUB_USER_ID,
                    agent_ids=[""],
                    statuses=[MemoryStatus.ACTIVE],
                    layers=[Layer.L0_RAW],
                ),
            )
            hits = [i for i in ids if len(needle) >= 6 and i.startswith(needle)]
            matches = list(
                (await asyncio.to_thread(vector.get_by_ids, hits)).values()
            )
            if not matches:
                raise HTTPException(
                    status_code=404, detail=f"L0 conversation {needle!r} not found"
                )
            anchor = max(
                matches,
                key=lambda n: (n.memory_at or n.gmt_created or 0, n.node_id),
            )

        span = max(0, min(context_turns, 20))
        if span > 0 and anchor.session_id:
            peers = await asyncio.to_thread(
                vector.get_many,
                build_filter(
                    app_ids=[core_client.settings.default_app_id],
                    user_id=HUB_USER_ID,
                    agent_ids=[""],
                    statuses=[MemoryStatus.ACTIVE],
                    layers=[Layer.L0_RAW],
                    session_ids=[anchor.session_id],
                ),
                None,
            )
            selected = sorted(
                peers,
                key=lambda n: (n.memory_at or n.gmt_created or 0, n.node_id),
            )
            idx = next(
                (i for i, n in enumerate(selected) if n.node_id == anchor.node_id),
                0,
            )
            selected = selected[max(0, idx - span) : idx + span + 1]
        else:
            selected = [anchor]

        def rows() -> list[dict[str, Any]]:
            # canonical_workspace walks the filesystem; one walk per workspace.
            canon: dict[str, str] = {}
            out = []
            for n in selected:
                raw_ws = str((n.custom or {}).get("workspace_id") or "")
                if raw_ws not in canon:
                    canon[raw_ws] = canonical_workspace(raw_ws)
                out.append(
                    {
                        "memory_id": n.node_id,
                        "source_id": n.node_id[:8],
                        "session_id": n.session_id,
                        "content": n.content,
                        "memory_at": n.memory_at or n.gmt_created,
                        "workspace_id": canon[raw_ws],
                        "is_anchor": n.node_id == anchor.node_id,
                    }
                )
            return out

        return {
            "source_id": anchor.node_id[:8],
            "memory_id": anchor.node_id,
            "session_id": anchor.session_id,
            "conversations": await asyncio.to_thread(rows),
        }

    @app.post("/v1/hub/recall", dependencies=[Depends(verify)])
    async def recall(body: HubRecallRequest):
        core_client: MemoryClient = app.state.client
        if app.state.syncer is not None:
            # Answer from the local store now; newer memories arrive for the next one.
            app.state.syncer.pull_if_stale()
        memories = SearchMemories()
        conversations = SearchMemories()
        sessions = (
            await asyncio.to_thread(project_sessions, core_client, body.workspace)
            if body.workspace
            else []
        )
        scope = sessions or None
        if body.limit > 0 or body.procedure_limit > 0:
            memories = (
                await core_client.search(
                    query=body.query,
                    user_id=HUB_USER_ID,
                    agent_ids=[""],
                    limit=body.limit,
                    procedure_limit=body.procedure_limit,
                    profile_limit=0 if scope else HUB_RECALL_PROFILE_LIMIT,
                    min_score=HUB_RECALL_MIN_SCORE,
                    profile_min_score=HUB_RECALL_MIN_SCORE,
                    session_ids=scope,
                )
            ).memories
            if scope:
                # The profile holds for every project. Another project's fact comes
                # in only when it matches better than anything of this project: the
                # prompt is then about that project. The query vector is cached by
                # the embed service, so this second pass costs no embedding call.
                wider = (
                    await core_client.search(
                        query=body.query,
                        user_id=HUB_USER_ID,
                        agent_ids=[""],
                        limit=body.limit,
                        procedure_limit=0,
                        profile_limit=HUB_RECALL_PROFILE_LIMIT,
                        min_score=HUB_RECALL_MIN_SCORE,
                        profile_min_score=HUB_RECALL_MIN_SCORE,
                    )
                ).memories
                memories.profile = wider.profile
                best = max((item.score for item in memories.normal), default=0.0)
                seen = {item.memory_id for item in memories.normal}
                memories.normal = sorted(
                    memories.normal
                    + [
                        item
                        for item in wider.normal
                        if item.memory_id not in seen and item.score > best
                    ],
                    key=lambda item: item.score,
                    reverse=True,
                )[: body.limit]
        if body.raw_limit > 0:
            conversations = (
                await core_client.search_conversation(
                    query=body.query,
                    user_id=HUB_USER_ID,
                    agent_ids=[""],
                    limit=body.raw_limit,
                    session_ids=scope,
                )
            ).memories
        out: dict[str, Any] = {
            "memories": memories.to_dict(),
            "conversations": [item.to_dict() for item in conversations.normal],
        }
        warnings = health_warnings()
        if warnings:
            out["warnings"] = warnings
        return out

    def embed_identity() -> dict[str, Any]:
        resolved = app.state.client.settings
        return {"model": resolved.embed_model, "dim": resolved.embed_dim}

    @app.get("/ui", response_class=HTMLResponse)
    async def ui():
        """The memory browser; it calls the API below with the token from its URL."""
        return UI_PAGE.read_text(encoding="utf-8")

    @app.get("/v1/hub/memories", dependencies=[Depends(verify)])
    async def list_memories():
        """Every version in the Hub scope, newest first, with project and host."""
        core_client: MemoryClient = app.state.client
        nodes = await asyncio.to_thread(
            core_client.factory.vector.get_many,
            build_filter(
                app_ids=[core_client.settings.default_app_id],
                user_id=HUB_USER_ID,
                agent_ids=[""],
                statuses=[MemoryStatus.ACTIVE],
            ),
            None,
        )
        return {"memories": browse_rows(nodes)}

    def changed(result) -> dict[str, Any]:
        if not result.success:
            detail = {404: "memory not found", 409: "memory is not the current version"}
            raise HTTPException(
                status_code=result.error_code, detail=detail[result.error_code]
            )
        if app.state.syncer is not None:
            app.state.syncer.kick()
        return result.to_dict()

    @app.put("/v1/hub/memories/{memory_id}", dependencies=[Depends(verify)])
    async def edit_memory(memory_id: str, body: HubMemoryEdit):
        """A user correction: a new current version; the old one stays as history."""
        content = sanitize_text(
            body.content, redact=app.state.client.settings.redact_secrets
        )
        if not content.strip():
            raise HTTPException(
                status_code=400, detail="content is empty after removing <private>"
            )
        return changed(await app.state.client.update(memory_id, content))

    @app.delete("/v1/hub/memories/{memory_id}", dependencies=[Depends(verify)])
    async def delete_memory(memory_id: str):
        """Retire the current version; it stays in the history."""
        return changed(await app.state.client.delete(memory_id))

    @app.get("/v1/hub/log/heads", dependencies=[Depends(verify)])
    async def log_heads():
        vector = app.state.client.factory.vector
        return {
            "origin": vector.origin,
            "embed": embed_identity(),
            "heads": await asyncio.to_thread(vector.log_heads),
        }

    @app.post("/v1/hub/log/export", dependencies=[Depends(verify)])
    async def log_export(body: HubLogExport):
        vector = app.state.client.factory.vector
        entries = await asyncio.to_thread(
            vector.export_log, body.after, limit=min(body.limit, 1000)
        )
        return {"entries": entries}

    @app.post("/v1/hub/log/import", dependencies=[Depends(verify)])
    async def log_import(body: HubLogImport):
        """Another device's changes; this Core relays them and digests for it."""
        core_client: MemoryClient = app.state.client
        result = await asyncio.to_thread(core_client.factory.vector.import_log, body.entries)
        await project_received(core_client)
        syncer = app.state.syncer
        if core_client.settings.mode == "dual" and (syncer is None or syncer.digests_here()):
            for app_id, user_id, agent_id in result["scopes"]:
                core_client.factory.cache.enqueue_s2_task(
                    user_id=user_id, app_id=app_id, agent_id=agent_id
                )
        if result["applied"]:
            app.state.queue.wake()
        return {k: v for k, v in result.items() if k != "scopes"}

    @app.post("/v1/hub/sync/reload", dependencies=[Depends(verify)])
    async def sync_reload():
        """Pick up ``hub.json`` after ``memhub connect`` / ``memhub disconnect``."""
        await app.state.configure_sync(load_sync_config(root))
        if app.state.syncer is not None:
            app.state.syncer.start()
        return {"sync": app.state.syncer.status() if app.state.syncer else None}

    @app.post("/v1/hub/sync", dependencies=[Depends(verify)])
    async def sync_now(compact: bool = False):
        """One sync cycle; ``compact=true`` also folds the log into a snapshot."""
        if app.state.syncer is None:
            raise HTTPException(
                status_code=400, detail="no sync target; run `memhub connect`"
            )
        try:
            return await app.state.syncer.run_once(compact=True if compact else None)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/hub/status", dependencies=[Depends(verify)])
    async def status():
        core_client: MemoryClient = app.state.client
        nodes = core_client.factory.vector.get_many(
            build_filter(
                app_ids=[core_client.settings.default_app_id],
                user_id=HUB_USER_ID,
                agent_ids=[""],
                statuses=[MemoryStatus.ACTIVE],
            ),
            limit=None,
        )
        latest = [node for node in nodes if node.is_latest]
        return {
            "instance_id": app.state.instance_id,
            "root": str(root),
            "memories": {
                "versions": len(nodes),
                "current": len(latest),
                "by_layer": dict(Counter(node.layer.value for node in latest)),
                "raw_by_source": dict(
                    Counter(
                        node.session_id.partition(":")[0]
                        for node in latest
                        if node.layer == Layer.L0_RAW
                    )
                ),
            },
            "writes": {
                **app.state.queue.counts(),
                **(
                    {"last_error": err}
                    if (err := app.state.queue.last_failed_error())
                    else {}
                ),
            },
            "warnings": health_warnings(),
            "digest": {
                "auto": app.state.client.settings.mode == "dual" and digest_min_hours > 0,
                "min_hours": digest_min_hours,
                "last_at": format_time(read_last_digest_at(root)),
                "last_error": app.state.queue.last_idle_error,
            },
            "sync": app.state.syncer.status() if app.state.syncer is not None else None,
        }

    return app

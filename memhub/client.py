# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Public async facade of the memhub layered-memory SDK. MemoryClient wires
configured providers/stores together and exposes add/search/get/list/update/delete/digest,
returning strongly typed dataclass models (sdk_models). Multi-turn messages are formatted
into natural dialogue text for L0_RAW + extract; embed sees the user-only concatenation.
A per-user asyncio.Lock serializes concurrent add() for the same (app_id, user_id) so
version appends cannot race on the same evolution chain.

memhub requires a JEV key, an LLM API key and an embedding API key. Missing credentials
raise on construction. Tests and custom backends inject ``jev=`` / ``llm=`` / ``embed=``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import asdict
from typing import Any

from memhub.coding.preproc import (
    coding_dialogue,
    has_any_tool_message,
)
from memhub.coding.types import PROCEDURE_KIND, revised_procedure
from memhub.coding.writer import ProcedureWriter
from memhub.config import Settings
from memhub.isolation import build_filter
from memhub.locks import LockRegistry
from memhub.registry import _UNSET, ComponentFactory
from memhub.retrieval.reader import Reader
from memhub.sdk_models import (
    MemoryDecision,
    ChatMessage,
    DeleteBulkResult,
    DeleteResult,
    DigestResult,
    MemoryItem,
    ScopeSummary,
    SearchResult,
    UpdateResult,
    WriteResult,
    write_status,
)
from memhub.system2.system2_writer import System2Writer
from memhub.types import MemoryStatus
from memhub.writer.memory_writer import MemoryWriter

logger = logging.getLogger("memhub.client")

_INJECTION_BLOCK_RE = re.compile(
    r"<(relevant-memories|user-profile|memory-tools-guide|topic-catalog|private)"
    r"\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)


def strip_memory_injection(text: str) -> str:
    """Remove host-injected memory blocks so they are not written back as L1."""
    if not text:
        return text
    cleaned = _INJECTION_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


class MissingCredentialsError(RuntimeError):
    """Raised when MemoryClient is constructed without the required Jev / LLM / embedding providers."""


class MemoryClient:
    """High-level async entry point for memhub.

    Picks ``system1`` (MemoryWriter) or ``dual`` (System2Writer) from ``Settings.mode``.

    Constructor kwargs (see also ``Settings`` / ``~/.memhub/config.yaml``):

    - ``mode``: ``"system1"`` (default, L0/L1/L2 versions) or ``"dual"`` (+ explicit digest L3).
    - ``storage_dir``: on-disk root for Chroma / Kuzu / SQLite; default ``./.memhub_data``.
    - ``settings``: explicit ``Settings`` instance; overrides YAML and env.
    - ``embed`` / ``llm``: inject custom clients; skips the corresponding api_key check.

    Tenant scope on ``add`` / ``search``:

    - ``app_id`` (optional): defaults to ``settings.default_app_id`` (``"default"``).
    - ``user_id`` (required): end-user id; reads and writes must share the same scope.
    - ``app_ids`` (optional on search): defaults to ``[default_app_id]``.
    - ``agent_id`` / ``session_id`` (optional): finer isolation within one user.

    Lifecycle: reuse one client per process (FastAPI lifespan / agent runtime) and call
    ``await aclose()`` once at shutdown. Do **not** close it after every add/search.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        storage_dir: str | None = None,
        mode: str | None = None,
        embed=None,
        llm=None,
        jev=None,
    ) -> None:
        overrides = {}
        if storage_dir is not None:
            overrides["storage_dir"] = storage_dir
        if mode is not None:
            overrides["mode"] = mode
        if settings is None:
            settings = Settings(**overrides)
        elif overrides:
            settings = Settings.model_validate({**settings.model_dump(), **overrides})
        self.settings = settings
        self.mode = mode or settings.mode

        self.factory = ComponentFactory(
            settings=settings,
            embed=embed,
            llm=llm if llm is not None else _UNSET,
            jev=jev,
        )

        self._validate_credentials(
            injected_embed=embed is not None, injected_llm=llm is not None
        )
        if jev is None and not settings.jev_api_key:
            raise MissingCredentialsError(
                "Jev requires MEMHUB_JEV_API_KEY or jev= provider"
            )
        self._build_writer()
        self.reader = Reader(factory=self.factory)
        self._write_locks = self.factory.commit_locks
        self.procedures = (
            ProcedureWriter(factory=self.factory)
            if self.settings.procedure_enabled
            else None
        )

    @classmethod
    def from_config(
        cls,
        config_dict: dict[str, Any],
        *,
        mode: str | None = None,
        embed=None,
        llm=None,
        jev=None,
    ) -> MemoryClient:
        """Create a client from a canonical flat config dict (see ``Settings.from_dict``).

        Usage::

            client = MemoryClient.from_config({
                "mode": "dual",
                "default_app_id": "my_app",
                "llm": {"model": "gpt-4o-mini", "api_key": "sk-..."},
                "embedder": {"model": "text-embedding-3-small", "api_key": "sk-..."},
            })
        """
        settings = Settings.from_dict(config_dict)
        if mode is not None:
            settings = settings.model_copy(update={"mode": mode})
        return cls(settings=settings, embed=embed, llm=llm, jev=jev)

    @classmethod
    async def acreate(
        cls,
        *,
        settings: Settings | None = None,
        storage_dir: str | None = None,
        mode: str | None = None,
        embed=None,
        llm=None,
        jev=None,
    ) -> "MemoryClient":
        """Async factory mirror of ``MemoryClient(...)``; kept for API symmetry with prior versions."""
        return cls(
            settings=settings,
            storage_dir=storage_dir,
            mode=mode,
            embed=embed,
            llm=llm,
            jev=jev,
        )

    def _validate_credentials(
        self, *, injected_embed: bool, injected_llm: bool
    ) -> None:
        """Fail fast when LLM or embedding credentials are missing.

        Injected ``embed`` / ``llm`` clients (tests, custom backends) bypass the api_key check
        — the caller is responsible for those. Raises ``MissingCredentialsError`` otherwise.
        """
        missing: list[str] = []
        if not injected_llm and not self.settings.llm_api_key:
            missing.append(
                "llm_api_key (set MEMHUB_LLM_API_KEY or pass settings.llm_api_key)"
            )
        if not injected_embed and not self.settings.embed_api_key:
            missing.append(
                "embed_api_key (set MEMHUB_EMBED_API_KEY or pass settings.embed_api_key)"
            )
        if missing:
            raise MissingCredentialsError(
                "memhub requires both LLM and embedding API keys. Missing: "
                + "; ".join(missing)
                + ". For tests / custom backends, pass embed=... and llm=... to MemoryClient(...)."
            )

    def _build_writer(self) -> None:
        """Instantiate the writer matching the configured mode (system1 or dual)."""
        if self.settings.mode == "dual":
            self.writer = System2Writer(factory=self.factory)
        else:
            self.writer = MemoryWriter(factory=self.factory)

    def _resolve_app_id(self, app_id: str | None) -> str:
        """Return explicit ``app_id`` or ``settings.default_app_id``."""
        return app_id if app_id is not None else self.settings.default_app_id

    def _user_write_lock(self, app_id: str, user_id: str) -> asyncio.Lock:
        """Return the per-user write lock, creating it lazily on first access."""
        return self._write_locks.get(f"{app_id}::{user_id}")

    def _user_write_lock_ctx(self, app_id: str, user_id: str):
        """Per-user write lock, or a no-op context when ``write_serialize_per_user`` is off.

        Disabling the lock lets concurrent add() for the same user overlap (batch ingest with
        deferred reconcile); the vector store is internally thread-safe so this is safe.
        """
        if self.settings.write_serialize_per_user:
            return self._user_write_lock(app_id, user_id)
        return contextlib.nullcontext()

    async def add(
        self,
        *,
        content: str = "",
        messages: list[dict] | list[ChatMessage] | None = None,
        app_id: str | None = None,
        user_id: str,
        agent_id: str = "",
        session_id: str = "",
        memory_at: int | None = None,
        request_id: str | None = None,
        workspace_id: str | None = None,
        branch: str | None = None,
        workspace_remote: str | None = None,
    ) -> WriteResult:
        """Write one memory and run the cognition pipeline (Extract -> commit -> ...).

        Pass either ``content`` (single blob) or ``messages`` (multi-turn chat) — same API.

        ``app_id`` defaults to ``settings.default_app_id`` when omitted.
        ``user_id`` is required. With ``messages``, L0_RAW and the extractor see the shaped
        full dialogue after host-injected system turns are removed.

        Coding scene: when ``messages`` carry a tool trace (``role="tool"`` or assistant
        ``tool_calls``) and JEV judges the conversation to be engineering work, the
        same write also decides procedure memories (``kind="procedure"``). Chat
        memories of the conversation are extracted as usual. A coding session is
        remembered by its dialogue — the user's requests and the answers they
        read: that is what L0_RAW keeps and what both extractors see; tool calls
        only contribute the files they touched, tool output is not kept.
        ``workspace_id`` / ``branch`` are the
        version boundary of those procedures: one only replaces another inside the
        same workspace and branch. ``workspace_id`` is also kept on the L0_RAW
        evidence (``custom["workspace_id"]``), chat or coding, so recall can be
        scoped to a project on every device the store replicates to.

        Cost per add: one candidate generation; a candidate with no similar current
        head is appended without a Jev call, otherwise one target question and (when a
        target is chosen) one action question. A tool trace adds one scene question,
        one procedure generation and its decisions. ``WriteResult.status`` reports
        committed / known / raw_only. Batch ``messages`` at session end to reduce cost.
        """
        request_id = request_id or str(uuid.uuid4())
        start = time.perf_counter()
        resolved_app_id = self._resolve_app_id(app_id)

        trajectory: list[dict] | None = None
        if messages and self.procedures is not None:
            turns = [
                dict(m) if isinstance(m, dict) else {"role": m.role, "content": m.content}
                for m in messages
            ]
            turns = [m for m in turns if m.get("role") != "system"]
            if has_any_tool_message(turns):
                # Only text is scrubbed; structured parts stay intact.
                for turn in turns:
                    if isinstance(turn.get("content"), str):
                        turn["content"] = strip_memory_injection(turn["content"])
                trajectory = turns
                # A coding session is remembered by its dialogue: what the user
                # asked and the answers they read. That is what L0_RAW keeps and
                # what both extractors see; the tool trajectory only tells the
                # procedure writer which files were touched.
                messages = coding_dialogue(turns)

        normalized: list[ChatMessage] | None = None
        user_turn_count = 0
        if messages is not None:
            content, normalized, user_turn_count = _prepare_write_text(
                content, messages, settings=self.settings
            )
        else:
            content = strip_memory_injection(content)

        if not content.strip():
            return WriteResult(
                success=True,
                memory_id="",
                request_id=request_id,
                processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
                commit_passed=False,
                extracted_count=0,
                is_ephemeral=True,
                status="raw_only",
            )

        logger.info(
            "add app=%s user=%s mode=%s len=%d turns=%d tools=%s",
            resolved_app_id,
            user_id,
            self.mode,
            len(content),
            user_turn_count,
            trajectory is not None,
        )

        write_kwargs = {
            "content": content,
            "app_id": resolved_app_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "request_id": request_id,
            "memory_at": memory_at,
            "workspace_id": workspace_id,
            "workspace_remote": workspace_remote,
        }
        async with self._user_write_lock_ctx(resolved_app_id, user_id):
            result = await self.writer.write(**write_kwargs, messages=normalized)
            procedure = None
            if trajectory is not None:
                raw = self.factory.vector.get(result.memory_id)
                procedure = await self.procedures.write(
                    messages=trajectory,
                    raw=raw,
                    request_id=request_id,
                    workspace_id=workspace_id,
                    branch=branch,
                )
                if procedure.committed and isinstance(self.writer, System2Writer):
                    self.factory.cache.enqueue_s2_task(
                        user_id=user_id, app_id=resolved_app_id, agent_id=agent_id
                    )
        decisions = list(result.decisions)
        node_ids = list(result.extra_node_ids)
        if procedure is not None and procedure.coding:
            decisions.extend(procedure.decisions)
            node_ids.extend(procedure.node_ids)
        return WriteResult(
            success=True,
            memory_id=result.memory_id,
            request_id=request_id,
            processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
            commit_passed=result.commit_passed
            or (procedure is not None and procedure.committed),
            extracted_count=len(node_ids),
            extra_node_ids=node_ids,
            is_ephemeral=bool(decisions)
            and all(d.action == "RAW_ONLY" for d in decisions),
            decisions=decisions,
            status=write_status(decisions),
            scene="coding" if procedure is not None and procedure.coding else "chat",
            procedure_ids=list(procedure.node_ids) if procedure is not None else [],
        )

    async def add_raw(
        self,
        *,
        content: str = "",
        messages: list[dict] | list[ChatMessage] | None = None,
        app_id: str | None = None,
        user_id: str,
        agent_id: str = "",
        session_id: str = "",
        memory_at: int | None = None,
        request_id: str | None = None,
        workspace_id: str | None = None,
        workspace_remote: str | None = None,
    ) -> WriteResult:
        """Write L0_RAW only (embed + store). Does not run extract.

        ``workspace_id`` is kept on the evidence, as in ``add``.
        """
        request_id = request_id or str(uuid.uuid4())
        start = time.perf_counter()
        resolved_app_id = self._resolve_app_id(app_id)
        text, _normalized, _turns = _prepare_write_text(
            content,
            messages,
            settings=self.settings,
        )
        if not text.strip():
            return WriteResult(
                success=True,
                memory_id="",
                request_id=request_id,
                processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
                commit_passed=False,
                extracted_count=0,
                is_ephemeral=True,
                status="raw_only",
            )
        async with self._user_write_lock_ctx(resolved_app_id, user_id):
            result = await self.writer.write_raw(
                content=text,
                app_id=resolved_app_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                memory_at=memory_at,
                request_id=request_id,
                workspace_id=workspace_id,
                workspace_remote=workspace_remote,
            )
        return WriteResult(
            success=True,
            memory_id=result.memory_id,
            request_id=request_id,
            processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
            commit_passed=False,
            extracted_count=0,
            is_ephemeral=True,
            status="raw_only",
        )

    async def distill(
        self,
        *,
        content: str = "",
        messages: list[dict] | list[ChatMessage] | None = None,
        user_id: str,
        source_node_ids: list[str],
        app_id: str | None = None,
        agent_id: str = "",
        session_id: str = "",
        memory_at: int | None = None,
    ) -> WriteResult:
        """Extract from existing L1 nodes without writing another L1."""
        request_id = str(uuid.uuid4())
        start = time.perf_counter()
        resolved_app_id = self._resolve_app_id(app_id)
        text, normalized, _turns = _prepare_write_text(
            content,
            messages,
            settings=self.settings,
        )
        if not text.strip() or not source_node_ids:
            return WriteResult(
                success=True,
                memory_id=source_node_ids[0] if source_node_ids else "",
                request_id=request_id,
                processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
                commit_passed=False,
                extracted_count=0,
                is_ephemeral=True,
                status="raw_only",
            )
        async with self._user_write_lock_ctx(resolved_app_id, user_id):
            result = await self.writer.distill(
                content=text,
                app_id=resolved_app_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                request_id=request_id,
                source_node_ids=list(source_node_ids),
                memory_at=memory_at,
                messages=normalized,
            )
        return WriteResult(
            success=True,
            memory_id=result.memory_id,
            request_id=request_id,
            processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
            commit_passed=result.commit_passed,
            extracted_count=len(result.extra_node_ids),
            extra_node_ids=list(result.extra_node_ids),
            is_ephemeral=result.is_ephemeral,
            decisions=result.decisions,
            status=result.status,
        )

    async def search(
        self,
        *,
        query: str,
        app_ids: list[str] | None = None,
        user_id: str,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.4,
        profile_limit: int | None = None,
        profile_min_score: float = 0.3,
        intention_limit: int = 0,
        include_derived: bool | None = None,
        created_after: int | None = None,
        request_id: str | None = None,
        debug: bool = False,
        include_derived_fusion: bool = True,
        as_of: int | None = None,
        raw_limit: int = 0,
        include_relations: bool = True,
        procedure_limit: int = 0,
        procedure_min_score: float = 0.3,
    ) -> SearchResult:
        """Semantic search; returns profile / proactive / normal / procedure groups.

        读路径只用 embedding + 混合检索，无 LLM，可在 agent 每轮生成前安全调用。
        Read path uses embedding + hybrid retrieval — no LLM; safe to call every user turn.

        Args:
            query: 查询文本 / the query string to recall against.
            app_ids: 应用隔离域，省略时用 ``[settings.default_app_id]`` /
                app isolation scope; defaults to ``[settings.default_app_id]``.
            user_id: 用户隔离域，必须与 ``add`` 时一致 / user scope, must match ``add``.
            agent_ids: 智能体隔离域（多 agent 共享同一 user 时用）/
                agent scope (when several agents share one user).
            session_ids: 限定在某些会话内检索 / restrict recall to specific sessions.
            limit: normal 路（L1 facts）返回上限 /
                cap on the normal route (L1 facts memories).
            min_score: normal 路最低融合分，低于此分丢弃 / min fused score for the normal route.
            profile_limit: profile 路（L2/L3 画像类）上限；省略时 System1
                关闭、dual 不限，显式 ``-1`` = 不限 / cap on the profile route;
                defaults to disabled for System1 and unlimited for dual, explicit ``-1`` = unlimited.
            profile_min_score: profile 路最低分 / min score for the profile route.
            intention_limit: proactive 路（L1/L3 意图）返回条数，``0`` = 关闭。意图是“用户接下来
                想做什么”的主动提醒，仅在需要主动推荐时才开；事实问答场景保持 0，以免意图噪声
                挤占事实召回。 / proactive route (L1/L3 intentions) size, ``0`` = OFF. Intentions
                are forward-looking "what the user plans to do" nudges — only enable for
                proactive recommendation; keep 0 for factual QA so intention noise does not
                crowd out facts.
            include_derived: 是否返回派生画像/evolution chain；省略时 System1
                为 ``False``、dual 为 ``True``。可显式覆盖。
            created_after: 只返回该 Unix 秒之后创建的记忆（时间窗过滤）/
                only memories created after this Unix-second cutoff.
            request_id: 日志/链路追踪 id，省略自动生成 / trace id for logs; auto-generated.
            debug: ``True`` 时填充 ``SearchResult.read_result`` 的逐阶段 trace /
                fill ``SearchResult.read_result`` with per-stage trace metadata.
            as_of: 有效期判定时刻（Unix 秒）。显式 ``valid_from``/``valid_until`` 不含该
                时刻的记忆（如已过期的 TEMPORAL 例外）被过滤；``None`` 不过滤。
                benchmark 传问题时间，在线场景传当前时间 / drop memories whose
                explicit validity window excludes this time.
            raw_limit: L0 原文兜底路条数，``0`` = 关闭。原文按语义 + BM25 RRF 排序，
                分数压在 0.5 以下，放在 ``SearchMemories.raw`` / fallback L0_RAW
                evidence for details extraction did not keep.
            include_relations: 为命中填充一跳 ``previous``（被直接取代的旧值）与仍有效的
                ``conflicts`` / attach the one-hop previous value and live conflicts.
            procedure_limit: 工程经验路（``kind="procedure"``）条数，``0`` = 关闭。
                procedure 不进入 normal/profile 路，分数只在本组内比较；coding agent
                的检索打开它，对话场景保持 0 / size of the procedure route, ``0`` = OFF.
                Procedures never enter the chat routes; their scores rank within the group.
            procedure_min_score: 工程经验路最低融合分 / min fused score for procedures.
        """
        request_id = request_id or str(uuid.uuid4())
        start = time.perf_counter()
        resolved_app_ids = (
            app_ids if app_ids is not None else [self.settings.default_app_id]
        )
        logger.info(
            "search app=%s user=%s query=%r limit=%d debug=%s",
            (resolved_app_ids[0] if resolved_app_ids else ""),
            user_id,
            query,
            limit,
            debug,
        )
        resolved_profile_limit = (
            (-1 if self.settings.enable_graph else 0)
            if profile_limit is None
            else profile_limit
        )
        resolved_include_derived = (
            self.settings.enable_graph if include_derived is None else include_derived
        )
        memories, trace = await self.reader.search(
            query=query,
            app_ids=resolved_app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            session_ids=session_ids,
            limit=limit,
            min_score=min_score,
            profile_limit=resolved_profile_limit,
            profile_min_score=profile_min_score,
            intention_limit=intention_limit,
            include_derived=resolved_include_derived,
            include_derived_fusion=include_derived_fusion,
            created_after=created_after,
            request_id=request_id,
            collect_trace=debug,
            as_of=as_of,
            raw_limit=raw_limit,
            include_relations=include_relations,
            procedure_limit=procedure_limit,
            procedure_min_score=procedure_min_score,
        )

        return SearchResult(
            success=True,
            request_id=request_id,
            memories=memories,
            processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
            read_result=trace,
        )

    async def search_conversation(
        self,
        *,
        query: str,
        app_ids: list[str] | None = None,
        user_id: str,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        created_after: int | None = None,
        request_id: str | None = None,
    ) -> SearchResult:
        """Search L0_RAW originals (including retained history). Not used by default QA."""
        request_id = request_id or str(uuid.uuid4())
        start = time.perf_counter()
        resolved_app_ids = (
            app_ids if app_ids is not None else [self.settings.default_app_id]
        )
        memories = await self.reader.search_conversation(
            query=query,
            app_ids=resolved_app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            session_ids=session_ids,
            limit=limit,
            min_score=min_score,
            created_after=created_after,
        )
        return SearchResult(
            success=True,
            request_id=request_id,
            memories=memories,
            processing_time_ms=round((time.perf_counter() - start) * 1000, 2),
        )

    async def get(self, memory_id: str) -> MemoryItem | None:
        """Fetch a single memory by id, or None if it does not exist."""
        node = self.factory.vector.get(memory_id)
        if node is None:
            return None
        return Reader.memory_node_to_item(node)

    async def list(
        self,
        *,
        app_id: str | None = None,
        user_id: str,
        agent_id: str = "",
        limit: int = 100,
        include_history: bool = False,
    ) -> list[MemoryItem]:
        """List current memories under the given app/user (optionally agent) scope."""
        resolved_app_id = self._resolve_app_id(app_id)
        where = build_filter(
            app_ids=[resolved_app_id],
            user_id=user_id,
            agent_ids=[agent_id],
            statuses=[MemoryStatus.ACTIVE],
            latest_only=not include_history,
        )
        nodes = self.factory.vector.get_many(where, limit=limit)
        return [Reader.memory_node_to_item(node) for node in nodes]

    async def update(self, memory_id: str, content: str) -> UpdateResult:
        """Append an explicit user correction and return its new version ID."""
        from dataclasses import replace

        node = self.factory.vector.get(memory_id)
        if node is None:
            return UpdateResult(success=False, error_code=404)
        if not content.strip() or not node.is_latest:
            return UpdateResult(success=False, error_code=409)
        scope = (node.app_id, node.user_id, node.agent_id)
        custom = dict(node.custom or {})
        index_text = content
        if node.kind == PROCEDURE_KIND:
            # A procedure correction replaces its solution; the task stays the index.
            draft = revised_procedure(node, content)
            custom["procedure"] = asdict(draft.detail())
            content, index_text = draft.content(), draft.index_text()
        replacement = replace(
            node,
            node_id=str(uuid.uuid4()),
            content=content,
            embedding=await self.factory.embed.embed(index_text),
            supersedes=[node.node_id],
            superseded_by=[],
            revision=0,
            gmt_created=int(time.time()),
            gmt_modified=None,
            s2_evidence_count=0,
            custom={
                **custom,
                "update_type": "OVERRIDE",
                "explicit_update": True,
            },
        )
        if not self.factory.vector.apply_version_plan(
            nodes=[replacement],
            expected={node.node_id: node.revision},
            archives=[],
            scope=scope,
        ):
            return UpdateResult(success=False, error_code=409)
        if self.mode == "dual":
            self.factory.cache.enqueue_s2_task(
                user_id=node.user_id, app_id=node.app_id, agent_id=node.agent_id
            )
        return UpdateResult(success=True, memory_id=replacement.node_id)

    async def delete(self, memory_id: str) -> DeleteResult:
        """Archive the current representation while retaining its evidence/history."""
        node = self.factory.vector.get(memory_id)
        if node is None:
            return DeleteResult(success=False, error_code=404)
        if not node.is_latest:
            return DeleteResult(success=False, error_code=409)
        if not self.factory.vector.apply_version_plan(
            nodes=[],
            expected={memory_id: node.revision},
            archives=[memory_id],
            scope=(node.app_id, node.user_id, node.agent_id),
        ):
            return DeleteResult(success=False, error_code=409)
        if self.mode == "dual":
            self.factory.cache.enqueue_s2_task(
                user_id=node.user_id, app_id=node.app_id, agent_id=node.agent_id
            )
        return DeleteResult(success=True)

    async def delete_bulk(
        self,
        *,
        app_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        confirm: bool = False,
    ) -> DeleteBulkResult:
        """Delete every memory in a scope; requires confirm=True as a safety guard."""
        if confirm is not True:
            return DeleteBulkResult(success=False, error_code=400)
        resolved_app_id = self._resolve_app_id(app_id)
        where: dict = {"app_id": {"$in": [resolved_app_id]}}
        if user_id is not None:
            where["user_id"] = user_id
        if agent_id is not None:
            where["agent_id"] = agent_id
        nodes = self.factory.vector.get_many(where)
        node_ids = [node.node_id for node in nodes]
        self.factory.vector.delete(node_ids)
        graph = self.factory.graph
        if graph is not None:
            graph.delete_scope(
                app_id=resolved_app_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        if self.settings.persist_history:
            for node in nodes:
                self.factory.history.append(
                    event="DELETE",
                    node_id=node.node_id,
                    user_id=node.user_id,
                    old=node.to_metadata(),
                    new=None,
                )
        return DeleteBulkResult(success=True, deleted=len(node_ids))

    async def list_scopes(
        self,
        *,
        app_id: str | None = None,
        limit: int = 5000,
    ) -> list[ScopeSummary]:
        """List distinct memory scopes (app_id, user_id, agent_id) present in storage."""
        where: dict = {}
        if app_id is not None:
            where["app_id"] = app_id
        nodes = self.factory.vector.get_many(where, limit=limit)
        counts: dict[tuple[str, str, str], int] = {}
        for node in nodes:
            key = (node.app_id, node.user_id, node.agent_id or "")
            counts[key] = counts.get(key, 0) + 1
        return [
            ScopeSummary(
                app_id=key[0],
                user_id=key[1],
                agent_id=key[2],
                memory_count=count,
            )
            for key, count in sorted(counts.items())
        ]

    async def digest(self) -> DigestResult:
        """Run pending System2 cognition (``dual`` mode only).

        The write path keeps no deferred work, so this is purely the L3 cognition
        queue; in ``system1`` it only housekeeps cached request outcomes.
        """
        timing: dict = {}
        if self.settings.request_outcome_ttl_seconds:
            cutoff = time.time() - self.settings.request_outcome_ttl_seconds
            self.factory.cache.purge_request_outcomes(older_than=cutoff)
        if not isinstance(self.writer, System2Writer):
            return DigestResult(success=True, processed=0, timing=timing)
        processed = await self.writer.digest_pending()
        timing.update(self.writer.last_digest_stats)
        if self.settings.purge_done_queues:
            self.factory.cache.purge_done_queues()
        return DigestResult(success=True, processed=processed, timing=timing)

    async def aclose(self) -> None:
        """Release storage resources. Idempotent; call once at application shutdown."""
        provider = self.factory._jev
        if provider is not None and hasattr(provider, "aclose"):
            await provider.aclose()
        for provider in (self.factory._embed, self.factory._llm):
            if (
                provider is not _UNSET
                and provider is not None
                and hasattr(provider, "aclose")
            ):
                await provider.aclose()
        self.factory.close()


def _prepare_write_text(
    content: str,
    messages: list | None,
    *,
    settings: Settings,
) -> tuple[str, list[ChatMessage] | None, int]:
    """Strip injection, drop system turns, shape history; return (text, msgs, user turns)."""
    if not messages:
        return strip_memory_injection(content), None, 0
    normalized = _normalize_messages(messages)
    normalized = [m for m in normalized if m.role != "system"]
    normalized = [
        ChatMessage(role=m.role, content=strip_memory_injection(m.content))
        for m in normalized
    ]
    normalized = [m for m in normalized if m.content.strip()]
    cpt = settings.chars_per_token
    threshold_chars = int(
        settings.llm_context_window * settings.extract_dialogue_context_ratio * cpt
    )
    normalized = _shape_history(
        normalized,
        threshold_chars=threshold_chars,
        assistant_max_chars=int(settings.extract_assistant_max_tokens * cpt),
    )
    user_turn_count = sum(
        1 for m in normalized if m.role == "user" and m.content.strip()
    )
    return _format_dialogue(normalized), normalized, user_turn_count


def _normalize_messages(messages: list) -> list[ChatMessage]:
    """Coerce the user's input (mixed dict / ChatMessage) into a clean ChatMessage list."""
    out: list[ChatMessage] = []
    for msg in messages:
        if isinstance(msg, ChatMessage):
            role = (msg.role or "user").lower()
            text = (msg.content or "").strip()
        elif isinstance(msg, dict):
            role = str(msg.get("role") or "user").lower()
            text = str(msg.get("content") or "").strip()
        else:
            continue
        if not text:
            continue
        out.append(ChatMessage(role=role, content=text))
    return out


def _shape_history(
    messages: list[ChatMessage],
    *,
    threshold_chars: int,
    assistant_max_chars: int,
) -> list[ChatMessage]:
    """Shape multi-turn input before extract WITHOUT dropping any turn.

    Truncation only kicks in when the whole dialogue is large: if the total content length is
    within ``threshold_chars`` (derived from the model context window), the dialogue passes
    through untouched — short batches keep their assistant turns in full. Above the threshold,
    assistant turns are truncated to ``assistant_max_chars``; user turns (the primary memory
    signal) are always preserved in full. Callers should omit ``role=system`` dialogue turns
    (filtered earlier in ``MemoryClient.add``); they are not memory content.

    ``threshold_chars<=0`` or ``assistant_max_chars<=0`` disables shaping entirely.
    """
    if threshold_chars <= 0 or assistant_max_chars <= 0:
        return messages
    total_chars = sum(len(m.content) for m in messages)
    if total_chars <= threshold_chars:
        return messages
    shaped: list[ChatMessage] = []
    for msg in messages:
        if msg.role == "user" or len(msg.content) <= assistant_max_chars:
            shaped.append(msg)
        else:
            shaped.append(
                ChatMessage(
                    role=msg.role, content=msg.content[:assistant_max_chars] + "…"
                )
            )
    return shaped


def _format_dialogue(messages: list[ChatMessage]) -> str:
    """Render normalized messages into a natural-language dialogue chunk for L0_RAW + extract."""
    parts: list[str] = []
    for msg in messages:
        if msg.role == "user":
            label = "[user]"
        elif msg.role == "assistant":
            label = "[assistant]"
        elif msg.role == "system":
            label = "[system]"
        else:
            label = f"[{msg.role}]"
        parts.append(f"{label}: {msg.content}")
    return "\n".join(parts)

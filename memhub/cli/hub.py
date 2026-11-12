# -*- coding: utf-8 -*-
"""`memhub hub`: one loopback Core per device, replicated through a sync target."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import time
import uuid
import webbrowser
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import typer
from pydantic import ValidationError

from memhub.hub import hosts as hub_hosts
from memhub.hub import service as hub_service
from memhub.hub import sync as hub_sync
from memhub.hub.browse import (
    canonical_workspace,
    current_rows,
    inspect_local_procedure_files,
)
from memhub.hub.client import HubClient
from memhub.hub.config import (
    CursorStore,
    SyncConfig,
    clear_sync_config,
    format_time,
    hub_root,
    hub_settings,
    load_sync_config,
    resolve_hub_config,
    runtime_status,
    save_sync_config,
)
from memhub.hub.sources import SOURCES, locate_transcript, scan_file, scan_sources
from memhub.retrieval.formatter import (
    covered_texts_by_source,
    format_memories,
    format_project_view,
)
from memhub.retrieval.hybrid_engine import novel_source_excerpt, source_excerpt

hub_app = typer.Typer(help="MemHub：每台设备一个本地 Core，跨 CLI 共享个人记忆，可跨设备同步")
HubDir = typer.Option(None, "--hub-dir", help="Hub 根目录（默认 ~/.memhub）")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# Hosts whose hook payload carries the turn itself (prompt on submit, answer on stop),
# so there is no transcript file to scan.
INLINE_SOURCES = ("trae",)
# Hosts that add whatever a prompt hook prints to the context, and read no JSON
# reply: the memories go out as plain text.
PLAIN_CONTEXT_SOURCES = ("kimi-code", "kiro")
HOOK_SOURCES = (*SOURCES, *INLINE_SOURCES)
# Per recalled dialogue in the UserPromptSubmit injection.
RAW_EXCERPT_CHARS = 1600
SAME_SOURCE_EXCERPT_CHARS = 480
# Default experimental thresholds when Core reply omits ``inject_policy``.
PROCEDURE_FULL_SCORE = 0.40
MAX_FULL_PROCEDURES = 2
HUB_INJECT_MAX_CHARS = 3000
HUB_INJECT_MAX_LINES = 120
# A prompt hook waits for recall before every turn; past this it is felt.
RECALL_WARN_SECONDS = 3.0


def make_client(hub_dir: str | None) -> HubClient:
    """Indirection for tests."""
    return HubClient.from_config(hub_dir)


def _echo(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _one_line(text: str, limit: int = 200) -> str:
    """Keep one memory on one line so an agent can read its id without noise."""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _targets(host: str, *, home: Path | None = None, path_env: str | None = None) -> list[str]:
    if host == "auto":
        return hub_hosts.detect_installed_hosts(home=home, path_env=path_env)
    if host == "all":
        return list(hub_hosts.HOSTS)
    if host not in hub_hosts.HOSTS:
        raise typer.BadParameter(
            f"unknown host {host!r}; choose from "
            f"{', '.join(hub_hosts.HOSTS)}, auto, or all"
        )
    return [host]


@contextmanager
def _hook_errors_logged(hub_dir: str | None, name: str) -> Iterator[None]:
    """Host hooks fail open; diagnostics stay under the local Hub home."""
    try:
        yield
    except Exception:
        log_dir = hub_root(hub_dir) / "logs"
        log_dir.mkdir(exist_ok=True)
        logger = logging.getLogger("memhub.hub.hooks")
        handler = logging.FileHandler(log_dir / "hooks.log", encoding="utf-8")
        logger.addHandler(handler)
        logger.exception("%s hook failed", name)
        logger.removeHandler(handler)
        handler.close()


def _spawn_detached(argv: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def _validate_base_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise typer.BadParameter("URL must be an absolute http(s) URL")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise typer.BadParameter(
            "a remote Core must be reached over HTTPS; use `connect ssh` otherwise"
        )
    return value


SYNC_KINDS = ("dir", "git", "http", "ssh")


def _sync_config(
    kind: str, target: str, *, api_key: str, ssh_arg: list[str], remote_command: str
) -> SyncConfig:
    if kind == "dir":
        path = Path(target).expanduser().resolve()
        return SyncConfig(kind="dir", target=str(path))
    if kind == "git":
        return SyncConfig(kind="git", target=target)
    if kind == "http":
        if not api_key:
            raise typer.BadParameter("connect http needs --api-key (the server's runtime-token)")
        return SyncConfig(kind="http", target=_validate_base_url(target), api_key=api_key)
    if kind == "ssh":
        return SyncConfig(
            kind="ssh", target=target, ssh_args=list(ssh_arg), remote_command=remote_command
        )
    raise typer.BadParameter(f"unknown kind {kind!r}; choose from {', '.join(SYNC_KINDS)}")


def _check_target(cfg: SyncConfig, hub_dir: str | None) -> None:
    """Reach the target once before saving it, so a typo fails here, not silently later."""
    if cfg.kind == "dir":
        Path(cfg.target).mkdir(parents=True, exist_ok=True)
        return
    if cfg.kind == "git":
        result = subprocess.run(
            ["git", "ls-remote", cfg.target], capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise typer.BadParameter(f"cannot reach {cfg.target}: {result.stderr.strip()}")
        return
    settings = hub_settings(hub_dir)
    try:
        hub_sync.make_backend(cfg, hub_dir).open(
            {"model": settings.embed_model, "dim": settings.embed_dim}, ""
        )
    except hub_sync.SyncError as exc:
        hint = (
            " Is `memhub install-service` running there, and does `ssh` "
            "reach it without a password prompt?"
            if cfg.kind == "ssh"
            else ""
        )
        raise typer.BadParameter(f"{exc}.{hint}") from exc


async def _local_core(hub_dir: str | None, method: str, path: str) -> dict[str, Any] | None:
    """Call the Core on this machine; None when it is not running."""
    try:
        async with make_client(hub_dir) as client:
            return await client.relay(method, path)
    except (RuntimeError, httpx.TransportError):
        return None


@hub_app.command("connect")
def hub_connect(
    kind: str = typer.Argument(..., help="dir | git | http | ssh"),
    target: str = typer.Argument(
        ...,
        help="dir: 同步文件夹（iCloud Drive / Dropbox / Syncthing / NAS）；"
        "git: 仓库地址；http: 远端 Core 的 https URL；ssh: user@server 或 ~/.ssh/config 的 Host",
    ),
    api_key: str = typer.Option("", "--api-key", help="http：远端 Core 的 runtime-token"),
    ssh_arg: list[str] = typer.Option(
        [], "--ssh-arg", help="ssh：透传给 ssh 的参数，可重复，如 --ssh-arg=-p --ssh-arg=2222"
    ),
    remote_command: str = typer.Option(
        "memhub", "--remote-command", help="ssh：服务器上运行 memhub 的命令"
    ),
    hub_dir: str | None = HubDir,
) -> None:
    """让本机记忆与其它设备同步。记忆始终在本机检索；同步只交换变更日志。"""
    cfg = _sync_config(
        kind, target, api_key=api_key, ssh_arg=ssh_arg, remote_command=remote_command
    )
    _check_target(cfg, hub_dir)
    save_sync_config(cfg, hub_dir)

    async def apply() -> dict[str, Any] | None:
        if await _local_core(hub_dir, "POST", "/v1/hub/sync/reload") is None:
            return None
        try:
            async with make_client(hub_dir) as client:
                return await client.sync()
        except httpx.HTTPStatusError as exc:
            return {"error": exc.response.text}

    first = asyncio.run(apply())
    _echo(
        {
            "hub_dir": str(hub_root(hub_dir)),
            "sync": cfg.public_dict(),
            "first_sync": first
            if first is not None
            else "local Core not running; it syncs when `memhub serve` starts",
        }
    )


@hub_app.command("disconnect")
def hub_disconnect(hub_dir: str | None = HubDir) -> None:
    """停止同步。本机记忆保持不变，同步目标上的数据也不删除。"""
    previous = load_sync_config(hub_dir)
    removed = clear_sync_config(hub_dir)
    asyncio.run(_local_core(hub_dir, "POST", "/v1/hub/sync/reload"))
    _echo(
        {
            "hub_dir": str(hub_root(hub_dir)),
            "sync_enabled": False,
            "removed_sync": previous.public_dict() if previous else None,
            "message": "sync disconnected"
            if removed
            else "already disconnected (no sync target was configured)",
        }
    )


@hub_app.command("sync")
def hub_sync_now(
    compact: bool = typer.Option(
        False,
        "--compact",
        help="同时把同步目标上的日志折叠成快照（只在负责 digest 的设备上生效；"
        "平时日志超过阈值会自动折叠）",
    ),
    hub_dir: str | None = HubDir,
) -> None:
    """立刻与同步目标交换一次变更（Core 平时会自动同步）。"""

    async def run() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.sync(compact=compact)

    _echo(asyncio.run(run()))


@hub_app.command("relay", hidden=True)
def hub_relay(
    method: str = typer.Argument(...),
    path: str = typer.Argument(...),
    hub_dir: str | None = HubDir,
) -> None:
    """`connect ssh` 的服务端入口：把 stdin 的 JSON 请求转给本机 Core。"""
    if not path.startswith("/v1/hub/log/"):
        raise typer.BadParameter("relay only serves /v1/hub/log/*")
    raw = sys.stdin.read()
    body = json.loads(raw) if raw.strip() else None

    async def run() -> Any:
        async with make_client(hub_dir) as client:
            return await client.relay(method, path, body)

    typer.echo(json.dumps(asyncio.run(run()), ensure_ascii=False))


@hub_app.command("serve")
def hub_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="只允许 loopback 地址"),
    port: int = typer.Option(8765, "--port"),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="MEMHUB_HUB_SERVER_API_KEY",
        help="可选固定 Bearer key；默认自动生成 runtime-token",
    ),
    mode: str | None = typer.Option(None, "--mode", help="system1 | dual"),
    digest_every: float = typer.Option(
        24.0,
        "--digest-every",
        help="dual 模式：距上次成功自动 digest 至少多少小时才再跑，0 = 关闭",
    ),
    hub_dir: str | None = HubDir,
) -> None:
    """启动本机 Hub Core；记忆与 Evidence 留在本机 Hub 根目录，按 hub.json 同步。"""
    import uvicorn

    from memhub.hub.server import create_hub_app

    if host not in LOOPBACK_HOSTS:
        # The bearer token travels in plain HTTP; remote access goes through ssh / TLS.
        raise typer.BadParameter(
            "Hub Core listens on loopback only; other devices sync with "
            "`memhub connect ssh` or through your own HTTPS reverse proxy"
        )
    url_host = f"[{host}]" if ":" in host else host
    app = create_hub_app(
        api_key=api_key,
        hub_dir=hub_dir,
        settings=hub_settings(hub_dir, mode=mode),
        published_base_url=f"http://{url_host}:{port}",
        digest_min_hours=digest_every,
    )
    uvicorn.run(app, host=host, port=port)


@hub_app.command("install-service")
def hub_install_service(
    port: int = typer.Option(8765, "--port"),
    hub_dir: str | None = HubDir,
) -> None:
    """开机/登录自动启动 Hub Core（macOS LaunchAgent / Linux systemd --user）。

    服务不继承 shell 环境变量，模型 API key 请写在 ~/.memhub/config.yaml。"""
    _echo(hub_service.install_service(hub_dir=hub_dir, port=port))


@hub_app.command("uninstall-service")
def hub_uninstall_service() -> None:
    """停止并移除 Hub Core 自启动服务（不删除记忆数据）。"""
    _echo(hub_service.uninstall_service())


@hub_app.command("extract")
def hub_extract(hub_dir: str | None = HubDir) -> None:
    """对 --raw-only 导入的 Evidence 补跑抽取（后台排队，复用已存的 L0）。"""

    async def run() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.extract_raw()

    _echo(asyncio.run(run()))


@hub_app.command("retry")
def hub_retry(hub_dir: str | None = HubDir) -> None:
    """把重试耗尽而失败的写入重新排队。"""

    async def run() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.retry_failed()

    _echo(asyncio.run(run()))


@hub_app.command("status")
def hub_status(hub_dir: str | None = HubDir) -> None:
    """Core 记忆/队列状态，以及本机扫描进度和宿主接入状态。"""

    async def remote() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.status()

    data = asyncio.run(remote())
    for section in ("digest", "sync"):
        if isinstance(data.get(section), dict):
            data[section]["last_at"] = format_time(data[section].get("last_at"))
    with CursorStore(hub_dir) as cursors:
        scanned = Counter()
        for cursor in cursors.all():
            scanned[cursor.source] += cursor.turns
    configured = load_sync_config(hub_dir)
    data.update(
        {
            "runtime": runtime_status(hub_dir),
            "sync_config": configured.public_dict() if configured else None,
            "turns_scanned": dict(scanned),
            "hosts": hub_hosts.hosts_status(),
        }
    )
    _echo(data)


@hub_app.command("ui")
def hub_ui(
    no_open: bool = typer.Option(False, "--no-open", help="只打印地址，不打开浏览器"),
    hub_dir: str | None = HubDir,
) -> None:
    """在浏览器里按项目查看、搜索、编辑、删除记忆（页面由本机 Core 提供）。"""
    cfg = resolve_hub_config(hub_dir)
    # The token rides in the fragment: it never reaches a server log or a referer.
    url = f"{cfg.base_url}/ui#token={cfg.api_key}"
    typer.echo(f"{cfg.base_url}/ui")
    if not no_open:
        webbrowser.open(url)


@hub_app.command("install-host")
def hub_install_host(
    host: str = typer.Argument(
        "auto",
        help=f"{' | '.join(hub_hosts.HOSTS)} | auto | all（默认 auto 只接入本机已安装工具）",
    ),
    hub_dir: str | None = HubDir,
) -> None:
    """安装 Hub MCP 代理、recall skill 与采集 Hook（默认自动探测本机已安装工具）。"""
    _echo(
        [
            hub_hosts.install_host(item, hub_dir=hub_dir)
            for item in _targets(host)
        ]
    )


@hub_app.command("uninstall-host")
def hub_uninstall_host(
    host: str = typer.Argument("all", help=f"{' | '.join(hub_hosts.HOSTS)} | auto | all"),
) -> None:
    """移除 Hub 写入的配置，其它宿主配置保持不动。"""
    _echo([hub_hosts.uninstall_host(item) for item in _targets(host)])


@hub_app.command("scan")
def hub_scan(
    source: str = typer.Option(
        "all",
        "--source",
        "-s",
        help=f"{' | '.join(SOURCES)} | all",
    ),
    file: Path | None = typer.Option(None, "--file", help="只扫一个会话文件"),
    complete: bool = typer.Option(False, "--complete", help="最后一轮已经结束"),
    raw_only: bool = typer.Option(False, "--raw-only", help="只保留原始 Evidence，之后可用 hub extract 补抽取"),
    limit: int = typer.Option(0, "--limit", help="最多写入轮数，0 = 不限"),
    batch: int = typer.Option(5, "--batch", help="同会话每次提交的轮数"),
    idle: int = typer.Option(300, "--idle", help="静默多少秒视为一轮结束"),
    hub_dir: str | None = HubDir,
) -> None:
    """按本机游标增量读取会话，并提交给唯一 Hub Core。"""
    selected = list(SOURCES) if source == "all" else [source]
    if not set(selected) <= set(SOURCES):
        raise typer.BadParameter(f"unknown source {source!r}")

    async def run() -> Any:
        async with make_client(hub_dir) as client:
            options = {
                "raw_only": raw_only,
                "batch_turns": batch,
                "idle_seconds": idle,
            }
            if file is None:
                return await scan_sources(
                    client,
                    hub_dir=hub_dir,
                    sources=selected,
                    max_turns=limit,
                    **options,
                )
            with CursorStore(hub_dir) as cursors:
                return {
                    selected[0]: await scan_file(
                        client,
                        source=selected[0],
                        path=file,
                        cursors=cursors,
                        complete=complete,
                        max_turns=limit,
                        **options,
                    )
                }

    _echo(asyncio.run(run()))


def _capture(source: str, payload: dict[str, Any], hub_dir: str | None) -> None:
    """Locate the transcript and scan it in the background; the host returns at once."""
    transcript = locate_transcript(source, payload)
    if transcript is None:
        return
    argv = [
        sys.executable,
        "-m",
        "memhub.cli",
        "scan",
        "--source",
        source,
        "--file",
        str(transcript),
        "--complete",
    ]
    if hub_dir:
        argv += ["--hub-dir", hub_dir]
    _spawn_detached(argv, hub_root(hub_dir) / "logs" / "capture.log")


def _pending_prompt(hub_dir: str | None, source: str, session_id: str) -> Path:
    return hub_root(hub_dir) / "pending" / f"{source}-{session_id}.json"


def _stash_prompt(payload: dict[str, Any], hub_dir: str | None, source: str) -> None:
    """Keep the prompt until the host's ``Stop`` brings the answer."""
    prompt = payload.get("prompt", "").strip()
    session_id = payload.get("session_id")
    if not prompt or not session_id:
        return
    path = _pending_prompt(hub_dir, source, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_ws = payload.get("cwd") or next(iter(payload.get("workspace_roots") or []), None)
    workspace = canonical_workspace(raw_ws) or None
    path.write_text(
        json.dumps(
            {"prompt": prompt, "at": int(time.time()), "workspace": workspace},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _capture_inline(source: str, payload: dict[str, Any], hub_dir: str | None) -> None:
    """A host with no transcript file: the turn is the stashed prompt plus the answer
    its ``Stop`` payload carries."""
    session_id = payload.get("session_id")
    path = _pending_prompt(hub_dir, source, session_id) if session_id else None
    if path is None or not path.is_file():
        return
    pending = json.loads(path.read_text(encoding="utf-8"))
    answer = payload.get("last_assistant_message", "").strip()
    messages = [{"role": "user", "content": pending["prompt"]}]
    if answer:
        messages.append({"role": "assistant", "content": answer})
    digest = hashlib.sha256(f"{pending['prompt']}\n{answer}".encode()).hexdigest()[:16]

    async def write() -> None:
        async with make_client(hub_dir) as client:
            await client.write(
                messages=messages,
                session_id=f"{source}:{session_id}",
                memory_at=pending["at"],
                request_id=f"memhub:{source}:{session_id}:{digest}",
                workspace_id=pending["workspace"],
            )

    asyncio.run(write())
    # Only once the Core holds the turn: if the write raises, the prompt stays and
    # a repeated Stop of this turn still has it (same request id, stored once).
    path.unlink()


def _when(item: dict[str, Any]) -> str:
    """The ``[time] `` prefix of a recalled dialogue; none when it carries no time."""
    return f"[{format_time(item['memory_at'])}] " if item.get("memory_at") else ""


def _other_host(source: str, payload: dict[str, Any]) -> bool:
    """Cursor and Trae also run the hooks in ~/.claude/settings.json. Each has its own
    entry that reads its own payload, so every other source leaves theirs alone."""
    if source != "cursor" and "cursor_version" in payload:
        return True
    return source not in ("cursor", "trae") and "workspace_roots" in payload


def _with_local_drift(items: list[dict], workspace: str | None) -> list[dict]:
    """Attach device-local file-presence hints without mutating caller/store dicts."""
    if not items:
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        drift = inspect_local_procedure_files(it.get("procedure"), workspace)
        out.append({**it, "_local_drift": drift} if drift else dict(it))
    return out


def build_hub_prompt_context(
    result: dict[str, Any],
    prompt: str,
    *,
    workspace: str | None = None,
    raw_excerpt_chars: int = RAW_EXCERPT_CHARS,
    same_source_excerpt_chars: int = SAME_SOURCE_EXCERPT_CHARS,
    max_chars: int | None = None,
    max_lines: int | None = None,
    procedure_full_score: float | None = None,
) -> str:
    """Budgeted progressive-disclosure context: project view + dynamic recall + novel L0."""
    policy = result.get("inject_policy") if isinstance(result.get("inject_policy"), dict) else {}
    char_cap = HUB_INJECT_MAX_CHARS if max_chars is None else max_chars
    if max_chars is None and isinstance(policy.get("max_chars"), int):
        char_cap = policy["max_chars"]
    line_cap = HUB_INJECT_MAX_LINES if max_lines is None else max_lines
    if max_lines is None and isinstance(policy.get("max_lines"), int):
        line_cap = policy["max_lines"]
    proc_threshold = (
        PROCEDURE_FULL_SCORE if procedure_full_score is None else procedure_full_score
    )
    if procedure_full_score is None and isinstance(
        policy.get("procedure_full_score"), (int, float)
    ):
        proc_threshold = float(policy["procedure_full_score"])

    raw_memories = result.get("memories") or {}
    memories: dict[str, Any] = {
        k: (
            _with_local_drift(v, workspace)
            if k == "procedures" and isinstance(v, list)
            else list(v)
            if isinstance(v, list)
            else v
        )
        for k, v in raw_memories.items()
    }
    raw_view = result.get("project_view")
    project_view: dict[str, Any] | None = None
    if isinstance(raw_view, dict):
        project_view = {
            **raw_view,
            "procedures": _with_local_drift(raw_view.get("procedures") or [], workspace),
        }

    # Decide the complete items that fit, then dedupe the project view against
    # only those survivors. Dropping a trailing recall item must put a project
    # rule back into the view; never cut a fact in the middle of its text.
    def _kept_ids(mems: dict[str, Any]) -> set[str]:
        return {
            str(it.get("memory_id") or it.get("id"))
            for group in ("profile", "proactive", "normal", "procedures")
            for it in (mems.get(group) or [])
            if isinstance(it, dict) and (it.get("memory_id") or it.get("id"))
        }

    def _within(text: str) -> bool:
        if char_cap > 0 and len(text) > char_cap:
            return False
        if line_cap > 0 and len(text.splitlines()) > line_cap:
            return False
        return True

    view_budget = (
        1200 if char_cap <= 0 else min(char_cap, max(int(char_cap * 0.40), 240))
    )
    working_memories = dict(memories)
    chosen_full_procs = MAX_FULL_PROCEDURES
    view_block, view_ids = "", set()
    mem_block = ""
    curated = ""
    for _ in range(64):
        view_block, view_ids = format_project_view(
            project_view,
            exclude_ids=_kept_ids(working_memories),
            max_chars=view_budget if project_view else 0,
        )
        mem_block = format_memories(
            working_memories,
            procedure_full_score=proc_threshold,
            max_full_procedures=chosen_full_procs,
        ).strip()
        curated = "\n\n".join(b for b in (view_block, mem_block) if b).strip()
        if _within(curated):
            break
        if chosen_full_procs > 0:
            chosen_full_procs = 0 if chosen_full_procs == 1 else 1
            continue
        reduced = False
        for group in ("normal", "proactive", "profile", "procedures"):
            items = list(working_memories.get(group) or [])
            if items:
                working_memories = {**working_memories, group: items[:-1]}
                reduced = True
                break
        if reduced:
            continue
        if view_block and view_budget > 0:
            nxt = len(view_block) - len(view_block.splitlines()[-1]) - 1
            if nxt < view_budget:
                view_budget = max(0, nxt)
                continue
        break

    covered = covered_texts_by_source(
        working_memories,
        procedure_full_score=proc_threshold,
        max_full_procedures=chosen_full_procs,
    )
    if project_view and view_ids:
        for item in project_view.get("conventions") or []:
            mid = str(item.get("memory_id") or item.get("id") or "")
            if mid in view_ids:
                text = str(item.get("content") or "").strip()
                sids = [item.get("source_node_id"), *(item.get("source_ids") or [])]
                for sid in dict.fromkeys(str(s) for s in sids if s and text):
                    covered.setdefault(sid, []).append(text)
        for item in project_view.get("procedures") or []:
            mid = str(item.get("memory_id") or item.get("id") or "")
            if mid in view_ids:
                proc = item.get("procedure") if isinstance(item.get("procedure"), dict) else {}
                text = str(proc.get("task") or "").strip()
                sids = [item.get("source_node_id"), *(item.get("source_ids") or [])]
                for sid in dict.fromkeys(str(s) for s in sids if s and text):
                    covered.setdefault(sid, []).append(text)

    conversations = result.get("conversations") or []
    if conversations:
        turns = sorted(conversations, key=lambda item: item.get("memory_at") or 0)
        header = "历史对话（从旧到新，同一件事以最新的为准）：\n"
        rem_chars = (
            char_cap - len(curated) - (2 if curated else 0) - len(header)
            if char_cap > 0
            else raw_excerpt_chars * len(turns)
        )
        rem_lines = (
            line_cap - len(curated.splitlines()) - (2 if curated else 0)
            if line_cap > 0
            else 0
        )
        candidate_excerpts: list[str] = []
        for item in turns:
            if rem_chars <= 40:
                break
            raw_text = str(item.get("content") or "").strip()
            if not raw_text:
                continue
            mid = str(item.get("memory_id") or "")
            retired_claims = [
                str(x) for x in (item.get("retired_claims") or []) if str(x).strip()
            ]
            active_covered = list(covered.get(mid) or [])
            if active_covered or retired_claims:
                snippet = novel_source_excerpt(
                    raw_text,
                    prompt,
                    active_covered,
                    min(same_source_excerpt_chars, max(rem_chars - 24, 0)),
                    retired_texts=retired_claims,
                )
            else:
                per_turn_cap = (
                    min(raw_excerpt_chars, max(rem_chars - 24, 0))
                    if char_cap > 0
                    else raw_excerpt_chars
                )
                snippet = source_excerpt(raw_text, prompt, [], per_turn_cap)
            if snippet.strip():
                candidate_excerpts.append(f"- {_when(item)}{snippet.strip()}")

        if candidate_excerpts:
            if char_cap > 0 or line_cap > 0:
                kept_indices: list[int] = []
                used_chars = 0
                used_lines = 0
                for idx in range(len(candidate_excerpts) - 1, -1, -1):
                    c_cost = len(candidate_excerpts[idx]) + (1 if kept_indices else 0)
                    l_cost = len(candidate_excerpts[idx].splitlines())
                    if char_cap > 0 and used_chars + c_cost > rem_chars:
                        continue
                    if line_cap > 0 and used_lines + l_cost > rem_lines:
                        continue
                    kept_indices.append(idx)
                    used_chars += c_cost
                    used_lines += l_cost
                kept_indices.reverse()
                candidate_excerpts = [candidate_excerpts[i] for i in kept_indices]
            if candidate_excerpts:
                raw = "\n".join(candidate_excerpts)
                curated = f"{curated}\n\n{header}{raw}".strip()

    warnings = result.get("warnings") or []
    if warnings:
        warning_block = "\n".join(str(w) for w in warnings)
        curated = f"{curated}\n\n{warning_block}".strip()
    return curated


def _recall(payload: dict[str, Any], limit: int, hub_dir: str | None, source: str) -> None:
    """Answer the host's prompt event with the memories relevant to this prompt."""
    prompt = payload.get("prompt", "").strip()
    if not prompt:
        return
    # Cursor names the open folders instead of a working directory.
    raw_ws = payload.get("cwd") or next(iter(payload.get("workspace_roots") or []), None)
    workspace = canonical_workspace(raw_ws) or None

    async def search() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.recall(
                query=prompt, limit=limit, raw_limit=3, workspace=workspace
            )

    result = asyncio.run(search())
    context = build_hub_prompt_context(result, prompt, workspace=workspace)
    if context:
        # Tagged so strip_memory_injection drops it if a host echoes it back.
        context = f"<relevant-memories>\n{context}\n</relevant-memories>"
        if source in PLAIN_CONTEXT_SOURCES:
            typer.echo(context)
            return
        typer.echo(
            json.dumps(
                {"additional_context": context}
                if source == "cursor"
                else {"additionalContext": context}
                if source == "copilot"
                else {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context,
                    }
                },
                ensure_ascii=False,
            )
        )


def _is_prompt_event(payload: dict[str, Any]) -> bool:
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if event in ("UserPromptSubmit", "beforeSubmitPrompt", "userPromptSubmitted"):
        return True
    # Copilot's native userPromptSubmitted stdin carries prompt + sessionId without an event field.
    return event is None and "prompt" in payload


@hub_app.command("hook")
def hub_hook(
    source: str = typer.Argument(..., help=f"{' | '.join(HOOK_SOURCES)}"),
    limit: int = typer.Option(8, "--limit", help="UserPromptSubmit 注入的记忆条数"),
    hub_dir: str | None = HubDir,
) -> None:
    """宿主 Hook 唯一入口：``UserPromptSubmit`` 召回注入，其余事件（Stop / run.completed /
    session.ended）后台采集。按 payload 的 ``hook_event_name`` 分派，Claude Code / Codex /
    agentica 同一协议。"""
    with _hook_errors_logged(hub_dir, f"hook {source}"):
        payload = json.loads(sys.stdin.read() or "{}")
        if _other_host(source, payload):
            return
        if _is_prompt_event(payload):
            if source in INLINE_SOURCES:
                _stash_prompt(payload, hub_dir, source)
            _recall(payload, limit, hub_dir, source)
        elif source in INLINE_SOURCES:
            _capture_inline(source, payload, hub_dir)
        else:
            _capture(source, payload, hub_dir)


@hub_app.command("recall")
def hub_recall(
    query: str = typer.Argument("", help="检索关键词（传 --source-id 时可省略）"),
    source_id: str = typer.Option(
        "",
        "--source-id",
        "-i",
        help="按 L0 来源短 ID（如 37c85fe9）直接展开完整原始对话与前后上下文",
    ),
    context_turns: int = typer.Option(
        1, "--context-turns", help="按 --source-id 展开时附带同会话前后轮数"
    ),
    limit: int = typer.Option(10, "--limit"),
    raw_limit: int = typer.Option(5, "--raw-limit"),
    workspace: str = typer.Option("", "--workspace", "-w", help="限定项目路径并返回项目约定视图"),
    json_out: bool = typer.Option(False, "--json"),
    hub_dir: str | None = HubDir,
) -> None:
    """同时检索当前记忆和历史对话 Evidence，或按 --source-id 展开 L0 原话。"""
    target_id = source_id.strip()
    if not target_id and not query.strip():
        raise typer.BadParameter("请提供 query 检索词或 --source-id")

    if target_id:
        async def fetch_source() -> dict[str, Any]:
            async with make_client(hub_dir) as client:
                return await client.get_conversation(
                    target_id, context_turns=context_turns
                )

        result = asyncio.run(fetch_source())
        if json_out:
            _echo(result)
            return
        lines = []
        for item in result.get("conversations") or []:
            marker = " [当前来源]" if item.get("is_anchor") else ""
            lines.append(
                f"- {_when(item)}[{item['session_id']}] (L0 {item.get('source_id', '')}){marker}\n"
                f"{item['content'].strip()}"
            )
        typer.echo("\n\n".join(lines) or "没有找到对应的原始对话。")
        return

    canon_ws = canonical_workspace(workspace) or None

    async def search() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            kw: dict[str, Any] = {"query": query, "limit": limit, "raw_limit": raw_limit}
            if canon_ws is not None:
                kw["workspace"] = canon_ws
            return await client.recall(**kw)

    result = asyncio.run(search())
    if json_out:
        _echo(result)
        return
    sections = []
    view_text, _ = format_project_view(result.get("project_view"))
    if view_text:
        sections.append(f"## 项目约定与目录\n{view_text}")
    memories = format_memories(result["memories"]).strip()
    if memories:
        sections.append(f"## 记忆\n{memories}")
    if result["conversations"]:
        sections.append(
            "## 历史对话\n"
            + "\n".join(
                f"- [{item['session_id']}] {item['content'].strip()}"
                for item in result["conversations"]
            )
        )
    typer.echo("\n\n".join(sections) or "没有相关记忆。")


@hub_app.command("memory-get")
def hub_memory_get(
    memory_id: str = typer.Argument(..., help="记忆完整 ID 或 >=8 位前缀（如 procedure-index 中的 id）"),
    json_out: bool = typer.Option(False, "--json", help="输出完整 JSON"),
    hub_dir: str | None = HubDir,
) -> None:
    """按记忆 ID 精确展开单条记忆的完整内容（含 procedure 完整方案、适用边界与来源）。"""

    async def remote() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.get_memory(memory_id)

    item = asyncio.run(remote())
    if json_out:
        _echo(item)
        return
    proc = item.get("procedure")
    header = f"[{item.get('kind') or item.get('layer')}] id={item.get('memory_id')}"
    if not item.get("is_latest", True):
        header += " (历史旧版本)"
    lines = [header]
    if isinstance(proc, dict):
        lines.append(f"Task: {proc.get('task', '')}")
        lines.append(f"Solution:\n{proc.get('solution', '')}")
        if proc.get("boundary_envs"):
            lines.append(f"Boundary: {proc['boundary_envs']} (scope={proc.get('boundary_scope', 'project')})")
        if proc.get("files"):
            lines.append(f"Files: {', '.join(proc['files'])}")
    else:
        lines.append(str(item.get("content") or ""))
    src = item.get("source_node_id") or next(iter(item.get("source_ids") or []), "")
    if src:
        lines.append(f"Source: L0 {str(src)[:8]} ({src})")
    typer.echo("\n".join(lines))


@hub_app.command("remember")
def hub_remember(
    content: str = typer.Argument(..., help="要长期记住的内容"),
    hub_dir: str | None = HubDir,
) -> None:
    """交给本机 Core 写入一条记忆（与 MCP 的 memory_add 相同，已绑定个人 scope）。"""

    async def write() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.write(content=content, request_id=f"hub-cli:{uuid.uuid4()}")

    _echo(asyncio.run(write()))


@hub_app.command("memory-list")
def hub_memory_list(
    query: str = typer.Argument("", help="可选关键词，按记忆内容子串过滤"),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json", help="输出完整 JSON 行"),
    hub_dir: str | None = HubDir,
) -> None:
    """列出当前生效的记忆及 id，供 memory-update / memory-delete 使用。"""

    async def remote() -> list[dict[str, Any]]:
        async with make_client(hub_dir) as client:
            return await client.list_memories()

    rows = current_rows(asyncio.run(remote()), query=query, limit=limit)
    if json_out:
        _echo(rows)
        return
    typer.echo(
        "\n".join(
            f"{row['id']}\t{row['layer']}\t{_one_line(row['content'])}"
            for row in rows
        )
        or "没有匹配的当前记忆。"
    )


@hub_app.command("memory-update")
def hub_memory_update(
    memory_id: str = typer.Argument(..., help="memory-list 返回的 id"),
    content: str = typer.Argument(..., help="更正后的完整内容"),
    hub_dir: str | None = HubDir,
) -> None:
    """更正一条记忆：追加新版本，旧版本保留为历史。"""

    async def remote() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.update_memory(memory_id, content)

    _echo(asyncio.run(remote()))


@hub_app.command("memory-delete")
def hub_memory_delete(
    memory_id: str = typer.Argument(..., help="memory-list 返回的 id"),
    hub_dir: str | None = HubDir,
) -> None:
    """删除（归档）一条当前记忆。"""

    async def remote() -> dict[str, Any]:
        async with make_client(hub_dir) as client:
            return await client.delete_memory(memory_id)

    _echo(asyncio.run(remote()))


def daemon_status(root: Path) -> dict[str, Any] | None:
    """The live Core's ``/v1/hub/status``, or None when no Core answers.

    A plain sync request on purpose: ``memhub doctor`` is a sync CLI command and
    must also work from inside a running event loop (tests, notebooks), where
    ``asyncio.run`` raises.
    """
    try:
        cfg = resolve_hub_config(root)
    except (RuntimeError, ValueError, OSError):
        # No runtime.json or token: the Core was never started on this machine.
        return None
    try:
        response = httpx.get(
            f"{cfg.base_url}/v1/hub/status",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def run_doctor(hub_dir: str | Path | None = None) -> dict[str, Any]:
    """Read-only diagnostic of configuration, Core, write queue, sync and host wiring.

    Nothing here starts a Core, touches the memory store or edits a host config, so
    it is safe to run while the daemon is serving. A broken ``config.yaml`` is
    reported as a failing check instead of raising, because that is exactly the
    case the command exists for.
    """
    checks: list[dict[str, Any]] = []
    root = hub_root(hub_dir)

    # 1. Model credentials and configuration.
    try:
        settings = hub_settings(root)
    except (ValueError, ValidationError) as exc:
        checks.append(
            {
                "name": "Config & keys",
                "status": "fail",
                "detail": f"~/.memhub/config.yaml is invalid: {_one_line(exc, 200)}",
                "required": True,
            }
        )
        settings = None
    else:
        missing_keys = [
            name
            for name, val in (
                ("jev_api_key", settings.jev_api_key),
                ("llm_api_key", settings.llm_api_key),
                ("embed_api_key", settings.embed_api_key),
            )
            if not (val or "").strip()
        ]
        if missing_keys:
            checks.append(
                {
                    "name": "Config & keys",
                    "status": "fail",
                    "detail": f"missing {', '.join(missing_keys)} — set in ~/.memhub/config.yaml",
                    "required": True,
                }
            )
        else:
            checks.append(
                {
                    "name": "Config & keys",
                    "status": "ok",
                    "detail": (
                        f"mode={settings.mode}, "
                        f"embed={settings.embed_model} ({settings.embed_dim}d)"
                    ),
                    "required": True,
                }
            )

    # 2. Core daemon reachability.
    remote_status = daemon_status(root)

    rt = runtime_status(root)
    if remote_status is not None:
        base_url = (rt or {}).get("base_url", "http://127.0.0.1:8765")
        current = (remote_status.get("memories") or {}).get("current", 0)
        checks.append(
            {
                "name": "Core daemon",
                "status": "ok",
                "detail": f"healthy at {base_url} ({current} current memories)",
                "required": True,
            }
        )
    else:
        checks.append(
            {
                "name": "Core daemon",
                "status": "fail",
                "detail": "not reachable — run `memhub install-service` or `memhub serve`",
                "required": True,
            }
        )

    # 3. Write queue health.
    writes = (remote_status or {}).get("writes") or {}
    failed = int(writes.get("failed", 0))
    last_err = writes.get("last_error")
    if failed > 0:
        checks.append(
            {
                "name": "Write queue",
                "status": "warn",
                "detail": (
                    f"{failed} failed write(s)"
                    + (f" ({last_err})" if last_err else "")
                    + " — run `memhub retry`"
                ),
                "required": False,
            }
        )
    elif remote_status is not None:
        checks.append(
            {
                "name": "Write queue",
                "status": "ok",
                "detail": (
                    f"done={writes.get('done', 0)}, "
                    f"pending={writes.get('pending', 0)}, failed=0"
                ),
                "required": False,
            }
        )

    # 3b. Store size and recall latency: recall reads more as the store grows.
    if remote_status is not None:
        current = int((remote_status.get("memories") or {}).get("current", 0))
        last = remote_status.get("recall")
        slow = last is not None and last["seconds"] > RECALL_WARN_SECONDS
        checks.append(
            {
                "name": "Recall latency",
                "status": "warn" if slow else "ok",
                "detail": (
                    f"{current} current memories; last recall "
                    + (f"{last['seconds']:.2f}s" if last is not None else "not yet run")
                    + (
                        f" (> {RECALL_WARN_SECONDS:g}s: the store has outgrown the "
                        "recall path, report it with this count)"
                        if slow
                        else ""
                    )
                ),
                "required": False,
            }
        )

    # 4. Sync configuration and status.
    try:
        sync_cfg = load_sync_config(root)
        sync_cfg_error = None
    except (ValueError, KeyError, TypeError) as exc:
        sync_cfg = None
        sync_cfg_error = f"{type(exc).__name__}: {exc}"
    if sync_cfg_error is not None:
        checks.append(
            {
                "name": "Sync target",
                "status": "fail",
                "detail": (
                    f"hub.json is invalid ({_one_line(sync_cfg_error, 160)}) — "
                    "run `memhub connect` again or `memhub disconnect`"
                ),
                "required": False,
            }
        )
    elif sync_cfg is None:
        checks.append(
            {
                "name": "Sync target",
                "status": "ok",
                "detail": "disabled (local only)",
                "required": False,
            }
        )
    else:
        sync_info = (remote_status or {}).get("sync") or {}
        sync_err = sync_info.get("last_error")
        checks.append(
            {
                "name": "Sync target",
                "status": "warn" if sync_err else "ok",
                "detail": (
                    f"{sync_cfg.kind} ({sync_cfg.target}) — error: {sync_err} — run `memhub sync`"
                    if sync_err
                    else f"{sync_cfg.kind} ({sync_cfg.target})"
                ),
                "required": False,
            }
        )

    # 5. Detected AI hosts and wiring status.
    detected = hub_hosts.detect_installed_hosts()
    h_status = hub_hosts.hosts_status()
    broken = [h for h in detected if h_status[h].get("error")]
    if broken:
        checks.append(
            {
                "name": "Host configs",
                "status": "warn",
                "detail": "; ".join(
                    f"{h}: {_one_line(h_status[h]['error'], 120)}" for h in broken
                )
                + " — fix the file, then run `memhub install-host`",
                "required": False,
            }
        )
    wired = [
        h
        for h in detected
        if h_status[h]["skill"]
        and h_status[h]["mcp"] in (True, None)
        and h_status[h]["hooks"] in (True, None)
    ]
    unwired = [h for h in detected if h not in wired]
    if not detected:
        checks.append(
            {
                "name": "Detected hosts",
                "status": "ok",
                "detail": "no supported AI hosts detected on this machine",
                "required": False,
            }
        )
    elif unwired:
        checks.append(
            {
                "name": "Detected hosts",
                "status": "warn",
                "detail": (
                    f"{len(wired)}/{len(detected)} wired; "
                    f"unwired: {', '.join(unwired)} — run `memhub install-host`"
                ),
                "required": False,
            }
        )
    else:
        checks.append(
            {
                "name": "Detected hosts",
                "status": "ok",
                "detail": f"{len(wired)}/{len(detected)} wired ({', '.join(wired)})",
                "required": False,
            }
        )

    ok = not any(c["required"] and c["status"] == "fail" for c in checks)
    return {
        "ok": ok,
        "hub_dir": str(root),
        "checks": checks,
        "detected_hosts": detected,
        "wired_hosts": wired,
    }


@hub_app.command("doctor")
def hub_doctor(
    json_out: bool = typer.Option(False, "--json", help="输出结构化 JSON 诊断报告"),
    hub_dir: str | None = HubDir,
) -> None:
    """只读诊断：检查配置密钥、本机 Core、写入队列、跨端同步与已安装宿主工具接入状态。"""
    report = run_doctor(hub_dir)
    if json_out:
        _echo(report)
    else:
        icons = {"ok": "✓", "warn": "!", "fail": "✗"}
        typer.echo("memhub doctor\n")
        for check in report["checks"]:
            icon = icons.get(check["status"], "?")
            typer.echo(f"  {icon} {check['name']:<18} {check['detail']}")
        typer.echo("")
        if report["ok"]:
            typer.echo("All required checks passed.")
        else:
            typer.echo("Required check(s) failed — see remediation above.")
    if not report["ok"]:
        raise typer.Exit(code=1)

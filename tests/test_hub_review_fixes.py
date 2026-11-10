"""Regressions for the review of the hub host / workspace / redaction commits.

1. One unparsable transcript does not stop a multi-source scan.
2. A short L0 id is resolved from ids, not by reading every L0 document.
3. A synced session of a same-named folder with another remote stays out.
4. A resend whose ``memory_at`` / workspace changed is the same write, not a 409.
5. ``doctor`` reports a broken ``hub.json`` / host config instead of crashing.
8. A memory edit drops ``<private>`` like every other write.
9. Block-shaped content and dict tool arguments are sanitized too.
10. Unclosed PEM headers are redacted in linear time.
13. An inline (Trae-style) turn survives an unreachable Core.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from memhub import MemoryClient
from memhub.cli import hub as cli_hub
from memhub.cli import main as cli_main
from memhub.config import Settings
from memhub.errors import IdempotencyConflictError
from memhub.hub import HubClient, scan_sources
from memhub.hub.browse import workspace_sessions
from memhub.hub.queue import HubWriteQueue
from memhub.hub.redact import redact_secrets, sanitize_messages
from memhub.hub.server import HUB_USER_ID, create_hub_app
from memhub.types import Layer, MemoryNode

runner = CliRunner()


def _core(root: Path, fake_embed, fake_llm, fake_jev) -> MemoryClient:
    return MemoryClient(
        settings=Settings(storage_dir=str(root / "store"), mode="system1"),
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )


class _Recorder:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def write(self, **kwargs: Any) -> dict:
        self.writes.append(kwargs)
        return {"status": "pending"}


def _agentica_transcript(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "content": "包管理用 pnpm", "timestamp": 1791000000},
        {"type": "assistant", "content": "好的，用 pnpm", "timestamp": 1791000001},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


async def test_unparsable_transcript_is_skipped_and_other_files_still_ingest(
    tmp_path: Path,
) -> None:
    from memhub.hub.sources import SOURCE_SPECS, default_source_roots

    roots = default_source_roots(tmp_path / "home")
    # A half-written Cline file (it rewrites ``<id>.messages.json`` in place).
    bad = roots["cline"] / "cl-x" / "cl-x.messages.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"messages": [{"role": "user", "content": "hi"', encoding="utf-8")
    good = roots["agentica"] / "p" / "s-good.jsonl"
    _agentica_transcript(good)
    # The good file must parse on its own, else this test proves nothing.
    assert SOURCE_SPECS["agentica"].read(good, 0)[0]

    client = _Recorder()
    summary = await scan_sources(
        client,
        hub_dir=tmp_path / "hub",
        sources=["cline", "agentica"],
        roots=roots,
        idle_seconds=0,
    )
    assert summary["cline"] == {"files": 1, "turns": 0, "errors": 1}
    assert summary["agentica"]["turns"] == 1 and summary["agentica"]["errors"] == 0
    assert [w["session_id"] for w in client.writes] == ["agentica:s-good"]

    # Once the host finishes writing, the next scan reads it: no cursor moved past it.
    bad.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "测试命令是 uv run pytest"},
                    {"role": "assistant", "content": "记下了"},
                ]
            }
        ),
        encoding="utf-8",
    )
    again = await scan_sources(
        client, hub_dir=tmp_path / "hub", sources=["cline"], roots=roots, idle_seconds=0
    )
    assert again["cline"] == {"files": 1, "turns": 1, "errors": 0}


async def test_unreachable_core_still_aborts_the_scan(tmp_path: Path) -> None:
    from memhub.hub.sources import default_source_roots

    roots = default_source_roots(tmp_path / "home")
    _agentica_transcript(roots["agentica"] / "p" / "s.jsonl")

    class Down:
        async def write(self, **_kw: Any) -> dict:
            raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await scan_sources(
            Down(), hub_dir=tmp_path / "hub", sources=["agentica"], roots=roots,
            idle_seconds=0,
        )


async def test_short_source_id_reads_only_the_matching_documents(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = _core(hub, fake_embed, fake_llm, fake_jev)
    app = create_hub_app(api_key="secret", hub_dir=hub, client=core)
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub", api_key="secret",
            transport=httpx.ASGITransport(app=app),
        ) as remote:
            ids = []
            for i in range(3):
                job = await remote.write(
                    content=f"第 {i} 条：端口是 80{i}", session_id=f"s{i}",
                    request_id=f"sid-{i}", wait=True,
                )
                ids.append(job["result"]["memory_id"])
            vector = core.factory.vector
            seen: list[list[str]] = []
            real = vector.get_by_ids

            def spy(node_ids: list[str]):
                seen.append(list(node_ids))
                return real(node_ids)

            def no_full_scan(*_a, **_kw):
                raise AssertionError("short id lookup read every L0 document")

            vector.get_by_ids = spy  # type: ignore[method-assign]
            vector.get_many = no_full_scan  # type: ignore[method-assign]
            out = await remote.get_conversation(ids[1][:8], context_turns=0)
    assert out["memory_id"] == ids[1]
    assert seen == [[ids[1]]]
    await core.aclose()


def _l0(node_id: str, session: str, workspace: str, remote: str | None) -> MemoryNode:
    custom: dict[str, str] = {"workspace_id": workspace}
    if remote:
        custom["workspace_remote"] = remote
    return MemoryNode(
        node_id=node_id, app_id="default", user_id="default", content=node_id,
        layer=Layer.L0_RAW, session_id=session, custom=custom,
    )


def test_synced_same_named_folder_with_another_remote_is_another_project(
    tmp_path: Path,
) -> None:
    here = tmp_path / "bob" / "api"
    (here / ".git").mkdir(parents=True)
    (here / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:bob/api.git\n', encoding="utf-8"
    )
    nodes = [
        # Alice's private repo, written on her machine: path absent here.
        _l0("a", "codex:alice", "/nonexistent/alice/repo/api", "alice/api"),
        # The same project as here, from another machine under another path.
        _l0("b", "codex:bob-laptop", "/nonexistent/laptop/code/api", "bob/api"),
        # Written before remotes were recorded: only the folder name is known.
        _l0("c", "codex:legacy", "/nonexistent/old/api", None),
    ]
    assert workspace_sessions(nodes, str(here)) == ["codex:bob-laptop", "codex:legacy"]


async def test_core_records_the_workspace_remote_on_l0(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/org/proj.git\n', encoding="utf-8"
    )
    hub = tmp_path / "hub"
    core = _core(hub, fake_embed, fake_llm, fake_jev)
    app = create_hub_app(api_key="secret", hub_dir=hub, client=core)
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub", api_key="secret",
            transport=httpx.ASGITransport(app=app),
        ) as remote:
            job = await remote.write(
                content="用 pnpm", session_id="s", request_id="ws-1", wait=True,
                workspace_id=str(repo / "src"),
            )
    raw = core.factory.vector.get(job["result"]["memory_id"])
    assert raw.custom == {"workspace_id": str(repo), "workspace_remote": "org/proj"}
    await core.aclose()


async def test_resend_with_a_new_time_or_workspace_is_the_same_write(
    tmp_path: Path,
) -> None:
    async def done(_p: dict) -> dict:
        return {}

    queue = HubWriteQueue(tmp_path / "hub.db", done)
    base = {
        "content": "", "messages": [{"role": "user", "content": "x"}],
        "session_id": "cline:a", "request_id": "r", "raw_only": False,
    }
    queue.submit({**base, "memory_at": 100, "workspace_id": "/w/a"})
    # Cline stamps the manifest's updated_at / mtime; a reread after an append moves it.
    again = queue.submit({**base, "memory_at": 200, "workspace_id": "/w/a-root"})
    assert again["status"] == "pending"
    # Different evidence under the same id is still refused.
    with pytest.raises(IdempotencyConflictError):
        queue.submit({**base, "messages": [{"role": "user", "content": "y"}]})
    await queue.close()


async def test_rows_from_the_old_fingerprint_are_migrated(tmp_path: Path) -> None:
    import hashlib
    import sqlite3

    async def done(_p: dict) -> dict:
        return {}

    payload = {
        "content": "x", "messages": None, "session_id": "s", "memory_at": 5,
        "request_id": "old",
    }
    queue = HubWriteQueue(tmp_path / "hub.db", done)
    await queue.close()
    conn = sqlite3.connect(tmp_path / "hub.db")
    legacy = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    conn.execute(
        "INSERT INTO hub_writes(request_id,fingerprint,payload,raw_only,status,"
        "event_at,created_at,updated_at) VALUES ('old',?,?,0,'done',5,5,5)",
        (legacy, json.dumps(payload)),
    )
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()

    queue = HubWriteQueue(tmp_path / "hub.db", done)
    assert queue.submit({**payload, "memory_at": 9, "raw_only": False})["status"] == "done"
    await queue.close()


async def test_sanitizer_settings_change_does_not_conflict_a_resend(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = _core(hub, fake_embed, fake_llm, fake_jev)
    app = create_hub_app(api_key="secret", hub_dir=hub, client=core)
    secret = "token sk-abcdefghijklmnopqrstuvwxyz123456"
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub", api_key="secret",
            transport=httpx.ASGITransport(app=app),
        ) as remote:
            await remote.write(content=secret, session_id="s", request_id="rs", wait=True)
            core.settings.redact_secrets = True
            again = await remote.write(content=secret, session_id="s", request_id="rs")
    assert again["status"] == "done"
    await core.aclose()


def test_doctor_reports_broken_hub_json_and_host_config(
    tmp_path: Path, monkeypatch
) -> None:
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "hub.json").write_text('{"sync": {"kind": "git",', encoding="utf-8")
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text('{"mcpServers": {},}', encoding="utf-8")
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty))
    for key in ("JEV", "LLM", "EMBED"):
        monkeypatch.setenv(f"MEMHUB_{key}_API_KEY", "k")
    monkeypatch.setattr(cli_hub, "daemon_status", lambda _root: None)

    out = runner.invoke(cli_main.app, ["doctor", "--json", "--hub-dir", str(hub)])
    report = json.loads(out.output)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["Sync target"]["status"] == "fail"
    assert "hub.json is invalid" in by_name["Sync target"]["detail"]
    assert by_name["Host configs"]["status"] == "warn"
    assert by_name["Host configs"]["detail"].startswith("cursor: JSONDecodeError")


async def test_memory_edit_drops_private_spans(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = _core(hub, fake_embed, fake_llm, fake_jev)
    app = create_hub_app(api_key="secret", hub_dir=hub, client=core)
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub", api_key="secret",
            transport=httpx.ASGITransport(app=app),
        ) as remote:
            await remote.write(content="端口是 8080", session_id="s", request_id="e1", wait=True)
            nodes = await core.list(user_id=HUB_USER_ID, agent_id="", limit=10)
            fact = next(n for n in nodes if n.layer != Layer.L0_RAW.value)
            edited = await remote.update_memory(
                fact.memory_id, "端口是 9090 <private>密码 hunter2</private>"
            )
            with pytest.raises(httpx.HTTPStatusError) as empty:
                await remote.update_memory(edited["memory_id"], "<private>x</private>")
    assert core.factory.vector.get(edited["memory_id"]).content == "端口是 9090"
    assert empty.value.response.status_code == 400
    await core.aclose()


def test_block_content_and_dict_tool_args_are_sanitized() -> None:
    key = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    out = sanitize_messages(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"<private>hide</private>用 {key}"}],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "1", "function": {"name": "sh", "arguments": {"cmd": f"export K={key}"}}}
                ],
            },
        ],
        redact=True,
    )
    assert out[0]["content"] == [{"type": "text", "text": "用 <redacted type='openai_key'/>"}]
    assert out[1]["tool_calls"][0]["function"]["arguments"] == {
        "cmd": "export K=<redacted type='openai_key'/>"
    }


def test_unclosed_pem_headers_redact_in_linear_time() -> None:
    header = "-----BEGIN PRIVATE KEY-----\n"
    start = time.perf_counter()
    redact_secrets(header * 4000)
    small = time.perf_counter() - start
    start = time.perf_counter()
    redact_secrets(header * 16000)
    large = time.perf_counter() - start
    # Quadratic would be 16x; allow generous noise above linear (4x).
    assert large < max(small, 0.01) * 8
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    assert redact_secrets(f"{header}a {block} b") == (
        f"{header}a <redacted type='private_key_pem'/> b"
    )


def test_inline_turn_survives_an_unreachable_core(tmp_path: Path, monkeypatch) -> None:
    written: list[dict] = []
    state = {"down": True}

    class Remote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **_kw):
            return {"memories": {}, "conversations": []}

        async def write(self, **kwargs):
            if state["down"]:
                raise httpx.ConnectError("refused")
            written.append(kwargs)
            return {"status": "pending"}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: Remote())

    def hook(**payload: Any):
        return runner.invoke(
            cli_main.app, ["hook", "trae", "--hub-dir", str(tmp_path)],
            input=json.dumps({"session_id": "t-1", "cwd": "/w/app", **payload}),
        )

    hook(hook_event_name="UserPromptSubmit", prompt="超时多少?")
    hook(hook_event_name="Stop", last_assistant_message="7 秒")
    assert written == []
    state["down"] = False
    hook(hook_event_name="Stop", last_assistant_message="7 秒")
    assert [w["messages"][0]["content"] for w in written] == ["超时多少?"]

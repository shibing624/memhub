# -*- coding: utf-8 -*-
"""MemHub Core, transcript intake and host wiring tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
import zstandard
from typer.testing import CliRunner

from memhub import MemoryClient
from memhub.cli import hub as cli_hub
from memhub.cli import main as cli_main
from memhub.coding.preproc import extract_files, has_any_tool_message
from memhub.config import Settings
from memhub.hub import (
    HOSTS,
    CursorStore,
    HubClient,
    build_turns,
    default_source_roots,
    hosts_status,
    install_host,
    scan_file,
    scan_sources,
    uninstall_host,
)
from memhub.hub.hosts import TRAE_MCP
from memhub.hub.queue import HubWriteQueue
from memhub.hub.server import (
    HUB_USER_ID,
    create_hub_app,
    last_digest_due,
    read_last_digest_at,
    write_last_digest_at,
)
from memhub.hub.sources import (
    SOURCE_SPECS,
    _slug,
    _workspace_of_slug,
    locate_transcript,
)
from memhub.hub.config import format_time, publish_runtime, resolve_hub_config
from memhub.types import Layer

runner = CliRunner()


def _memory_client(
    root: Path,
    fake_embed,
    fake_llm,
    fake_jev,
) -> MemoryClient:
    settings = Settings(storage_dir=str(root / "store"), mode="system1")
    return MemoryClient(
        settings=settings,
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )


async def _drain(app) -> None:
    """Wait for the Core's writer to finish the jobs already accepted."""
    while app.state.queue.counts().get("pending", 0) or app.state.queue.counts().get(
        "running", 0
    ):
        await asyncio.sleep(0.01)


@asynccontextmanager
async def _attached_core(root: Path, memory_client: MemoryClient):
    app = create_hub_app(api_key="secret", hub_dir=root, client=memory_client)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(
            base_url="http://127.0.0.1:8765",
            api_key="secret",
            transport=transport,
        ) as remote:
            yield app, remote


def _jsonl(path: Path, records: list[dict], *, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _zstd_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_bytes(zstandard.ZstdCompressor().compress(text.encode()))


def _dsh_events(
    session_id: str, cwd: str, *, origin: str | None = None, prompt: str = "我偏好用 pnpm"
) -> list[dict]:
    header = {"type": "session", "version": 4, "id": session_id, "createdAt": 1791004000000, "cwd": cwd}
    if origin:
        header["origin"] = origin
    return [
        header,
        {"type": "turn/start", "seq": 1, "time": 1791004001000, "data": {"turn": 1}},
        {
            "type": "user/message",
            "seq": 2,
            "time": 1791004001000,
            "data": {
                "content": [{"type": "text", "text": prompt}],
                "source": {"kind": "user"},
                "role": "user",
                "id": "u1",
            },
        },
        {
            "type": "assistant/message",
            "seq": 3,
            "time": 1791004002000,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "thinking about pnpm"},
                        {"type": "text", "text": "先查一下包管理器"},
                    ],
                },
            },
        },
        {
            "type": "tool/call",
            "seq": 4,
            "time": 1791004003000,
            "data": {"turn": 1, "step": 1, "callId": "c1", "name": "read", "arguments": "{\"path\": \"package.json\"}"},
        },
        {"type": "tool/result", "seq": 5, "time": 1791004004000, "data": {"callId": "c1", "output": "secret tool output"}},
        {
            "type": "assistant/message",
            "seq": 6,
            "time": 1791004005000,
            "data": {
                "turn": 1,
                "step": 2,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "好的，记下 pnpm"}]},
            },
        },
        {"type": "turn/end", "seq": 7, "time": 1791004006000, "data": {"turn": 1}},
    ]


def _hermes_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
        "active INTEGER DEFAULT 1, _compressed_summary INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _opencode_db(
    path: Path,
    sessions: list[tuple[str, str, str | None]],
    messages: list[tuple[str, str, int, dict]],
    parts: list[tuple[str, str, str, int, dict]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL, parent_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "time_created INTEGER NOT NULL, data TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, time_created INTEGER NOT NULL, data TEXT NOT NULL)"
    )
    conn.executemany("INSERT INTO session VALUES (?,?,?)", sessions)
    conn.executemany(
        "INSERT INTO message VALUES (?,?,?,?)",
        [(mid, sid, ts, json.dumps(data, ensure_ascii=False)) for mid, sid, ts, data in messages],
    )
    conn.executemany(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        [
            (pid, mid, sid, ts, json.dumps(data, ensure_ascii=False))
            for pid, mid, sid, ts, data in parts
        ],
    )
    conn.commit()
    conn.close()


def _seed_transcripts(home: Path) -> dict[str, Path]:
    roots = default_source_roots(home)
    _jsonl(
        roots["agentica"] / "default" / "proj" / "ag-1.jsonl",
        [
            {
                "type": "user",
                "session_id": "ag-1",
                "timestamp": "2026-10-01T10:00:00Z",
                "content": "我在研究强化学习 "
                "<relevant-memories>old echo</relevant-memories>",
            },
            {"type": "assistant", "session_id": "ag-1", "content": "中间步骤"},
            {"type": "tool", "content": "search result"},
            {
                "type": "assistant",
                "session_id": "ag-1",
                "content": "推荐先读 Sutton 的书",
            },
        ],
    )
    _jsonl(
        roots["claude-code"] / "proj" / "cc-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "cc-1",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<system-reminder>x</system-reminder>我常用 Python 3.12",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "sessionId": "cc-1",
                "message": {"content": [{"type": "text", "text": "好的"}]},
            },
        ],
    )
    _jsonl(
        roots["codebuddy"] / "proj" / "cb-1.jsonl",
        [
            {
                "type": "message",
                "role": "user",
                "sessionId": "cb-1",
                "timestamp": 1791001000123,
                "cwd": "/Users/me/cb",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<system-reminder>ctx</system-reminder>我习惯用 uv 管理虚拟环境",
                    }
                ],
            },
            {
                "type": "reasoning",
                "sessionId": "cb-1",
                "rawContent": [{"type": "reasoning_text", "text": "thinking"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "sessionId": "cb-1",
                "timestamp": 1791001002000,
                "cwd": "/Users/me/cb",
                "content": [{"type": "output_text", "text": "已记录 uv 偏好"}],
            },
        ],
    )
    _jsonl(
        roots["codex"] / "2026" / "10" / "03" / "rollout-x.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "cx-1"}},
            {
                "type": "event_msg",
                "payload": {
                "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "我的网站是 mulanai.com"}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": "记住了"}],
                    },
                },
            },
        ],
    )
    _jsonl(
        roots["cursor"] / "proj" / "agent-transcripts" / "cu-1" / "cu-1.jsonl",
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<user_query>喜欢简洁的代码</user_query>",
                        }
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "明白"}]},
            },
        ],
    )
    _hermes_db(
        roots["hermes"],
        [
            ("he-1", "user", "周末喜欢爬山", 1791000000.0),
            ("he-1", "assistant", "不错", 1791000001.0),
        ],
    )
    _opencode_db(
        roots["opencode"],
        [
            ("oc-1", "/Users/me/oc", None),
            ("oc-sub", "/Users/me/oc", "oc-1"),
        ],
        [
            ("m-u1", "oc-1", 1791002000000, {"role": "user"}),
            ("m-a1", "oc-1", 1791002001000, {"role": "assistant"}),
            ("m-sub", "oc-sub", 1791002002000, {"role": "user"}),
        ],
        [
            (
                "p-1",
                "m-u1",
                "oc-1",
                1791002000000,
                {"type": "text", "text": "我喜欢用ruff做lint"},
            ),
            (
                "p-2",
                "m-a1",
                "oc-1",
                1791002001000,
                {"type": "text", "text": "收到"},
            ),
            (
                "p-sub",
                "m-sub",
                "oc-sub",
                1791002002000,
                {"type": "text", "text": "subagent ignored"},
            ),
        ],
    )
    _jsonl(
        roots["workbuddy"] / "proj" / "wb-1.jsonl",
        [
            {"type": "session-meta", "sessionId": "wb-1", "timestamp": 1791003000000},
            {
                "type": "message",
                "role": "user",
                "sessionId": "wb-1",
                "timestamp": 1791003001000,
                "cwd": "/Users/me/wb",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<system-reminder>SOUL.md</system-reminder>\n"
                        "<user_query>默认用中文写技术方案</user_query>",
                    }
                ],
            },
            {
                "type": "message",
                "role": "assistant",
                "sessionId": "wb-1",
                "timestamp": 1791003002000,
                "cwd": "/Users/me/wb",
                "content": [{"type": "output_text", "text": "好的，按中文输出"}],
            },
        ],
    )
    _zstd_jsonl(
        roots["dsh"] / "--Users-me-ds--" / "session-ds-1" / "session.v4.jsonl.zstd",
        _dsh_events("session-ds-1", "/Users/me/ds"),
    )
    kimi_dir = hashlib.md5(b"/Users/me/kimi").hexdigest()
    (roots["kimi-cli"].parent).mkdir(parents=True, exist_ok=True)
    (roots["kimi-cli"].parent / "kimi.json").write_text(
        json.dumps({"work_dirs": [{"path": "/Users/me/kimi", "kaos": "local"}]})
    )
    kimi_session = roots["kimi-cli"] / kimi_dir / "kimi-1"
    _jsonl(
        kimi_session / "context.jsonl",
        [
            {"role": "_checkpoint", "id": 0},
            {"role": "user", "content": "我习惯用 just 管理任务"},
            {"role": "_usage", "token_count": 5},
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "internal reasoning", "encrypted": None},
                    {"type": "text", "text": "收到，用 just"},
                ],
                "tool_calls": [],
            },
            {"role": "tool", "content": [{"type": "text", "text": "tool output"}], "tool_call_id": "t"},
        ],
    )
    (kimi_session / "metadata.json").write_text(json.dumps({"wire_mtime": 1791005000.5}))
    pi_header = {
        "type": "session",
        "version": 3,
        "id": "pi-1",
        "timestamp": "2026-11-08T10:00:00.000Z",
        "cwd": "/Users/me/pi",
    }

    def pi_message(role: str, content: list[dict] | str, ms: int, **extra: Any) -> dict:
        return {
            "type": "message",
            "id": "e1",
            "parentId": None,
            "timestamp": "2026-11-08T10:00:01.000Z",
            "message": {"role": role, "content": content, "timestamp": ms, **extra},
        }

    pi_records = [
        pi_header,
        {"type": "model_change", "id": "m1", "parentId": None, "provider": "venus", "modelId": "x"},
        pi_message("user", [{"type": "text", "text": "我的包管理器是 uv"}], 1794144606000),
        pi_message(
            "assistant",
            [
                {"type": "thinking", "thinking": "pi hidden reasoning"},
                {
                    "type": "toolCall",
                    "id": "t1",
                    "name": "write",
                    "arguments": {"path": "/Users/me/pi/a.py", "content": "SECRET FILE BODY"},
                },
                {
                    "type": "toolCall",
                    "id": "t2",
                    "name": "edit",
                    "arguments": {
                        "path": "/Users/me/pi/a.py",
                        "oldText": "OLD BODY",
                        "newText": "NEW BODY",
                    },
                },
            ],
            1794144607000,
        ),
        pi_message(
            "toolResult",
            [{"type": "text", "text": "pi secret tool output"}],
            1794144608000,
            toolCallId="t1",
            toolName="write",
            isError=False,
        ),
        {
            "type": "message",
            "id": "b1",
            "parentId": None,
            "timestamp": "2026-11-08T10:00:02.000Z",
            "message": {
                "role": "bashExecution",
                "command": "ls",
                "output": "pi bash output",
                "exitCode": 0,
            },
        },
        {"type": "custom_message", "id": "c1", "customType": "ext", "content": "injected extension context"},
        pi_message("assistant", [{"type": "text", "text": "记下了，用 uv"}], 1794144609000),
    ]
    _jsonl(roots["pi"] / "--Users-me-pi--" / "2026-11-08T10-00-00-000Z_pi-1.jsonl", pi_records)
    _jsonl(
        roots["openclaw"] / "main" / "sessions" / "oc-1.jsonl",
        [{**pi_header, "id": "oc-1", "cwd": "/Users/me/oc"}, *pi_records[1:]],
    )
    _jsonl(
        roots["qoder"] / "-Users-me-qo" / "qo-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "qo-1",
                "cwd": "/Users/me/qo",
                "gitBranch": "main",
                "timestamp": "2026-11-08T11:00:00Z",
                "message": {"content": [{"type": "text", "text": "单元测试用 pytest -q"}]},
            },
            {
                "type": "assistant",
                "sessionId": "qo-1",
                "cwd": "/Users/me/qo",
                "gitBranch": "main",
                "timestamp": "2026-11-08T11:00:02Z",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "qoder hidden thought"},
                        {
                            "type": "tool_use",
                            "id": "qt-1",
                            "name": "Read",
                            "input": {"file_path": "/Users/me/qo/pyproject.toml"},
                        },
                        {"type": "text", "text": "收到，用 pytest -q"},
                    ]
                },
            },
        ],
    )
    _opencode_db(
        roots["zcode"],
        [("zc-1", "/Users/me/zc", None)],
        [
            ("zm-u1", "zc-1", 1791007000000, {"role": "user"}),
            ("zm-a1", "zc-1", 1791007001000, {"role": "assistant"}),
        ],
        [
            (
                "zp-1",
                "zm-u1",
                "zc-1",
                1791007000000,
                {"type": "text", "text": "构建用 cargo check"},
            ),
            (
                "zp-t1",
                "zm-a1",
                "zc-1",
                1791007000500,
                {
                    "type": "tool",
                    "callID": "call-zc-1",
                    "tool": "Write",
                    "state": {
                        "status": "completed",
                        "input": {"file_path": "/Users/me/zc/src/lib.rs", "content": "ZC SECRET"},
                    },
                },
            ),
            (
                "zp-2",
                "zm-a1",
                "zc-1",
                1791007001000,
                {"type": "text", "text": "记下 cargo check"},
            ),
        ],
    )
    _jsonl(
        roots["copilot"] / "cp-1" / "events.jsonl",
        [
            {
                "type": "session.start",
                "timestamp": "2026-11-08T12:00:00Z",
                "data": {
                    "sessionId": "cp-1",
                    "context": {"cwd": "/Users/me/cp", "branch": "feat/cp"},
                },
            },
            {
                "type": "user.message",
                "timestamp": "2026-11-08T12:00:01Z",
                "data": {"content": "system injection", "source": "system"},
            },
            {
                "type": "user.message",
                "timestamp": "2026-11-08T12:00:02Z",
                "data": {"content": "数据库迁移用 alembic"},
            },
            {
                "type": "tool.execution_start",
                "timestamp": "2026-11-08T12:00:03Z",
                "data": {
                    "toolCallId": "cp-t1",
                    "toolName": "create",
                    "arguments": {"path": "/Users/me/cp/alembic.ini", "file_text": "CP SECRET"},
                },
            },
            {
                "type": "assistant.message",
                "timestamp": "2026-11-08T12:00:04Z",
                "ephemeral": True,
                "data": {"content": "streaming delta ignored"},
            },
            {
                "type": "assistant.message",
                "timestamp": "2026-11-08T12:00:05Z",
                "data": {"content": "好的，用 alembic"},
            },
        ],
    )
    kiro_dir = roots["kiro"] / "ws-hash" / "sess_kiro-1"
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / "session.json").write_text(
        json.dumps(
            {
                "id": "sess_kiro-1",
                "workspacePaths": ["/Users/me/kiro"],
                "createdAt": "2026-11-08T13:00:00Z",
            }
        )
    )
    _jsonl(
        kiro_dir / "messages.jsonl",
        [
            {
                "timestamp": "2026-11-08T13:00:01Z",
                "_meta": {"kiro": {"syntheticUserMessageReason": "agent-initiated-prompt"}},
                "payload": {"type": "user", "content": "workflow notice ignored"},
            },
            {
                "timestamp": "2026-11-08T13:00:02Z",
                "payload": {"type": "user", "content": "AWS 区域默认用 us-west-2"},
            },
            {
                "timestamp": "2026-11-08T13:00:03Z",
                "payload": {
                    "type": "tool_call",
                    "id": "kr-t1",
                    "name": "fs_write",
                    "args": {"path": "/Users/me/kiro/infra.tf", "text": "KIRO SECRET"},
                },
            },
            {
                "timestamp": "2026-11-08T13:00:04Z",
                "payload": {"type": "assistant", "content": "记下了，us-west-2"},
            },
        ],
    )
    cline_dir = roots["cline"] / "cl-1"
    cline_dir.mkdir(parents=True, exist_ok=True)
    (cline_dir / "cl-1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "cl-1",
                "cwd": "/Users/me/cline",
                "started_at": "2026-11-08T14:00:00Z",
            }
        )
    )
    (cline_dir / "cl-1.messages.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agent": "lead",
                "sessionId": "cl-1",
                "updated_at": "2026-11-08T14:00:05Z",
                "messages": [
                    {
                        "role": "user",
                        "ts": 1794146401000,
                        "metadata": {"displayRole": "system"},
                        "content": "injected hook context",
                    },
                    {
                        "role": "user",
                        "ts": 1794146402000,
                        "content": "前端状态管理用 zustand",
                    },
                    {
                        "role": "assistant",
                        "ts": 1794146403000,
                        "content": [
                            {"type": "thinking", "thinking": "cline hidden reasoning"},
                            {
                                "type": "tool_use",
                                "id": "cl-t1",
                                "name": "write_to_file",
                                "input": {
                                    "path": "/Users/me/cline/store.ts",
                                    "content": "CLINE SECRET",
                                },
                            },
                            {"type": "text", "text": "好的，用 zustand"},
                        ],
                    },
                ],
            }
        )
    )
    kc_session = roots["kimi-code"] / "wd_me_1" / "session_kc-1"
    (kc_session).mkdir(parents=True)
    (kc_session / "state.json").write_text(json.dumps({"id": "session_kc-1", "cwd": "/Users/me/kc"}))

    def loop(event: dict, ms: int) -> dict:
        return {"type": "context.append_loop_event", "agentId": "main", "event": event, "time": ms}

    def message(text: str, origin: str, ms: int) -> dict:
        return {
            "type": "context.append_message",
            "agentId": "main",
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "toolCalls": [], "origin": {"kind": origin}},
            "time": ms,
        }

    _jsonl(
        kc_session / "agents" / "main" / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1791006000000},
            message("我的部署平台是 fly.io", "user", 1791006001000),
            message("<system-reminder>date</system-reminder>", "injection", 1791006001001),
            loop({"type": "step.begin", "step": 1}, 1791006002000),
            loop({"type": "content.part", "step": 1, "part": {"type": "think", "think": "hidden"}}, 1791006002001),
            loop({"type": "tool.call", "step": 1, "toolCallId": "k1", "name": "Bash", "args": {"command": "ls"}}, 1791006002002),
            loop({"type": "tool.result", "toolCallId": "k1", "result": {"output": "secret listing"}}, 1791006002003),
            loop({"type": "step.end", "step": 1, "finishReason": "tool_use"}, 1791006002004),
            loop({"type": "step.begin", "step": 2}, 1791006003000),
            loop({"type": "content.part", "step": 2, "part": {"type": "text", "text": "记下了，"}}, 1791006003001),
            loop({"type": "content.part", "step": 2, "part": {"type": "text", "text": "用 fly.io"}}, 1791006003002),
            loop({"type": "step.end", "step": 2, "finishReason": "stop"}, 1791006003003),
        ],
    )
    return roots


def test_readers_keep_only_real_dialogue(tmp_path: Path) -> None:
    roots = _seed_transcripts(tmp_path)
    turns = {}
    for name, spec in SOURCE_SPECS.items():
        (path,) = spec.discover(roots[name])
        messages, _ = spec.read(path, 0)
        done, open_turn = build_turns(messages)
        turns[name] = [*done, open_turn]
    assert turns["agentica"][0].assistant == "推荐先读 Sutton 的书"
    assert turns["agentica"][0].user == "我在研究强化学习"
    assert turns["claude-code"][0].user == "我常用 Python 3.12"
    assert turns["codebuddy"][0].user == "我习惯用 uv 管理虚拟环境"
    assert turns["codebuddy"][0].at == 1791001000
    assert turns["codex"][0].session_id == "cx-1"
    assert turns["cursor"][0].user == "喜欢简洁的代码"
    assert turns["hermes"][0].at == 1791000000
    assert len(turns["opencode"]) == 1 and turns["opencode"][0].user == "我喜欢用ruff做lint"
    assert turns["opencode"][0].workspace == "/Users/me/oc"
    assert turns["workbuddy"][0].user == "默认用中文写技术方案"
    assert turns["workbuddy"][0].workspace == "/Users/me/wb"
    # dsh: reasoning blocks and tool output are not kept; the tool call is a step.
    dsh = turns["dsh"][0]
    assert (dsh.user, dsh.assistant) == ("我偏好用 pnpm", "好的，记下 pnpm")
    assert (dsh.session_id, dsh.workspace, dsh.at) == ("session-ds-1", "/Users/me/ds", 1791004001)
    assert [s["tool_calls"][0]["function"]["name"] for s in dsh.steps] == ["read"]
    assert "secret tool output" not in json.dumps(dsh.messages(), ensure_ascii=False)
    # kimi-cli: the project comes from md5(work dir), the time from the session's last write.
    kimi = turns["kimi-cli"][0]
    assert (kimi.user, kimi.assistant) == ("我习惯用 just 管理任务", "收到，用 just")
    assert (kimi.session_id, kimi.workspace, kimi.at) == ("kimi-1", "/Users/me/kimi", 1791005000)
    assert "internal reasoning" not in json.dumps(kimi.messages(), ensure_ascii=False)
    # kimi-code: streamed text parts join into the step's answer; injected reminders,
    # reasoning and tool results are not conversation.
    kc = turns["kimi-code"][0]
    assert (kc.user, kc.assistant) == ("我的部署平台是 fly.io", "记下了，用 fly.io")
    assert (kc.session_id, kc.workspace, kc.at) == ("session_kc-1", "/Users/me/kc", 1791006001)
    assert [s["tool_calls"][0]["function"]["name"] for s in kc.steps] == ["Bash"]
    dumped = json.dumps(kc.messages(), ensure_ascii=False)
    assert "hidden" not in dumped and "secret listing" not in dumped and "date" not in dumped
    for source, session, workspace in (
        ("pi", "pi-1", "/Users/me/pi"),
        ("openclaw", "oc-1", "/Users/me/oc"),
    ):
        pi_turn = turns[source][0]
        assert (pi_turn.user, pi_turn.assistant) == ("我的包管理器是 uv", "记下了，用 uv")
        assert (pi_turn.session_id, pi_turn.workspace, pi_turn.at) == (
            session,
            workspace,
            1794144606,
        )
        assert [s["tool_calls"][0]["function"]["name"] for s in pi_turn.steps] == ["write", "edit"]
        dumped_pi = json.dumps(pi_turn.messages(), ensure_ascii=False)
        for leaked in (
            "pi hidden reasoning",
            "SECRET FILE BODY",
            "OLD BODY",
            "NEW BODY",
            "pi secret tool output",
            "pi bash output",
            "injected extension context",
        ):
            assert leaked not in dumped_pi, leaked
        assert "/Users/me/pi/a.py" in dumped_pi
    qo = turns["qoder"][0]
    assert (qo.user, qo.assistant) == ("单元测试用 pytest -q", "收到，用 pytest -q")
    assert (qo.session_id, qo.workspace, qo.branch) == ("qo-1", "/Users/me/qo", "main")
    assert [s["tool_calls"][0]["function"]["name"] for s in qo.steps] == ["Read"]
    assert "qoder hidden thought" not in json.dumps(qo.messages(), ensure_ascii=False)
    zc = turns["zcode"][0]
    assert (zc.user, zc.assistant) == ("构建用 cargo check", "记下 cargo check")
    assert (zc.session_id, zc.workspace, zc.at) == ("zc-1", "/Users/me/zc", 1791007000)
    dumped_zc = json.dumps(zc.messages(), ensure_ascii=False)
    assert "/Users/me/zc/src/lib.rs" in dumped_zc and "ZC SECRET" not in dumped_zc
    cp = turns["copilot"][0]
    assert (cp.user, cp.assistant) == ("数据库迁移用 alembic", "好的，用 alembic")
    assert (cp.session_id, cp.workspace, cp.branch) == ("cp-1", "/Users/me/cp", "feat/cp")
    dumped_cp = json.dumps(cp.messages(), ensure_ascii=False)
    assert "/Users/me/cp/alembic.ini" in dumped_cp
    assert "CP SECRET" not in dumped_cp and "system injection" not in dumped_cp
    assert "streaming delta ignored" not in dumped_cp
    kr = turns["kiro"][0]
    assert (kr.user, kr.assistant) == ("AWS 区域默认用 us-west-2", "记下了，us-west-2")
    assert (kr.session_id, kr.workspace) == ("sess_kiro-1", "/Users/me/kiro")
    dumped_kr = json.dumps(kr.messages(), ensure_ascii=False)
    assert "/Users/me/kiro/infra.tf" in dumped_kr
    assert "KIRO SECRET" not in dumped_kr and "workflow notice ignored" not in dumped_kr
    cl = turns["cline"][0]
    assert (cl.user, cl.assistant) == ("前端状态管理用 zustand", "好的，用 zustand")
    assert (cl.session_id, cl.workspace, cl.at) == ("cl-1", "/Users/me/cline", 1794146402)
    dumped_cl = json.dumps(cl.messages(), ensure_ascii=False)
    assert "/Users/me/cline/store.ts" in dumped_cl
    assert "CLINE SECRET" not in dumped_cl and "cline hidden reasoning" not in dumped_cl
    assert "injected hook context" not in dumped_cl


def test_build_turns_merges_users_and_drops_orphan_answer() -> None:
    from memhub.hub.sources import Message

    messages = [
        Message("assistant", "orphan", "s", None, 0, 1),
        Message("user", "a", "s", None, 1, 2),
        Message("user", "b", "s", None, 2, 3),
        Message("assistant", "answer", "s", None, 3, 4),
        Message("user", "open", "s", None, 4, 5),
    ]
    done, open_turn = build_turns(messages)
    assert [(turn.user, turn.assistant) for turn in done] == [("a\nb", "answer")]
    assert open_turn is not None and open_turn.user == "open"


async def test_core_auth_durable_queue_and_single_scope(
    tmp_path: Path,
    fake_embed,
    fake_llm,
    fake_jev,
) -> None:
    core = _memory_client(tmp_path, fake_embed, fake_llm, fake_jev)
    async with _attached_core(tmp_path, core) as (app, remote):
        discovered = resolve_hub_config(tmp_path)
        assert discovered.base_url == "http://127.0.0.1:8765"
        assert discovered.api_key == "secret"
        identity = await remote.identity()
        assert identity["scope"] == {
            "app_id": "default",
            "user_id": HUB_USER_ID,
            "agent_id": "",
        }
        job = await remote.write(
            messages=[
                {"role": "user", "content": "我偏好 PyTorch"},
                {"role": "assistant", "content": "记住了"},
            ],
            session_id="codex:s1",
            request_id="memhub:one",
            wait=True,
        )
        assert job["status"] == "done"
        same = await remote.write(
            messages=[
                {"role": "user", "content": "我偏好 PyTorch"},
                {"role": "assistant", "content": "记住了"},
            ],
            session_id="codex:s1",
            request_id="memhub:one",
            wait=True,
        )
        assert same["result"]["memory_id"] == job["result"]["memory_id"]
        assert (tmp_path / "hub.db").is_file()

        direct = httpx.AsyncClient(
            base_url="http://hub",
            transport=httpx.ASGITransport(app=app),
        )
        assert (await direct.get("/v1/hub/identity")).status_code == 401
        assert (
            await direct.get(
                "/v1/hub/identity",
                headers={"Authorization": "Bearer wrong"},
            )
        ).status_code == 403
        await direct.aclose()

        recall = await remote.recall(query="PyTorch")
        assert any(
            "PyTorch" in item["content"]
            for group in recall["memories"].values()
            for item in group
        )
        status = await remote.status()
        assert status["memories"]["raw_by_source"] == {"codex": 1}
    assert not (tmp_path / "runtime.json").exists()
    assert oct((tmp_path / "runtime-token").stat().st_mode & 0o777) == "0o600"


async def test_core_serializes_concurrent_writes(tmp_path: Path) -> None:
    class SlowClient:
        def __init__(self) -> None:
            self.settings = Settings(storage_dir=str(tmp_path / "store"))
            self.active = 0
            self.max_active = 0

        async def add(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.03)
            self.active -= 1
            return SimpleNamespace(
                to_dict=lambda: {
                    "success": True,
                    "memory_id": kwargs["request_id"],
                }
            )

        async def add_raw(self, **kwargs):
            return await self.add(**kwargs)

    slow = SlowClient()
    app = create_hub_app(
        api_key="secret",
        hub_dir=tmp_path,
        client=slow,  # type: ignore[arg-type]
        settings=slow.settings,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(
            base_url="http://hub",
            api_key="secret",
            transport=transport,
        ) as remote:
            await asyncio.gather(
                *[
                    remote.write(content=f"memory {i}", request_id=f"r{i}", wait=True)
                    for i in range(4)
                ]
            )
    assert slow.max_active == 1


async def test_scan_all_hosts_through_core(
    tmp_path: Path,
    fake_embed,
    fake_llm,
    fake_jev,
) -> None:
    hub = tmp_path / "hub"
    roots = _seed_transcripts(tmp_path / "home")
    core = _memory_client(hub, fake_embed, fake_llm, fake_jev)
    async with _attached_core(hub, core) as (_app, remote):
        summary = await scan_sources(
            remote,
            hub_dir=hub,
            roots=roots,
            idle_seconds=0,
        )
        assert {key: value["turns"] for key, value in summary.items()} == dict.fromkeys(
            SOURCE_SPECS,
            1,
        )
        again = await scan_sources(
            remote,
            hub_dir=hub,
            roots=roots,
            idle_seconds=0,
        )
        assert all(value["turns"] == 0 for value in again.values())
        await _drain(_app)
        nodes = await core.list(
            user_id=HUB_USER_ID,
            agent_id="",
            limit=100,
            include_history=True,
        )
        assert {(node.layer, node.session_id.partition(":")[0]) for node in nodes if node.layer == Layer.L0_RAW.value} == {
            (Layer.L0_RAW.value, source) for source in SOURCE_SPECS
        }


async def test_open_turn_cursor_and_raw_idempotency(
    tmp_path: Path,
    fake_embed,
    fake_llm,
    fake_jev,
) -> None:
    core = _memory_client(tmp_path, fake_embed, fake_llm, fake_jev)
    path = tmp_path / "ag.jsonl"
    _jsonl(
        path,
        [
        {"type": "user", "session_id": "s", "content": "第一问"},
        {"type": "assistant", "session_id": "s", "content": "第一答"},
        {"type": "user", "session_id": "s", "content": "第二问"},
            {"type": "assistant", "session_id": "s", "content": "中间步骤"},
        ],
    )
    async with _attached_core(tmp_path, core) as (_app, remote):
        with CursorStore(tmp_path) as cursors:
            kwargs = {
                "client": remote,
                "source": "agentica",
                "path": path,
                "cursors": cursors,
            }
            assert await scan_file(idle_seconds=3600, raw_only=True, **kwargs) == 1
            _jsonl(
                path,
                [{"type": "assistant", "session_id": "s", "content": "最终答案"}],
                mode="a",
            )
            assert await scan_file(complete=True, raw_only=True, **kwargs) == 1
            assert await scan_file(complete=True, raw_only=True, **kwargs) == 0
        first = await core.add_raw(
            content="固定原文",
            user_id=HUB_USER_ID,
            request_id="raw-idempotent",
        )
        second = await core.add_raw(
            content="固定原文",
            user_id=HUB_USER_ID,
            request_id="raw-idempotent",
        )
        assert first.memory_id == second.memory_id


def test_install_uninstall_hosts_use_local_proxy(tmp_path: Path, monkeypatch) -> None:
    from agentica.shell_hooks.config import load_shell_hooks_config

    hub = tmp_path / "hub"
    home = tmp_path / "home"
    (home / ".agentica").mkdir(parents=True)
    (home / ".agentica" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}})
    )
    (home / ".agentica" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "settings": {
                    "hooks": {
                        "consumers": [
                            {"name": "open-island", "command": ["/bin/oi"]}
                        ]
                    }
                }
            }
        )
    )
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    dsh_before = (
        "# Your patch layer for this dsh profile.\n"
        "- id: ui-chat\n"
        '  name: "@deepseek-ai/dsh-client-ui-chat"\n'
        "  config:\n"
        "    transcriptView: standard\n"
        "    purpose: null\n"
    )
    patch.write_text(dsh_before)
    (home / ".kimi").mkdir()
    kimi_before = (
        'default_model = "k3"\n\n[[hooks]]\nevent = "Stop"\ncommand = "/bin/island"\ntimeout = 45\n'
    )
    (home / ".kimi" / "config.toml").write_text(kimi_before)
    (home / ".trae").mkdir()
    (home / ".trae" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {"Stop": [{"hooks": [{"command": "/bin/island", "type": "command"}]}]},
                "version": 1,
            }
        )
    )

    for host in HOSTS:
        install_host(host, hub_dir=hub, home=home)
    status = hosts_status(home=home)
    assert all(
        item["skill"] and item["mcp"] in (True, None) and item["hooks"] in (True, None)
        for item in status.values()
    )
    assert status["openclaw"] == {"skill": True, "mcp": None, "hooks": None}
    assert status["pi"] == {"skill": True, "mcp": True, "hooks": None}
    assert status["cline"] == {"skill": True, "mcp": True, "hooks": None}
    for host in ("copilot", "kiro", "qoder", "zcode"):
        assert status[host] == {"skill": True, "mcp": True, "hooks": True}
    zcode_cfg = json.loads((home / ".zcode" / "cli" / "config.json").read_text())
    assert zcode_cfg["mcp"]["servers"]["memhub"]["type"] == "stdio"
    assert zcode_cfg["hooks"]["enabled"] is True
    assert set(zcode_cfg["hooks"]["events"]) >= {"UserPromptSubmit", "Stop"}
    qoder_cfg = json.loads((home / ".qoder" / "settings.json").read_text())
    assert "memhub" in qoder_cfg["mcpServers"]
    assert set(qoder_cfg["hooks"]) >= {"UserPromptSubmit", "Stop"}
    copilot_hooks = json.loads((home / ".copilot" / "hooks" / "memhub.json").read_text())
    assert copilot_hooks["version"] == 1 and set(copilot_hooks["hooks"]) == {"UserPromptSubmit", "Stop"}
    kiro_hooks = json.loads((home / ".kiro" / "hooks" / "memhub.json").read_text())
    assert kiro_hooks["version"] == "v1"
    assert [h["trigger"] for h in kiro_hooks["hooks"]] == ["UserPromptSubmit", "Stop"]
    mcp = json.loads(
        (home / ".agentica" / "mcp_config.json").read_text()
    )["mcpServers"]["memhub"]
    assert mcp["args"][:2] == ["-m", "memhub.hub.mcp"]
    assert "env" not in mcp and "secret" not in json.dumps(mcp)
    oc_mcp = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text()
    )["mcp"]["memhub"]
    assert oc_mcp["type"] == "local"
    assert oc_mcp["command"][1:3] == ["-m", "memhub.hub.mcp"]
    oc_plugin = (
        home / ".config" / "opencode" / "plugins" / "memhub-hub.js"
    ).read_text()
    assert "chat.message" in oc_plugin and "hub" in oc_plugin and "opencode" in oc_plugin
    for buddy in (".codebuddy", ".workbuddy"):
        assert "memhub" in json.loads((home / buddy / "mcp.json").read_text())["mcpServers"]
        buddy_hooks = json.loads((home / buddy / "settings.json").read_text())["hooks"]
        assert set(buddy_hooks) >= {"UserPromptSubmit", "Stop"}
    # dsh: our server joins the profile's patch via `- insert:`; the user's entry and comment stay.
    dsh_patch = patch.read_text()
    assert dsh_patch.startswith("# Your patch layer for this dsh profile.\n")
    dsh_items = yaml.safe_load(dsh_patch)
    entries = {item["id"]: item for item in dsh_items if "id" in item}
    assert entries["ui-chat"]["config"] == {"transcriptView": "standard", "purpose": None}
    assert dsh_patch.startswith(dsh_before)  # the user's own text is byte-identical
    inserted = [row for item in dsh_items if "insert" in item for row in item["insert"]]
    assert len(inserted) == 1
    assert inserted[0]["id"] == "mcp-memhub"
    assert inserted[0]["name"] == "@deepseek-ai/dsh-mcp-client"
    server = inserted[0]["config"]
    assert server["serverName"] == "memhub"
    assert server["transport"] == "stdio" and server["args"][:2] == ["-m", "memhub.hub.mcp"]
    # kimi: one marked block at the end; the user's own hook and settings are untouched.
    kimi_text = (home / ".kimi" / "config.toml").read_text()
    assert kimi_text.startswith(kimi_before)
    # kimi-cli ignores what a prompt hook prints, so it only captures (Stop).
    kimi_hooks = tomllib.loads(kimi_text)["hooks"]
    assert [(h["event"], "memhub.cli" in h["command"]) for h in kimi_hooks] == [
        ("Stop", False),
        ("Stop", True),
    ]
    assert json.loads((home / ".kimi" / "mcp.json").read_text())["mcpServers"]["memhub"]
    # kimi-code recalls on the prompt as well.
    kc_hooks = tomllib.loads((home / ".kimi-code" / "config.toml").read_text())["hooks"]
    assert [h["event"] for h in kc_hooks] == ["UserPromptSubmit", "Stop"]
    assert all(h["command"].endswith("hook kimi-code --hub-dir " + str(hub)) for h in kc_hooks)
    assert "memhub" in json.loads((home / ".kimi-code" / "mcp.json").read_text())["mcpServers"]
    # trae: Claude-style hooks beside the existing one; MCP in the editor's user dir.
    trae_hooks = json.loads((home / ".trae" / "hooks.json").read_text())
    assert trae_hooks["version"] == 1
    assert [len(trae_hooks["hooks"][e]) for e in ("UserPromptSubmit", "Stop")] == [1, 2]
    assert "memhub" in json.loads((home / TRAE_MCP).read_text())["mcpServers"]
    skill = (
        home
        / ".claude"
        / "skills"
        / "memhub-recall"
        / "SKILL.md"
    ).read_text()
    assert "不需要也不能传 `user_id`" in skill
    # The fallback commands go through the Core, never the SDK CLI's own store.
    assert "-m memhub.cli recall --hub-dir" in skill
    assert "-m memhub.cli remember --hub-dir" in skill
    # Rendered from the template in the repo; every placeholder is filled.
    assert "{" not in skill.split("---", 2)[2]
    assert "`claude-code`" in skill

    for key in ("ENABLED", "CONSUMERS"):
        monkeypatch.delenv(f"AGENTICA_HOOKS_{key}", raising=False)
    hooks = load_shell_hooks_config(
        yaml.safe_load((home / ".agentica" / "config.yaml").read_text())
    )
    assert [item.name for item in hooks.consumers_for("run.completed")] == [
        "open-island",
        "memhub",
    ]
    # One consumer for recall and capture; the pre-existing one is not drawn
    # into the per-prompt wait.
    recall = hooks.consumers_for("UserPromptSubmit")
    assert [item.name for item in recall] == ["memhub"]
    assert recall[0].command[-4:] == ["hook", "agentica", "--hub-dir", str(hub)]
    assert "memhub" in [item.name for item in hooks.consumers_for("session.ended")]
    for host in HOSTS:
        uninstall_host(host, home=home)
    assert not any(
        value
        for item in hosts_status(home=home).values()
        for value in item.values()
    )
    assert patch.read_text() == dsh_before
    assert (home / ".kimi" / "config.toml").read_text().strip() == kimi_before.strip()


def test_agentica_yaml_install_keeps_comments_and_uninstall_restores_bytes(
    tmp_path: Path,
) -> None:
    hub = tmp_path / "hub"
    home = tmp_path / "home"
    (home / ".agentica").mkdir(parents=True)
    original = (
        "# Agentica configuration - hand-edited.\n"
        "profiles:\n"
        "  main:\n"
        "    model_name: gpt-5.5  # main model\n"
        "    extra_headers:\n"
        '      Routing: "true"\n'
        "settings:\n"
        "  hooks:\n"
        "    enabled: true\n"
        "    # open-island owns this consumer\n"
        "    consumers:\n"
        "      - name: open-island\n"
        "        command:\n"
        '          - "/Applications/Open Island/bin/hooks"\n'
    )
    config = home / ".agentica" / "config.yaml"
    config.write_text(original)

    install_host("agentica", hub_dir=hub, home=home)
    installed = config.read_text()
    for line in (
        "# Agentica configuration - hand-edited.",
        "model_name: gpt-5.5  # main model",
        'Routing: "true"',
        "# open-island owns this consumer",
        '- "/Applications/Open Island/bin/hooks"',
    ):
        assert line in installed
    assert [c["name"] for c in yaml.safe_load(installed)["settings"]["hooks"]["consumers"]] == [
        "open-island",
        "memhub",
    ]

    uninstall_host("agentica", home=home)
    assert config.read_text() == original


def test_cli_capture_hook_and_status(tmp_path: Path, monkeypatch) -> None:
    hub = tmp_path / "hub"
    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **_kwargs):
            return {
                "memories": {
                    "profile": [],
                    "proactive": [],
                    "normal": [
                        {
                            "memory_id": "m1",
                            "content": "我偏好 PyTorch",
                            "category": "fact",
                            "score": 1.0,
                            "tags": [],
                            "session_id": "codex:s1",
                            "layer": "L1_FACT",
                            "kind": "fact",
                            "is_latest": True,
                            "revision": 0,
                            "source_ids": [],
                            "supporting_ids": [],
                            "supersedes": [],
                            "conflicts_with": [],
                        }
                    ],
                    "raw": [],
                    "schema": [],
                    "catalog": [],
                },
                "conversations": [],
            }

        async def status(self):
            return {"instance_id": "core-1", "memories": {}, "writes": {}}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    hook = runner.invoke(
        cli_main.app,
        ["hook", "claude-code", "--hub-dir", str(hub)],
        input=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "我的框架偏好"},
            ensure_ascii=False,
        ),
    )
    assert hook.exit_code == 0
    context = json.loads(hook.output)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("<relevant-memories>") and "PyTorch" in context
    # agentica speaks the same UserPromptSubmit wire as Claude Code / Codex, so
    # the very same command and reply shape serve it — no per-host branch.
    agentica = runner.invoke(
        cli_main.app,
        ["hook", "agentica", "--hub-dir", str(hub)],
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s-1",
                "prompt": "我的框架偏好",
            },
            ensure_ascii=False,
        ),
    )
    assert agentica.exit_code == 0
    reply = json.loads(agentica.output)
    assert reply == json.loads(hook.output)
    assert reply["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "PyTorch" in reply["hookSpecificOutput"]["additionalContext"]
    publish_runtime(base_url="http://127.0.0.1:8765", instance_id="core-1", pid=1, hub_dir=hub)
    status = runner.invoke(
        cli_main.app,
        ["status", "--hub-dir", str(hub)],
    )
    status_data = json.loads(status.output)
    assert status_data["instance_id"] == "core-1"
    assert len(status_data["runtime"]["started_at"]) == 19
    assert status_data["runtime"]["started_at"][4] == "-"

    spawned = []
    monkeypatch.setattr(
        cli_hub,
        "_spawn_detached",
        lambda argv, _log: spawned.append(argv),
    )
    transcript = tmp_path / "turn.jsonl"
    capture = runner.invoke(
        cli_main.app,
        ["hook", "claude-code", "--hub-dir", str(hub)],
        input=json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)}),
    )
    assert capture.exit_code == 0
    assert spawned == [
        [
            sys.executable,
            "-m",
            "memhub.cli",
            "scan",
            "--source",
            "claude-code",
            "--file",
            str(transcript),
            "--complete",
            "--hub-dir",
            str(hub),
        ]
    ]


def test_connect_http_rejects_plaintext_non_loopback(tmp_path: Path) -> None:
    result = runner.invoke(
        cli_main.app,
        ["connect", "http", "http://memory.example.com", "--api-key", "secret",
         "--hub-dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "HTTPS" in result.output
    assert not (tmp_path / "hub.json").exists()


async def test_failed_write_is_retried_and_cursor_never_skips_it(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    class FlakyWrite:
        """Model unavailable at first, then healthy."""

        def __init__(self, core: MemoryClient) -> None:
            self.core = core
            self.settings = core.settings
            self.factory = core.factory
            self.down = True
            self.calls = 0

        async def add(self, **kwargs):
            self.calls += 1
            if self.down:
                raise RuntimeError("429 rate limited")
            return await self.core.add(**kwargs)

        async def add_raw(self, **kwargs):
            return await self.core.add_raw(**kwargs)

    core = _memory_client(tmp_path / "hub", fake_embed, fake_llm, fake_jev)
    flaky = FlakyWrite(core)
    hub = tmp_path / "hub"
    app = create_hub_app(
        api_key="secret",
        hub_dir=hub,
        client=flaky,  # type: ignore[arg-type]
        settings=core.settings,
        queue_retry_seconds=0.01,
    )
    path = tmp_path / "ag.jsonl"
    _jsonl(
        path,
        [
            {"type": "user", "session_id": "s", "content": "我喜欢 Rust"},
            {"type": "assistant", "session_id": "s", "content": "好"},
        ],
    )
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub",
            api_key="secret",
            transport=httpx.ASGITransport(app=app),
        ) as remote:
            with CursorStore(hub) as cursors:
                kwargs = {
                    "client": remote,
                    "source": "agentica",
                    "path": path,
                    "cursors": cursors,
                    "complete": True,
                }
                assert await scan_file(**kwargs) == 1
                # The evidence is durable and queued while the model is down.
                assert app.state.queue.counts()["pending"] == 1
                while flaky.calls == 0:
                    await asyncio.sleep(0.01)
                assert app.state.queue.counts().get("done", 0) == 0
                flaky.down = False
                for _ in range(500):
                    if app.state.queue.counts().get("done"):
                        break
                    await asyncio.sleep(0.01)
                assert app.state.queue.counts().get("done") == 1
                assert app.state.queue.counts().get("failed", 0) == 0
                assert flaky.calls >= 2
                assert await scan_file(**kwargs) == 0
            # The retry reused the same L0 node instead of storing a second copy.
            raws = [
                n
                for n in await core.list(user_id=HUB_USER_ID, limit=50, include_history=True)
                if n.layer == Layer.L0_RAW.value
            ]
            assert len(raws) == 1


async def test_write_gives_up_after_max_attempts_then_retry_requeues(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    calls = []

    async def broken(_payload):
        calls.append(1)
        raise RuntimeError("400 bad model response")

    queue = HubWriteQueue(
        tmp_path / "hub.db", broken, max_attempts=2, retry_base_seconds=0.01
    )
    queue.start()
    queue.submit({"content": "x", "messages": None, "session_id": "", "memory_at": None,
                  "request_id": "r1", "raw_only": False})
    for _ in range(500):
        if queue.get("r1")["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    job = queue.get("r1")
    assert (job["status"], job["attempts"]) == ("failed", 2)
    assert "400 bad model response" in job["error"]
    assert len(calls) == 2

    # Resubmitting the same request puts it back in the queue instead of returning the failure.
    queue.submit({"content": "x", "messages": None, "session_id": "", "memory_at": None,
                  "request_id": "r1", "raw_only": False})
    assert queue.get("r1")["status"] in ("pending", "running")
    await queue.close()


async def test_raw_only_scan_can_be_extracted_later(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = _memory_client(hub, fake_embed, fake_llm, fake_jev)
    async with _attached_core(hub, core) as (app, remote):
        path = tmp_path / "ag.jsonl"
        _jsonl(
            path,
            [
                {"type": "user", "session_id": "s", "content": "我常用 PyTorch 训练"},
                {"type": "assistant", "session_id": "s", "content": "记下了"},
            ],
        )
        with CursorStore(hub) as cursors:
            assert await scan_file(
                remote,
                source="agentica",
                path=path,
                cursors=cursors,
                complete=True,
                raw_only=True,
            ) == 1
        await _drain(app)
        assert app.state.queue.counts()["raw_only_done"] == 1
        assert await core.list(user_id=HUB_USER_ID, limit=50) != []
        raw_ids = {
            node.memory_id
            for node in await core.list(user_id=HUB_USER_ID, limit=50, include_history=True)
            if node.layer == Layer.L0_RAW.value
        }
        assert app.state.queue.counts()["done"] == 1

        assert (await remote.extract_raw())["queued"] == 1
        await _drain(app)
        assert app.state.queue.counts()["raw_only_done"] == 0
        extracted = [
            node
            for node in await core.list(user_id=HUB_USER_ID, limit=50, include_history=True)
            if node.layer != Layer.L0_RAW.value
        ]
        assert extracted, "extraction created facts from the raw evidence"
        # Extraction reused the stored L0 rather than writing a second copy.
        assert {
            node.memory_id
            for node in await core.list(user_id=HUB_USER_ID, limit=50, include_history=True)
            if node.layer == Layer.L0_RAW.value
        } == raw_ids


async def test_core_processes_oldest_event_time_first(tmp_path: Path) -> None:
    order = []

    async def record(payload):
        order.append(payload["content"])
        return {}

    queue = HubWriteQueue(tmp_path / "hub.db", record)

    def submit(content: str, at: int) -> None:
        queue.submit(
            {"content": content, "messages": None, "session_id": "", "memory_at": at,
             "request_id": content, "raw_only": False}
        )

    # Backlog accepted before the writer runs: a live turn, then older transcripts.
    submit("live", 1791000200)
    submit("old", 1791000000)
    submit("older", 1790900000)
    queue.start()
    while queue.counts().get("done", 0) < 3:
        await asyncio.sleep(0.01)
    await queue.close()
    assert order == ["older", "old", "live"]


async def test_write_that_names_no_time_is_dated_by_its_arrival(tmp_path: Path) -> None:
    """MCP ``memory_add`` and ``remember`` send no ``memory_at``: the evidence is dated
    when it arrived, so recall can order it and show when it was said."""
    seen = []

    async def record(payload):
        seen.append(payload["memory_at"])
        return {}

    queue = HubWriteQueue(tmp_path / "hub.db", record)
    before = int(time.time())
    queue.submit(
        {"content": "约定", "messages": None, "session_id": "", "memory_at": None,
         "request_id": "no-time", "raw_only": False}
    )
    queue.start()
    while queue.counts().get("done", 0) < 1:
        await asyncio.sleep(0.01)
    await queue.close()
    assert before <= seen[0] <= int(time.time())


def test_prompt_hook_survives_recalled_conversations_without_a_time(
    tmp_path: Path, monkeypatch
) -> None:
    """Rows written before arrival times were recorded have no ``memory_at`` key at all;
    a KeyError here silently dropped the whole injection."""

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **kwargs):
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "mcp:a", "content": "错误码前缀是 QX9"},
                {"session_id": "codex:b", "content": "超时 7.3 秒", "memory_at": 1_793_600_000},
            ]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["hook", "claude-code", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "错误码前缀?",
                          "transcript_path": "/x.jsonl"}, ensure_ascii=False),
    )
    assert out.exit_code == 0, out.output
    context = json.loads(out.output)["hookSpecificOutput"]["additionalContext"]
    assert "QX9" in context and "7.3" in context
    assert "[None]" not in context
    assert "[2026-" in context  # the dated one keeps its time
    assert not (tmp_path / "logs" / "hooks.log").exists()


async def test_recall_skips_the_group_the_caller_disabled(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    calls = []
    core = _memory_client(tmp_path / "hub", fake_embed, fake_llm, fake_jev)

    class Counted:
        settings = core.settings
        factory = core.factory

        async def search(self, **kwargs):
            calls.append("memories")
            return await core.search(**kwargs)

        async def search_conversation(self, **kwargs):
            calls.append("conversations")
            return await core.search_conversation(**kwargs)

    app = create_hub_app(
        api_key="secret", hub_dir=tmp_path / "hub", client=Counted(), settings=core.settings
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret", transport=transport) as remote:
            await remote.recall(query="q", limit=0, raw_limit=3, procedure_limit=0)
            assert calls == ["conversations"]
            calls.clear()
            await remote.recall(query="q", limit=3, raw_limit=0)
            assert calls == ["memories"]


async def test_serve_refuses_non_loopback_and_a_second_core_cannot_steal_the_token(
    tmp_path: Path, monkeypatch, fake_embed, fake_llm, fake_jev
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_kw: None)
    refused = runner.invoke(
        cli_main.app,
        ["serve", "--host", "0.0.0.0", "--hub-dir", str(tmp_path)],
    )
    assert refused.exit_code != 0 and "loopback only" in refused.output

    core = _memory_client(tmp_path, fake_embed, fake_llm, fake_jev)
    running = create_hub_app(api_key="owner-token", hub_dir=tmp_path, client=core)
    async with running.router.lifespan_context(running):
        assert (tmp_path / "runtime-token").read_text().strip() == "owner-token"
        # A second Core (different --api-key) cannot take the lock, and must leave
        # the running Core's token usable.
        second = create_hub_app(api_key="attacker", hub_dir=tmp_path)
        with pytest.raises(RuntimeError, match="already owns"):
            await _start_lifespan(second)
        assert (tmp_path / "runtime-token").read_text().strip() == "owner-token"
        assert resolve_hub_config(tmp_path).api_key == "owner-token"


async def _start_lifespan(app) -> None:
    async with app.router.lifespan_context(app):
        pass


def test_install_service_writes_launchd_plist_or_systemd_unit(
    tmp_path: Path, monkeypatch
) -> None:
    from memhub.hub import service

    hub = tmp_path / "hub"
    monkeypatch.setattr(service, "launchd_plist_path", lambda label: tmp_path / f"{label}.plist")
    monkeypatch.setattr(service, "systemd_unit_path", lambda unit: tmp_path / unit)
    commands = []

    def runner(argv):
        commands.append(argv)
        return {"command": argv, "returncode": 0, "stderr": ""}

    info = service.install_service(hub_dir=hub, port=9999, runner=runner)
    text = Path(info["service_file"]).read_text(errors="replace") if sys.platform != "darwin" else Path(
        info["service_file"]
    ).read_bytes().decode()
    assert "hub" in text and "serve" in text and "9999" in text and str(hub.resolve()) in text
    if sys.platform == "darwin":
        assert commands[0][:1] == ["launchctl"] and commands[-1][1] == "load"
    else:
        assert ["systemctl", "--user", "enable", service.SYSTEMD_UNIT] in commands

    removed = service.uninstall_service(runner=runner)
    assert removed["removed"] is True and not Path(removed["service_file"]).exists()


def test_mcp_builds_without_a_running_core(tmp_path: Path) -> None:
    """A host that starts the MCP server before `hub serve` must not crash at import time."""
    pytest.importorskip("mcp.server.mcpserver")
    from memhub.hub.mcp import build_hub_mcp

    server = build_hub_mcp(hub_dir=tmp_path)  # no runtime.json yet: no connection attempted
    assert server is not None


async def test_unreachable_core_leaves_cursor_in_place(tmp_path: Path) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Core is down")

    path = tmp_path / "ag.jsonl"
    _jsonl(
        path,
        [
            {"type": "user", "session_id": "s", "content": "q"},
            {"type": "assistant", "session_id": "s", "content": "a"},
        ],
    )
    async with HubClient(
        base_url="http://hub", api_key="k", transport=httpx.MockTransport(refuse)
    ) as remote:
        with CursorStore(tmp_path) as cursors:
            with pytest.raises(httpx.ConnectError):
                await scan_file(
                    remote, source="agentica", path=path, cursors=cursors, complete=True
                )
            assert cursors.get("agentica", str(path.resolve())) is None


# --- coding scene: tool steps, version boundary, idle digest ---------------------


def test_tool_calls_become_turn_steps_and_tool_output_is_dropped(tmp_path: Path) -> None:
    roots = default_source_roots(tmp_path)
    agentica = roots["agentica"] / "default" / "proj" / "ag-tools.jsonl"
    # Shape of a real agentica session log: tool_calls on the assistant record,
    # one ``tool`` record per result (parallel calls), cwd / git_branch on each.
    _jsonl(
        agentica,
        [
            {"type": "event", "name": "session_meta", "session_id": "ag"},
            {"type": "user", "session_id": "ag", "cwd": "/proj", "git_branch": "fix/slug",
             "content": "slugify does nothing"},
            {"type": "assistant", "session_id": "ag", "cwd": "/proj", "git_branch": "fix/slug",
             "content": "", "tool_calls": [
                 {"id": "a1", "type": "function",
                  "function": {"name": "read_file", "arguments": "{\"file_path\": \"t.py\"}"}},
                 {"id": "a2", "type": "function",
                  "function": {"name": "glob", "arguments": "{\"pattern\": \"AGENTS.md\"}"}},
             ]},
            {"type": "tool", "session_id": "ag", "cwd": "/proj", "git_branch": "fix/slug",
             "tool_call_id": "a1", "tool_name": "read_file", "content": "def slugify(s): return s"},
            {"type": "tool", "session_id": "ag", "cwd": "/proj", "git_branch": "fix/slug",
             "tool_call_id": "a2", "tool_name": "glob", "content": "[]"},
            {"type": "event", "name": "token_usage", "session_id": "ag"},
            {"type": "assistant", "session_id": "ag", "cwd": "/proj", "git_branch": "fix/slug",
             "content": "fixed slugify"},
        ],
    )
    messages, _ = SOURCE_SPECS["agentica"].read(agentica, 0)
    done, open_turn = build_turns(messages)
    turn = (done + [open_turn])[0]
    assert (turn.user, turn.assistant) == ("slugify does nothing", "fixed slugify")
    assert (turn.workspace, turn.branch) == ("/proj", "fix/slug")
    msgs = turn.messages()
    # Tool calls are kept (the files they touch); tool output never is.
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant", "assistant"]
    assert [m["tool_calls"][0]["function"]["name"] for m in msgs[1:3]] == ["read_file", "glob"]
    assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"file_path": "t.py"}
    assert "def slugify" not in json.dumps(msgs)

    claude = roots["claude-code"] / "proj" / "cc-tools.jsonl"
    _jsonl(
        claude,
        [
            {"type": "user", "sessionId": "cc", "cwd": "/repo", "gitBranch": "main",
             "message": {"content": "fix the flaky test"}},
            {"type": "assistant", "sessionId": "cc", "cwd": "/repo", "gitBranch": "main",
             "message": {"content": [
                 {"type": "text", "text": "running it"},
                 {"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {"command": "pytest -q tests/test_x.py"}},
             ]}},
            {"type": "user", "sessionId": "cc", "cwd": "/repo", "gitBranch": "fix",
             "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "t1",
                  "content": [{"type": "text", "text": "1 failed: race in setup"}]},
             ]}},
            {"type": "assistant", "sessionId": "cc", "cwd": "/repo", "gitBranch": "fix",
             "message": {"content": [{"type": "text", "text": "fixed the race"}]}},
            {"type": "user", "sessionId": "cc", "message": {"content": "thanks"}},
        ],
    )
    messages, _ = SOURCE_SPECS["claude-code"].read(claude, 0)
    done, open_turn = build_turns(messages)
    (turn,) = done
    assert (turn.user, turn.assistant) == ("fix the flaky test", "fixed the race")
    assert (turn.workspace, turn.branch) == ("/repo", "fix")
    msgs = turn.messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
    call = msgs[1]["tool_calls"][0]
    assert call["function"]["name"] == "Bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "pytest -q tests/test_x.py"}
    assert "race in setup" not in json.dumps(msgs)
    assert open_turn is not None and open_turn.user == "thanks"

    codex = roots["codex"] / "2026" / "10" / "04" / "rollout-tools.jsonl"
    _jsonl(
        codex,
        [
            {"type": "session_meta",
             "payload": {"id": "cx", "cwd": "/svc", "git": {"branch": "dev"}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "why 500?"}]}}},
            {"type": "response_item", "payload": {
                "type": "function_call", "call_id": "c1", "name": "shell",
                "arguments": "{\"cmd\": \"tail app.log\"}"}},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": "x" * 9000}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "AgentMessage", "content": [{"type": "Text", "text": "DB pool exhausted"}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "ok"}]}}},
        ],
    )
    messages, _ = SOURCE_SPECS["codex"].read(codex, 0)
    (turn,), _ = build_turns(messages)
    assert (turn.session_id, turn.workspace, turn.branch) == ("cx", "/svc", "dev")
    roles = [m["role"] for m in turn.messages()]
    assert roles == ["user", "assistant", "assistant"]
    assert "xxxx" not in json.dumps(turn.messages())

    from memhub.coding.preproc import extract_files

    codebuddy = roots["codebuddy"] / "proj" / "cb-tools.jsonl"
    _jsonl(
        codebuddy,
        [
            {"type": "message", "role": "user", "sessionId": "cb", "cwd": "/cb",
             "gitBranch": "feat/cb", "timestamp": 1791000000000,
             "content": [{"type": "input_text", "text": "fix parser"}]},
            {"type": "function_call", "sessionId": "cb", "cwd": "/cb",
             "callId": "fc1", "name": "Read",
             "arguments": "{\"file_path\": \"src/parser.py\"}"},
            {"type": "function_call_result", "sessionId": "cb", "cwd": "/cb",
             "callId": "fc1", "name": "Read", "output": "secret_body"},
            {"type": "message", "role": "assistant", "sessionId": "cb", "cwd": "/cb",
             "timestamp": 1791000005000,
             "content": [{"type": "output_text", "text": "parser fixed"}]},
        ],
    )
    messages, _ = SOURCE_SPECS["codebuddy"].read(codebuddy, 0)
    _, open_turn = build_turns(messages)
    assert open_turn is not None
    assert (open_turn.session_id, open_turn.workspace, open_turn.branch) == ("cb", "/cb", "feat/cb")
    assert extract_files(open_turn.messages()) == ["src/parser.py"]
    assert "secret_body" not in json.dumps(open_turn.messages())

    opencode = roots["opencode"]
    _opencode_db(
        opencode,
        [("oc-s", "/oc/repo", None)],
        [
            ("m1", "oc-s", 1791000010000, {"role": "user"}),
            ("m2", "oc-s", 1791000011000, {"role": "assistant"}),
            ("m3", "oc-s", 1791000012000, {"role": "assistant"}),
        ],
        [
            ("p1", "m1", "oc-s", 1791000010000, {"type": "text", "text": "write svg"}),
            ("p2", "m2", "oc-s", 1791000011000, {
                "type": "tool", "tool": "write", "callID": "tc1",
                "state": {"status": "completed", "input": {
                    "filePath": "/oc/repo/out.svg", "content": "<svg>huge</svg>",
                }, "output": "wrote file"},
            }),
            ("p3", "m3", "oc-s", 1791000012000, {"type": "text", "text": "done svg"}),
        ],
    )
    messages, _ = SOURCE_SPECS["opencode"].read(opencode, 0)
    _, open_turn = build_turns(messages)
    assert open_turn is not None
    assert (open_turn.session_id, open_turn.workspace) == ("oc-s", "/oc/repo")
    assert extract_files(open_turn.messages()) == ["/oc/repo/out.svg"]
    assert "<svg>huge</svg>" not in json.dumps(open_turn.messages())
    assert "wrote file" not in json.dumps(open_turn.messages())


async def test_scan_forwards_tool_steps_and_boundary(tmp_path: Path) -> None:
    roots = default_source_roots(tmp_path)
    path = roots["claude-code"] / "proj" / "cc-b.jsonl"
    _jsonl(
        path,
        [
            {"type": "user", "sessionId": "s", "cwd": "/w", "gitBranch": "b",
             "message": {"content": "q"}},
            {"type": "assistant", "sessionId": "s", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Read", "input": {"path": "a.py"}}]}},
            {"type": "user", "sessionId": "s", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "body"}]}},
            {"type": "assistant", "sessionId": "s", "message": {"content": "a"}},
            {"type": "user", "sessionId": "s", "message": {"content": "next"}},
        ],
    )
    sent: list[dict] = []

    class Writer:
        async def write(self, **kwargs):
            sent.append(kwargs)
            return {"status": "pending"}

    await scan_file(Writer(), source="claude-code", path=path,
                    cursors=CursorStore(tmp_path / "cursors.db"))
    (call,) = sent
    assert (call["workspace_id"], call["branch"]) == ("/w", "b")
    assert [m["role"] for m in call["messages"]] == ["user", "assistant", "assistant"]


def test_last_digest_due_follows_the_stamp(tmp_path: Path) -> None:
    assert last_digest_due(tmp_path, min_hours=24) is True
    write_last_digest_at(tmp_path, 1_000_000.0)
    assert last_digest_due(tmp_path, min_hours=24, now=1_000_000.0 + 23 * 3600) is False
    assert last_digest_due(tmp_path, min_hours=24, now=1_000_000.0 + 24 * 3600) is True
    assert last_digest_due(tmp_path, min_hours=0, now=1_000_000.0) is False


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


async def test_idle_hook_runs_after_work_and_survives_failures(tmp_path: Path) -> None:
    runs: list[int] = []
    fail = {"on": True}

    async def process(payload):
        return {}

    async def idle():
        runs.append(1)
        if fail["on"]:
            raise RuntimeError("model down")

    queue = HubWriteQueue(tmp_path / "hub.db", process, on_idle=idle, idle_seconds=0.05)
    queue.start()
    # Once after start (work left by a previous process), failure recorded.
    await asyncio.wait_for(_until(lambda: runs), 5)
    await asyncio.sleep(0.05)
    assert queue.last_idle_error == "RuntimeError: model down"
    await asyncio.sleep(0.15)
    assert len(runs) == 1  # nothing new written: no repeat
    fail["on"] = False
    queue.submit({"content": "x", "messages": None, "session_id": "", "memory_at": None,
                  "request_id": "r", "raw_only": False})
    await asyncio.wait_for(_until(lambda: len(runs) >= 2), 5)
    await asyncio.sleep(0.02)
    assert queue.last_idle_error is None
    await queue.close()


async def test_dual_core_digests_on_idle_and_recalls_procedures(
    tmp_path: Path, fake_embed
) -> None:
    from conftest import FakeJev, FakeLLMClient

    candidate = {"task": "How to fix the flaky race in setup",
                 "solution": "Serialize fixture setup with a lock", "search_keys": []}
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "hub" / "store"), mode="dual"),
        embed=fake_embed,
        llm=FakeLLMClient({"json": {"candidates": [candidate]}}),
        jev=FakeJev({"scene": "CODING"}),
    )
    digests: list[int] = []
    original = core.digest

    async def counted():
        digests.append(1)
        return await original()

    core.digest = counted
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core,
                         settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            receipt = await remote.write(
                messages=[
                    {"role": "user", "content": "fix the flaky test"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "t", "type": "function",
                         "function": {"name": "Bash", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "t", "content": "race in setup"},
                    {"role": "assistant", "content": "fixed"},
                ],
                request_id="r1", wait=True, workspace_id="/repo", branch="main",
            )
            assert receipt["status"] == "done"
            assert receipt["result"]["scene"] == "coding"
            proc = core.factory.vector.get(receipt["result"]["procedure_ids"][0])
            assert proc.custom["procedure"]["workspace_id"] == "/repo"
            recalled = await remote.recall(query=candidate["task"], limit=0, raw_limit=0)
            assert [m["memory_id"] for m in recalled["memories"]["procedures"]] == [
                proc.node_id
            ]
            # The write arrived inside the first idle window: one digest after it.
            await asyncio.wait_for(_until(lambda: digests), 5)
            assert core.factory.cache.list_pending_s2_scopes() == []
            status = await remote.status()
            assert status["digest"]["auto"] is True
            assert status["digest"]["min_hours"] == 24.0
            assert status["digest"]["last_at"] == format_time(
                read_last_digest_at(tmp_path / "hub")
            )
            assert len(status["digest"]["last_at"]) == 19
            assert status["digest"]["last_error"] is None
    await core.aclose()


async def test_auto_digest_skips_pauses_until_a_day_has_passed(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    hub.mkdir()
    write_last_digest_at(hub, time.time())
    core = MemoryClient(
        settings=Settings(storage_dir=str(hub / "store"), mode="dual"),
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )
    calls: list[int] = []
    seen: list = []
    original = core.digest

    async def counted():
        calls.append(1)
        result = await original()
        seen.append(result)
        return result

    core.digest = counted
    app = create_hub_app(
        api_key="secret", hub_dir=hub, client=core, settings=core.settings,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            receipt = await remote.write(content="我习惯用 pytest", request_id="r", wait=True)
            assert receipt["status"] == "done"
            assert core.factory.cache.list_pending_s2_scopes()
            await asyncio.sleep(0.25)
            assert calls == []
            stamped = read_last_digest_at(hub)
            assert stamped is not None and time.time() - stamped < 10

            write_last_digest_at(hub, time.time() - 25 * 3600)
            remote_write = await remote.write(
                content="明天起改用 uv", request_id="r2", wait=True
            )
            assert remote_write["status"] == "done"
            await asyncio.wait_for(
                _until(lambda: seen or app.state.queue.last_idle_error), 5
            )
            assert app.state.queue.last_idle_error is None
            assert len(calls) == 1 and seen[0].processed > 0
            assert time.time() - read_last_digest_at(hub) < 10
            assert core.factory.cache.list_pending_s2_scopes() == []
    await core.aclose()


async def test_auto_digest_does_not_run_without_pending_evidence(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = MemoryClient(
        settings=Settings(storage_dir=str(hub / "store"), mode="dual"),
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )
    calls: list[int] = []

    async def counted():
        calls.append(1)
        raise AssertionError("digest with an empty queue")

    core.digest = counted
    app = create_hub_app(
        api_key="secret", hub_dir=hub, client=core, settings=core.settings,
    )
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.2)
        assert calls == []
        assert read_last_digest_at(hub) is None
    await core.aclose()


async def test_failed_auto_digest_does_not_start_the_day_clock(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    hub = tmp_path / "hub"
    core = MemoryClient(
        settings=Settings(storage_dir=str(hub / "store"), mode="dual"),
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )

    async def broken():
        raise RuntimeError("model down")

    core.digest = broken
    app = create_hub_app(
        api_key="secret", hub_dir=hub, client=core, settings=core.settings,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            receipt = await remote.write(content="记下这件事", request_id="r", wait=True)
            assert receipt["status"] == "done"
            await asyncio.wait_for(
                _until(lambda: app.state.queue.last_idle_error), 5
            )
            assert app.state.queue.last_idle_error == "RuntimeError: model down"
            assert read_last_digest_at(hub) is None
            assert core.factory.cache.list_pending_s2_scopes()
    await core.aclose()


async def test_digest_every_zero_turns_auto_digest_off(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "hub" / "store"), mode="dual"),
        embed=fake_embed,
        llm=fake_llm,
        jev=fake_jev,
    )
    app = create_hub_app(
        api_key="secret", hub_dir=tmp_path / "hub", client=core,
        settings=core.settings, digest_min_hours=0,
    )
    async with app.router.lifespan_context(app):
        assert app.state.queue._on_idle is None
    await core.aclose()


async def test_system1_core_never_digests(tmp_path: Path, fake_embed, fake_llm, fake_jev) -> None:
    core = _memory_client(tmp_path / "hub", fake_embed, fake_llm, fake_jev)
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core,
                         settings=core.settings)
    async with app.router.lifespan_context(app):
        assert app.state.queue._on_idle is None
    await core.aclose()


def test_claude_code_hook_keeps_tool_trajectory() -> None:
    from memhub.integrations.claude_code import to_messages

    out = to_messages([
        {"type": "user", "message": {"role": "user", "content": "fix it"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "t", "name": "Read", "input": {"path": "a.py"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "print(1)"}]}},
        {"role": "assistant", "content": "done"},
        {"type": "summary", "summary": "ignored"},
    ])
    assert out == [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "t", "type": "function",
             "function": {"name": "Read", "arguments": "{\"path\": \"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": "t", "content": "print(1)"},
        {"role": "assistant", "content": "done"},
    ]


def test_hook_install_replaces_hooks_written_as_cli_main(tmp_path: Path) -> None:
    """Hooks installed before ``-m memhub.cli`` are still recognised as ours."""
    from memhub.hub.hosts import ClaudeStyleHooks, Launch

    hook_file = tmp_path / "settings.json"
    old = f"{sys.executable} -m memhub.cli.main hub capture claude-code"
    hook_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": old}]},
                        {"hooks": [{"type": "command", "command": "other-tool"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    ClaudeStyleHooks().install(hook_file, Launch("claude-code", tmp_path / "hub"))

    stop = json.loads(hook_file.read_text(encoding="utf-8"))["hooks"]["Stop"]
    commands = [e["hooks"][0]["command"] for e in stop]
    assert "other-tool" in commands
    assert old not in commands
    ours = [c for c in commands if "-m memhub.cli" in c]
    assert len(ours) == 1 and "-m memhub.cli hook" in ours[0]


async def test_memory_browser_groups_by_project_and_edits_through_versions(
    tmp_path: Path, fake_embed
) -> None:
    """``/v1/hub/memories`` gives each memory its project; edit and delete keep history."""
    from conftest import FakeJev, FakeLLMClient

    candidate = {"task": "How to fix the flaky race in setup",
                 "solution": "Serialize fixture setup with a lock", "search_keys": []}
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "hub" / "store"), mode="system1"),
        embed=fake_embed,
        llm=FakeLLMClient({
            "json": {"candidates": [candidate]},
            "extract": lambda system, user: {
                "facts": [{"content": f"记录：{user[:40]}", "tags": []}],
                "identity": [], "intentions": [], "is_ephemeral": False,
                "basic_info": {},
            },
        }),
        jev=FakeJev({"scene": "CODING"}),
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core,
                         settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            coding = await remote.write(
                messages=[
                    {"role": "user", "content": "fix the flaky test"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "t", "type": "function",
                         "function": {"name": "Bash", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "t", "content": "race in setup"},
                    {"role": "assistant", "content": "fixed"},
                ],
                session_id="codex:s1", request_id="r1", wait=True,
                workspace_id="/w/repo", branch="main",
            )
            chat = await remote.write(
                messages=[{"role": "user", "content": "随便聊聊"},
                          {"role": "assistant", "content": "好"}],
                session_id="agentica:s2", request_id="r2", wait=True,
            )
            rows = (await remote.relay("GET", "/v1/hub/memories"))["memories"]
            by_id = {row["id"]: row for row in rows}
            proc_id = coding["result"]["procedure_ids"][0]
            coding_raw = coding["result"]["memory_id"]
            chat_raw = chat["result"]["memory_id"]
            # The procedure carries the workspace; its dialogue and the facts
            # extracted from that dialogue inherit it through source_ids.
            assert by_id[proc_id]["project"] == "/w/repo"
            assert by_id[proc_id]["procedure"]["task"] == candidate["task"]
            assert by_id[coding_raw]["project"] == "/w/repo"
            assert by_id[coding_raw]["source"] == "codex"
            coding_facts = [r for r in rows if r["kind"] == "fact"
                            and r["source_ids"] == [coding_raw]]
            assert coding_facts and all(r["project"] == "/w/repo" for r in coding_facts)
            assert by_id[chat_raw]["project"] == ""
            assert [r for r in rows if r["source_ids"] == [chat_raw]]
            assert all(r["project"] == "" for r in rows if r["source_ids"] == [chat_raw])

            edited = await remote.relay("PUT", f"/v1/hub/memories/{proc_id}",
                                        {"content": "Use a session-scoped lock"})
            rows = (await remote.relay("GET", "/v1/hub/memories"))["memories"]
            by_id = {row["id"]: row for row in rows}
            new = by_id[edited["memory_id"]]
            assert new["is_latest"] and new["supersedes"] == [proc_id]
            assert new["procedure"]["solution"] == "Use a session-scoped lock"
            assert new["procedure"]["task"] == candidate["task"]
            assert new["project"] == "/w/repo"
            old = by_id[proc_id]
            assert not old["is_latest"] and not old["deleted"]
            assert old["superseded_by"] == [new["id"]]

            fact_id = coding_facts[0]["id"]
            await remote.relay("DELETE", f"/v1/hub/memories/{fact_id}")
            rows = (await remote.relay("GET", "/v1/hub/memories"))["memories"]
            gone = next(r for r in rows if r["id"] == fact_id)
            assert not gone["is_latest"] and gone["deleted"]

            with pytest.raises(httpx.HTTPStatusError) as stale:
                await remote.relay("PUT", f"/v1/hub/memories/{proc_id}", {"content": "x"})
            assert stale.value.response.status_code == 409
            with pytest.raises(httpx.HTTPStatusError) as missing:
                await remote.relay("DELETE", "/v1/hub/memories/nope")
            assert missing.value.response.status_code == 404

        async with httpx.AsyncClient(base_url="http://hub", transport=transport) as anon:
            assert (await anon.get("/v1/hub/memories")).status_code == 401
            page = await anon.get("/ui")
            assert page.status_code == 200
            # The page holds no data; it reads the token from its own URL.
            assert "/v1/hub/memories" in page.text and "secret" not in page.text
    await core.aclose()


async def test_recall_stays_in_the_prompts_project_except_profile(
    tmp_path: Path, fake_embed
) -> None:
    """Facts and dialogue come from the prompt's project; the profile from any."""
    from conftest import FakeJev, FakeLLMClient

    rule = "用 tmp 放临时脚本"
    style = "有歧义先按推荐方案做"
    other = "b 项目的部署走 k8s"

    def extract(system: str, user: str) -> dict:
        identity = [{"content": style, "speaker": "user"}] if style in user else []
        facts = [{"content": t, "speaker": "user"} for t in (rule, other) if t in user]
        return {"facts": facts,
                "identity": identity, "intentions": [], "is_ephemeral": False,
                "basic_info": {}}

    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "hub" / "store"), mode="system1"),
        embed=fake_embed,
        llm=FakeLLMClient({"extract": extract}),
        jev=FakeJev(),
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core,
                         settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            for i, (text, workspace) in enumerate([
                (rule, "/w/a"), (rule, "/w/b"), (style, "/w/b"),
            ]):
                await remote.write(
                    messages=[{"role": "user", "content": text},
                              {"role": "assistant", "content": "好"}],
                    session_id=f"agentica:s{i}", request_id=f"r{i}", wait=True,
                    workspace_id=workspace,
                )
            facts = await remote.recall(query=rule, raw_limit=3, workspace="/w/a")
            assert {m["session_id"] for m in facts["memories"]["normal"]} == {"agentica:s0"}
            assert {c["session_id"] for c in facts["conversations"]} == {"agentica:s0"}

            profile = await remote.recall(query=style, raw_limit=3, workspace="/w/a")
            assert [m["content"] for m in profile["memories"]["profile"]] == [style]
            assert {m["session_id"] for m in profile["memories"]["normal"]} <= {"agentica:s0"}
            assert {c["session_id"] for c in profile["conversations"]} <= {"agentica:s0"}

            # No workspace (a plain chat host): everything is in scope.
            anywhere = await remote.recall(query=rule, raw_limit=3)
            assert {m["session_id"] for m in anywhere["memories"]["normal"]} == {
                "agentica:s0", "agentica:s1"}

            # A question about another project: its fact matches better than
            # anything of this one, so it comes in; this project's fact stays.
            await remote.write(
                messages=[{"role": "user", "content": other},
                          {"role": "assistant", "content": "好"}],
                session_id="agentica:s3", request_id="r3", wait=True, workspace_id="/w/b",
            )
            crossed = await remote.recall(query=other, workspace="/w/a")
            contents = [m["content"] for m in crossed["memories"]["normal"]]
            assert contents[0] == other and "agentica:s1" not in {
                m["session_id"] for m in crossed["memories"]["normal"]}

            # The hook's cwd and the write's workspace are the same directory
            # written differently (a trailing slash, a symlinked parent).
            alias = await remote.recall(query=rule, workspace="/w/a/")
            assert {m["session_id"] for m in alias["memories"]["normal"]} == {"agentica:s0"}
    await core.aclose()


async def test_recall_scope_includes_project_sessions_from_another_device(
    tmp_path: Path, fake_embed
) -> None:
    """The project's sessions are read from the replicated store, not this
    device's write queue: once this device writes one session of the project,
    the sessions another device wrote there still count."""
    from conftest import FakeJev, FakeLLMClient

    away = "部署脚本放在 deploy/ 目录"
    here = "单测用 tmp_path"
    elsewhere = "部署脚本放在 ops/ 目录"

    def extract(system: str, user: str) -> dict:
        facts = [{"content": t, "speaker": "user"}
                 for t in (away, here, elsewhere) if t in user]
        return {"facts": facts, "identity": [], "intentions": [],
                "is_ephemeral": False, "basic_info": {}}

    def device(name: str) -> MemoryClient:
        return MemoryClient(
            settings=Settings(storage_dir=str(tmp_path / name / "store"), mode="system1"),
            embed=fake_embed,
            llm=FakeLLMClient({"extract": extract}),
            jev=FakeJev(),
        )

    other = device("a")
    for i, (text, workspace) in enumerate([(away, "/w/repo"), (elsewhere, "/w/ops")]):
        await other.add(
            messages=[{"role": "user", "content": text},
                      {"role": "assistant", "content": "好"}],
            user_id="default", session_id=f"cursor:a{i}", request_id=f"a{i}",
            workspace_id=workspace,
        )
    raw = await other.add_raw(
        content="只存原文的会话", user_id="default", session_id="cursor:a9",
        request_id="a9", workspace_id="/w/repo",
    )
    assert other.factory.vector.get(raw.memory_id).custom == {"workspace_id": "/w/repo"}

    core = device("b")
    core.factory.vector.import_log(other.factory.vector.export_log())
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "b" / "hub", client=core,
                         settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret",
                             transport=transport) as remote:
            # Nothing written here yet: the synced sessions already scope recall.
            first = await remote.recall(query=away, raw_limit=5, workspace="/w/repo")
            assert {m["session_id"] for m in first["memories"]["normal"]} == {"cursor:a0"}
            await remote.write(
                messages=[{"role": "user", "content": here},
                          {"role": "assistant", "content": "好"}],
                session_id="agentica:b0", request_id="b0", wait=True,
                workspace_id="/w/repo",
            )
            recalled = await remote.recall(query=away, raw_limit=5, workspace="/w/repo")
            normal = {m["session_id"] for m in recalled["memories"]["normal"]}
            assert "cursor:a0" in normal and "cursor:a1" not in normal
            assert {c["session_id"] for c in recalled["conversations"]} == {
                "cursor:a0", "cursor:a9", "agentica:b0"}
    await other.aclose()
    await core.aclose()


def test_recalled_dialogue_is_dated_and_oldest_first(tmp_path: Path, monkeypatch) -> None:
    """A later turn must read as the correction of an earlier one."""
    sent: list[dict] = []

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **kwargs):
            sent.append(kwargs)
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "agentica:new", "content": "改成默认 300 秒超时",
                 "memory_at": 1_793_600_000},
                {"session_id": "agentica:old", "content": "一直阻塞不加超时",
                 "memory_at": 1_793_500_000},
            ]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["hook", "agentica", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "超时?",
                          "cwd": "/w/repo"}, ensure_ascii=False),
    )
    assert out.exit_code == 0, out.output
    context = json.loads(out.output)["hookSpecificOutput"]["additionalContext"]
    old, new = context.index("一直阻塞不加超时"), context.index("改成默认 300 秒超时")
    assert old < new
    assert f"[{format_time(1_793_500_000)}] 一直阻塞" in context
    assert sent[0]["workspace"] == "/w/repo"


def test_long_recalled_dialogue_is_cut_to_the_passages_on_the_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    filler = "".join(f"[user]: 第 {i} 件无关的事 {'x ' * 300}\n[assistant]: 好\n" for i in range(30))
    session = f"{filler}[user]: 迁移脚本要先备份 ledger 库\n[assistant]: 明白，先备份\n{filler}"

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **_kwargs):
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "agentica:s", "content": session, "memory_at": 1_793_500_000},
            ]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["hook", "agentica", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": "迁移脚本前要备份 ledger 吗"}, ensure_ascii=False),
    )
    assert out.exit_code == 0, out.output
    context = json.loads(out.output)["hookSpecificOutput"]["additionalContext"]
    assert "迁移脚本要先备份 ledger 库" in context
    assert len(context) < cli_hub.RAW_EXCERPT_CHARS + 400 < len(session)


def test_hub_ui_opens_the_page_with_the_token_in_the_fragment(
    tmp_path: Path, monkeypatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        cli_hub, "resolve_hub_config",
        lambda _dir: SimpleNamespace(base_url="http://127.0.0.1:8765", api_key="tok"),
    )
    monkeypatch.setattr(cli_hub.webbrowser, "open", opened.append)
    result = runner.invoke(cli_hub.hub_app, ["ui", "--hub-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "http://127.0.0.1:8765/ui"
    assert opened == ["http://127.0.0.1:8765/ui#token=tok"]


def test_agentica_install_replaces_the_old_two_consumers_with_one(tmp_path: Path) -> None:
    """An install from before the merge left ``memhub-hub`` + ``memhub-hub-recall``;
    reinstalling leaves exactly one consumer of ours, found by its command."""
    hub = tmp_path / "hub"
    home = tmp_path / "home"
    (home / ".agentica").mkdir(parents=True)
    old = [sys.executable, "-m", "memhub.cli"]
    (home / ".agentica" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "settings": {
                    "hooks": {
                        "enabled": True,
                        "consumers": [
                            {"name": "open-island", "command": ["/bin/oi"]},
                            {"name": "memhub-hub", "command": old + ["capture", "agentica"]},
                            {"name": "memhub-hub-recall", "command": old + ["hook-search"]},
                        ],
                    }
                }
            }
        )
    )
    install_host("agentica", hub_dir=hub, home=home)
    consumers = yaml.safe_load((home / ".agentica" / "config.yaml").read_text())[
        "settings"
    ]["hooks"]["consumers"]
    assert [c["name"] for c in consumers] == ["open-island", "memhub"]


def test_hook_dispatches_on_the_event_name(tmp_path: Path, monkeypatch) -> None:
    """``UserPromptSubmit`` recalls and never captures; any other event captures
    and never prints context."""
    hub = tmp_path / "hub"
    spawned, recalled = [], []
    monkeypatch.setattr(cli_hub, "_spawn_detached", lambda argv, _log: spawned.append(argv))
    monkeypatch.setattr(cli_hub, "_recall", lambda payload, *_a: recalled.append(payload["prompt"]))
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text("")

    def hook(*args: str, **payload: Any) -> None:
        result = runner.invoke(
            cli_main.app, [*args, "--hub-dir", str(hub)], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output

    hook("hook", "agentica", hook_event_name="UserPromptSubmit", prompt="q1",
         transcript_path=str(transcript))
    assert recalled == ["q1"] and spawned == []
    hook("hook", "agentica", hook_event_name="run.completed", prompt="q1",
         transcript_path=str(transcript))
    assert recalled == ["q1"] and len(spawned) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the plugin")
def test_opencode_plugin_bridges_events_onto_hub_hook(tmp_path, monkeypatch):
    from memhub.hub.hosts import Launch, OpenCodePlugin

    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake_hook.py"
    fake.write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(json.dumps(p) + '\\n')\n"
        "if p['hook_event_name'] == 'UserPromptSubmit':\n"
        "    print(json.dumps({'hookSpecificOutput': {'additionalContext': 'CTX:' + p['prompt']}}))\n"
    )
    monkeypatch.setattr(Launch, "hook", lambda self: [sys.executable, str(fake)])
    plugin = tmp_path / "memhub-hub.mjs"
    OpenCodePlugin().install(plugin, Launch("opencode", tmp_path))
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        f"import make from {json.dumps(plugin.as_uri())};\n"
        "const hooks = await make();\n"
        "const output = { message: { system: 'base' }, parts: [\n"
        "  { type: 'text', text: '问题' }, { type: 'text', text: 'x', synthetic: true }] };\n"
        "await hooks['chat.message']({ sessionID: 's1' }, output);\n"
        "await hooks.event({ event: { type: 'session.status', properties: { sessionID: 's1', status: { type: 'idle' } } } });\n"
        "await hooks.event({ event: { type: 'session.idle', properties: { sessionID: 's1' } } });\n"
        "console.log(output.message.system);\n"
    )
    proc = subprocess.run(["node", str(driver)], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "base\n\nCTX:问题"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "问题"},
        {"hook_event_name": "Stop", "session_id": "s1"},
    ]


def _cursor_transcript(root: Path, project: str, records: list[dict]) -> Path:
    path = root / project / "agent-transcripts" / "c1" / "c1.jsonl"
    _jsonl(path, records)
    return path


def _cursor_user(text: str) -> dict:
    return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _cursor_assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "message": {"content": list(blocks)}}


def test_cursor_turn_keeps_time_workspace_and_tool_paths_but_no_file_text(
    tmp_path: Path,
) -> None:
    project = tmp_path / "my_repo-x"
    project.mkdir()
    path = _cursor_transcript(
        tmp_path / "projects",
        _slug(str(project)),
        [
            _cursor_user(
                "<timestamp>Wednesday, Oct 14, 2026, 1:16 PM (UTC+8)</timestamp>\n"
                "<user_query>\n改一下 a.py\n</user_query>"
            ),
            _cursor_assistant(
                {"type": "text", "text": "我先看看"},
                {"type": "tool_use", "name": "Read", "input": {"path": "/w/a.py", "limit": 5}},
                {"type": "tool_use", "name": "StrReplace", "input": {
                    "path": "/w/a.py", "old_string": "OLD_TEXT", "new_string": "NEW_TEXT"}},
                {"type": "tool_use", "name": "Write", "input": {"path": "/w/b.py", "contents": "BODY"}},
                {"type": "tool_use", "name": "ApplyPatch", "input": "*** Begin Patch PATCH_TEXT"},
            ),
            _cursor_assistant({"type": "text", "text": "改好了"}),
            {"type": "turn_ended", "status": "success"},
        ],
    )
    messages, _ = SOURCE_SPECS["cursor"].read(path, 0)
    turns, open_turn = build_turns(messages)
    assert turns == [] and open_turn is not None
    assert open_turn.user == "改一下 a.py"
    assert open_turn.assistant == "改好了"
    assert open_turn.workspace == str(project)
    assert open_turn.at == 1791954960  # 2026-10-14 05:16 UTC
    assert open_turn.branch is None
    wire = json.dumps(open_turn.messages(), ensure_ascii=False)
    for leaked in ("OLD_TEXT", "NEW_TEXT", "BODY", "PATCH_TEXT"):
        assert leaked not in wire
    # The trace is what makes the Core treat this as a coding session.
    assert has_any_tool_message(open_turn.messages())
    assert extract_files(open_turn.messages()) == ["/w/a.py", "/w/b.py"]


def test_cursor_read_resumed_mid_file_keeps_workspace_and_uses_each_prompts_time(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    path = _cursor_transcript(
        tmp_path / "projects",
        _slug(str(project)),
        [
            _cursor_user(
                "<timestamp>Wednesday, Oct 14, 2026, 1:16 PM (UTC+8)</timestamp>"
                "<user_query>一</user_query>"
            ),
            _cursor_assistant({"type": "text", "text": "答一"}),
            _cursor_user(
                "<timestamp>Wednesday, Oct 14, 2026, 11:30 PM (UTC-5)</timestamp>"
                "<user_query>二</user_query>"
            ),
            _cursor_assistant({"type": "text", "text": "答二"}),
        ],
    )
    first_two = sum(len(line) for line in path.read_bytes().splitlines(True)[:2])
    messages, _ = SOURCE_SPECS["cursor"].read(path, first_two)
    _, open_turn = build_turns(messages)
    assert open_turn.user == "二"
    assert open_turn.workspace == str(project)
    assert open_turn.at == 1792038600  # 2026-10-15 04:30 UTC


def test_cursor_prompt_without_timestamp_falls_back_to_file_mtime(tmp_path: Path) -> None:
    path = _cursor_transcript(
        tmp_path,
        "empty-window",
        [_cursor_user("<user_query>问</user_query>"), _cursor_assistant({"type": "text", "text": "答"})],
    )
    os.utime(path, (1700000000, 1700000000))
    messages, _ = SOURCE_SPECS["cursor"].read(path, 0)
    assert [m.at for m in messages] == [1700000000, 1700000000]
    assert {m.workspace for m in messages} == {None}


def test_cursor_project_that_no_longer_exists_is_placed_from_tool_paths(tmp_path: Path) -> None:
    gone = tmp_path / "gone" / "my.repo"
    path = _cursor_transcript(
        tmp_path / "projects",
        _slug(str(gone)),
        [
            _cursor_user("<user_query>问</user_query>"),
            _cursor_assistant(
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "/not/a/match"}},
                {"type": "tool_use", "name": "Read", "input": {"path": f"{gone}/src/a.py"}},
            ),
            _cursor_assistant({"type": "text", "text": "答"}),
        ],
    )
    messages, _ = SOURCE_SPECS["cursor"].read(path, 0)
    assert build_turns(messages)[1].workspace == str(gone)


def test_cursor_slug_matching_two_directories_is_left_undecided(tmp_path: Path) -> None:
    (tmp_path / "a-b").mkdir()
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert _workspace_of_slug(_slug(str(tmp_path / "a-b"))) is None
    # A symlink to the same directory is one directory, not an ambiguity.
    (tmp_path / "c").mkdir()
    (tmp_path / "d-e").mkdir()
    (tmp_path / "d").symlink_to(tmp_path / "c")
    (tmp_path / "c" / "e").symlink_to(tmp_path / "d-e")
    assert _workspace_of_slug(_slug(str(tmp_path / "d-e"))) in (
        str(tmp_path / "d-e"), str(tmp_path / "d" / "e"),
    )


def test_cursor_hooks_install_beside_other_hooks_and_uninstall_cleanly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".cursor" / "hooks.json"
    path.parent.mkdir(parents=True)
    other = {"command": "/bin/other --source cursor"}
    path.write_text(json.dumps({"version": 1, "hooks": {"stop": [other], "afterFileEdit": [other]}}))

    install_host("cursor", hub_dir=tmp_path / "hub", home=home)
    install_host("cursor", hub_dir=tmp_path / "hub", home=home)  # idempotent

    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["hooks"]["afterFileEdit"] == [other]
    assert data["hooks"]["stop"][0] == other and len(data["hooks"]["stop"]) == 2
    ours = ["-m", "memhub.cli", "hook", "cursor", "--hub-dir", str(tmp_path / "hub")]
    assert shlex.split(data["hooks"]["stop"][1]["command"])[1:] == ours
    assert [shlex.split(e["command"])[1:] for e in data["hooks"]["beforeSubmitPrompt"]] == [ours]
    assert hosts_status(home=home)["cursor"]["hooks"] is True

    assert uninstall_host("cursor", home=home)["hooks"] is True
    assert json.loads(path.read_text())["hooks"]["stop"] == [other]
    assert json.loads(path.read_text())["hooks"]["beforeSubmitPrompt"] == []
    assert hosts_status(home=home)["cursor"]["hooks"] is False


def test_hook_captures_cursor_stop_and_ignores_cursor_running_claude_hooks(
    tmp_path: Path, monkeypatch
) -> None:
    hub = tmp_path / "hub"
    spawned, recalled = [], []
    monkeypatch.setattr(cli_hub, "_spawn_detached", lambda argv, _log: spawned.append(argv))
    monkeypatch.setattr(cli_hub, "_recall", lambda payload, *_a: recalled.append(payload))
    transcript = tmp_path / "t.jsonl"
    cursor_base = {"cursor_version": "2.0", "transcript_path": str(transcript)}

    def hook(source: str, **payload: Any) -> None:
        result = runner.invoke(
            cli_main.app, ["hook", source, "--hub-dir", str(hub)], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        assert result.output == ""  # stop's stdout would be read as a follow-up message

    # Cursor also runs ~/.claude/settings.json; that entry would parse the wrong format.
    hook("claude-code", hook_event_name="Stop", **cursor_base)
    hook("claude-code", hook_event_name="UserPromptSubmit", prompt="q", **cursor_base)
    assert spawned == [] and recalled == []

    hook("cursor", hook_event_name="stop", status="completed", **cursor_base)
    assert spawned[0][spawned[0].index("--source") + 1] == "cursor"
    assert spawned[0][spawned[0].index("--file") + 1] == str(transcript)
    assert "--complete" in spawned[0]


def test_locate_cursor_transcript_by_conversation_id_when_path_is_null(tmp_path: Path) -> None:
    path = _cursor_transcript(tmp_path, "proj", [])
    roots = {"cursor": tmp_path}
    assert locate_transcript(
        "cursor", {"conversation_id": "c1", "transcript_path": None}, roots
    ) == path


def test_cursor_prompt_hook_replies_additional_context_scoped_to_workspace_root(
    tmp_path: Path, monkeypatch
) -> None:
    sent: list[dict] = []

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **kwargs):
            sent.append(kwargs)
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "cursor:a", "content": "测试命令是 uv run pytest", "memory_at": 1_793_600_000},
            ]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["hook", "cursor", "--hub-dir", str(tmp_path)],
        input=json.dumps({
            "hook_event_name": "beforeSubmitPrompt", "cursor_version": "3.27.22",
            "workspace_roots": ["/w/repo", "/w/other"], "prompt": "测试命令?",
        }, ensure_ascii=False),
    )
    assert out.exit_code == 0, out.output
    reply = json.loads(out.output)
    assert list(reply) == ["additional_context"]
    assert reply["additional_context"].startswith("<relevant-memories>")
    assert "uv run pytest" in reply["additional_context"]
    assert sent[0]["workspace"] == "/w/repo" and sent[0]["query"] == "测试命令?"


def test_dsh_reader_skips_subagents_and_resumes_by_decompressed_offset(tmp_path: Path) -> None:
    spec = SOURCE_SPECS["dsh"]
    sub = tmp_path / "--w--" / "sub" / "session.v4.jsonl.zstd"
    _zstd_jsonl(sub, _dsh_events("sub", "/w", origin="subagent", prompt="subagent task"))
    assert spec.read(sub, 0)[0] == []

    path = tmp_path / "--w--" / "session-1" / "session.v4.jsonl.zstd"
    events = _dsh_events("session-1", "/w")
    _zstd_jsonl(path, events[:4])
    first, end = spec.read(path, 0)
    assert [m.role for m in first] == ["user", "assistant"]
    # The writer appends a frame: the cursor resumes after what was already read.
    more = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events[4:])
    path.write_bytes(path.read_bytes() + zstandard.ZstdCompressor().compress(more.encode()))
    rest, _ = spec.read(path, end)
    assert [m.role for m in rest] == ["assistant", "assistant"]
    assert rest[-1].text == "好的，记下 pnpm" and rest[-1].workspace == "/w"
    # A frame still being written decodes up to its last complete event.
    path.write_bytes(path.read_bytes()[:-5])
    assert len(spec.read(path, 0)[0]) >= 2


def test_trae_hook_recalls_then_captures_prompt_and_answer(tmp_path: Path, monkeypatch) -> None:
    recalled: list[dict] = []
    written: list[dict] = []

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **kwargs):
            recalled.append(kwargs)
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "trae:a", "content": "接口超时是 7 秒", "memory_at": 1_793_600_000},
            ]}

        async def write(self, **kwargs):
            written.append(kwargs)
            return {"status": "pending"}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    base = {"session_id": "t-1", "cwd": "/w/app", "workspace_roots": ["/w/app"]}

    def hook(source: str, **payload: Any):
        return runner.invoke(
            cli_main.app, ["hook", source, "--hub-dir", str(tmp_path)],
            input=json.dumps({**base, **payload}, ensure_ascii=False),
        )

    asked = hook("trae", hook_event_name="UserPromptSubmit", prompt="接口超时多少?")
    assert asked.exit_code == 0, asked.output
    context = json.loads(asked.output)["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    assert "接口超时是 7 秒" in context["additionalContext"]
    assert recalled[0]["workspace"] == "/w/app" and written == []

    answered = hook("trae", hook_event_name="Stop", last_assistant_message="是 7 秒")
    assert answered.exit_code == 0 and answered.output == ""
    (turn,) = written
    assert turn["messages"] == [
        {"role": "user", "content": "接口超时多少?"},
        {"role": "assistant", "content": "是 7 秒"},
    ]
    assert turn["session_id"] == "trae:t-1" and turn["workspace_id"] == "/w/app"
    assert turn["request_id"].startswith("memhub:trae:t-1:")
    # The prompt is spent: a second Stop writes nothing.
    hook("trae", hook_event_name="Stop", last_assistant_message="是 7 秒")
    assert len(written) == 1

    # Trae also runs ~/.claude/settings.json; the Claude entry must not double-inject.
    recalled.clear()
    ignored = hook("claude-code", hook_event_name="UserPromptSubmit", prompt="接口超时多少?")
    assert ignored.exit_code == 0 and ignored.output == "" and recalled == []


def test_kimi_code_prompt_hook_replies_plain_text(tmp_path: Path, monkeypatch) -> None:
    """kimi-code adds whatever the hook prints to the context and reads no JSON, so a
    JSON reply would reach the model as raw JSON."""

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def recall(self, **kwargs):
            empty = {k: [] for k in ("profile", "proactive", "normal", "raw", "schema", "catalog")}
            return {"memories": empty, "conversations": [
                {"session_id": "mcp:a", "content": "错误码前缀是 QX9", "memory_at": 1_793_600_000},
            ]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["hook", "kimi-code", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": "/w",
                          "prompt": "错误码前缀?"}, ensure_ascii=False),
    )
    assert out.exit_code == 0, out.output
    assert out.output.startswith("<relevant-memories>") and "QX9" in out.output
    assert "hookSpecificOutput" not in out.output

    # Kiro also reads plain stdout from UserPromptSubmit.
    kiro_out = runner.invoke(
        cli_main.app, ["hook", "kiro", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": "/w",
                          "prompt": "错误码前缀?"}, ensure_ascii=False),
    )
    assert kiro_out.exit_code == 0, kiro_out.output
    assert kiro_out.output.startswith("<relevant-memories>") and "QX9" in kiro_out.output

    # Copilot command hooks only read top-level additionalContext, and may omit hook_event_name.
    cp_out = runner.invoke(
        cli_main.app, ["hook", "copilot", "--hub-dir", str(tmp_path)],
        input=json.dumps({"sessionId": "s", "cwd": "/w", "prompt": "错误码前缀?"}, ensure_ascii=False),
    )
    assert cp_out.exit_code == 0, cp_out.output
    cp_reply = json.loads(cp_out.output)
    assert list(cp_reply) == ["additionalContext"]
    assert cp_reply["additionalContext"].startswith("<relevant-memories>")

    # ZCode sends camelCase hookEventName / sessionId and reads hookSpecificOutput.
    zc_out = runner.invoke(
        cli_main.app, ["hook", "zcode", "--hub-dir", str(tmp_path)],
        input=json.dumps({"hookEventName": "UserPromptSubmit", "sessionId": "s", "cwd": "/w",
                          "prompt": "错误码前缀?"}, ensure_ascii=False),
    )
    assert zc_out.exit_code == 0, zc_out.output
    assert "QX9" in json.loads(zc_out.output)["hookSpecificOutput"]["additionalContext"]


def test_locate_transcript_for_new_hosts(tmp_path: Path) -> None:
    roots = _seed_transcripts(tmp_path)
    assert locate_transcript("zcode", {"sessionId": "zc-1"}, roots) == roots["zcode"]
    assert locate_transcript("qoder", {"session_id": "qo-1"}, roots) == (
        roots["qoder"] / "-Users-me-qo" / "qo-1.jsonl"
    )
    assert locate_transcript("copilot", {"sessionId": "cp-1"}, roots) == (
        roots["copilot"] / "cp-1" / "events.jsonl"
    )
    assert locate_transcript("kiro", {"session_id": "sess_kiro-1"}, roots) == (
        roots["kiro"] / "ws-hash" / "sess_kiro-1" / "messages.jsonl"
    )
    assert locate_transcript("cline", {"session_id": "cl-1"}, roots) == (
        roots["cline"] / "cl-1" / "cl-1.messages.json"
    )
    assert locate_transcript("pi", {"session_id": "pi-1"}, roots) == (
        roots["pi"] / "--Users-me-pi--" / "2026-11-08T10-00-00-000Z_pi-1.jsonl"
    )
    assert locate_transcript("openclaw", {"session_id": "oc-1"}, roots) == (
        roots["openclaw"] / "main" / "sessions" / "oc-1.jsonl"
    )


def test_remember_writes_through_the_core_without_a_user_id(tmp_path: Path, monkeypatch) -> None:
    sent: list[dict] = []

    class FakeRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def write(self, **kwargs):
            sent.append(kwargs)
            return {"status": "pending", "request_id": kwargs["request_id"]}

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: FakeRemote())
    out = runner.invoke(
        cli_main.app, ["remember", "我在开发 super agent", "--hub-dir", str(tmp_path)]
    )
    assert out.exit_code == 0, out.output
    assert [s["content"] for s in sent] == ["我在开发 super agent"]
    assert sent[0]["request_id"].startswith("hub-cli:")
    assert "user_id" not in sent[0]
    assert json.loads(out.output)["status"] == "pending"


def test_git_worktree_submodule_subdir_and_remote_slug_normalization(
    tmp_path: Path,
) -> None:
    from memhub.hub.browse import (
        canonical_workspace,
        git_remote_slug,
        parse_origin_slug,
        workspace_sessions,
    )
    from memhub.types import MemoryNode

    assert parse_origin_slug("git@github.com:shibing624/memhub.git") == "shibing624/memhub"
    assert parse_origin_slug("https://github.com/shibing624/memhub.git/") == "shibing624/memhub"
    assert parse_origin_slug("https://dev.azure.com/org/proj/_git/repo") == "proj/repo"
    assert parse_origin_slug("/local/path/repo.git") is None
    assert parse_origin_slug("file:///tmp/repo.git") is None

    # Main repo with origin remote
    main_repo = tmp_path / "machine_a" / "memhub"
    (main_repo / ".git" / "worktrees" / "wt-feat").mkdir(parents=True)
    (main_repo / ".git" / "modules" / "vendor" / "sub").mkdir(parents=True)
    (main_repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:shibing624/memhub.git\n',
        encoding="utf-8",
    )
    subdir = main_repo / "memhub" / "hub"
    subdir.mkdir(parents=True)

    # Linked worktree
    wt = tmp_path / "machine_a" / "memhub-wt-feat"
    wt_sub = wt / "src"
    wt_sub.mkdir(parents=True)
    (wt / ".git").write_text(
        f"gitdir: {main_repo / '.git' / 'worktrees' / 'wt-feat'}\n",
        encoding="utf-8",
    )

    # Submodule
    submod = main_repo / "vendor" / "sub"
    submod.mkdir(parents=True)
    (submod / ".git").write_text(
        f"gitdir: {main_repo / '.git' / 'modules' / 'vendor' / 'sub'}\n",
        encoding="utf-8",
    )

    assert canonical_workspace(str(subdir)) == str(main_repo)
    assert canonical_workspace(str(wt)) == str(main_repo)
    assert canonical_workspace(str(wt_sub)) == str(main_repo)
    assert canonical_workspace(str(submod)) == str(main_repo)
    assert git_remote_slug(wt_sub) == "shibing624/memhub"

    # Second machine checkout in a differently named folder with HTTPS remote
    other_clone = tmp_path / "machine_b" / "my-fork-dir"
    (other_clone / ".git").mkdir(parents=True)
    (other_clone / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/shibing624/memhub.git\n',
        encoding="utf-8",
    )

    nodes = [
        MemoryNode(
            node_id="n1",
            app_id="default",
            user_id="default",
            content="worktree session",
            layer=Layer.L0_RAW,
            session_id="cursor:s-wt",
            custom={"workspace_id": str(wt_sub)},
        ),
        MemoryNode(
            node_id="n2",
            app_id="default",
            user_id="default",
            content="synced from another machine path that does not exist here",
            layer=Layer.L0_RAW,
            session_id="claude-code:s-remote-missing",
            custom={"workspace_id": "/nonexistent/alice/work/memhub"},
        ),
        MemoryNode(
            node_id="n3",
            app_id="default",
            user_id="default",
            content="unrelated project",
            layer=Layer.L0_RAW,
            session_id="codex:s-other",
            custom={"workspace_id": "/nonexistent/alice/work/other-repo"},
        ),
    ]
    # Querying from a subdirectory of main_repo matches both the worktree session
    # and the synced session from another machine's `/nonexistent/alice/work/memhub`.
    assert set(workspace_sessions(nodes, str(subdir))) == {
        "cursor:s-wt",
        "claude-code:s-remote-missing",
    }
    # Querying from other_clone matches via shared origin slug `shibing624/memhub`.
    assert "cursor:s-wt" in workspace_sessions(nodes, str(other_clone))


async def test_progressive_disclosure_l0_source_id_lookup_and_cli(
    tmp_path: Path, fake_embed, fake_llm, fake_jev, monkeypatch
) -> None:
    hub = tmp_path / "hub"
    core = _memory_client(hub, fake_embed, fake_llm, fake_jev)
    async with _attached_core(hub, core) as (_app, remote):
        r1 = await remote.write(
            messages=[
                {"role": "user", "content": "第一轮：数据库连接池设成 20"},
                {"role": "assistant", "content": "已设为 20"},
            ],
            session_id="codex:sess-pd",
            memory_at=1_793_000_100,
            request_id="pd-1",
            wait=True,
            workspace_id="/w/app",
        )
        r2 = await remote.write(
            messages=[
                {"role": "user", "content": "第二轮：把超时改为 15 秒"},
                {"role": "assistant", "content": "已改为 15 秒"},
            ],
            session_id="codex:sess-pd",
            memory_at=1_793_000_200,
            request_id="pd-2",
            wait=True,
            workspace_id="/w/app",
        )
        l0_id = r2["result"]["memory_id"]
        short_id = l0_id[:8]

        expanded = await remote.get_conversation(short_id, context_turns=1)
        assert expanded["source_id"] == short_id
        assert expanded["memory_id"] == l0_id
        assert len(expanded["conversations"]) == 2
        assert [c["is_anchor"] for c in expanded["conversations"]] == [False, True]
        assert "第一轮" in expanded["conversations"][0]["content"]
        assert "第二轮" in expanded["conversations"][1]["content"]

    class FakeSourceRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get_conversation(self, sid: str, *, context_turns: int = 1):
            assert sid == short_id and context_turns == 1
            return expanded

    monkeypatch.setattr(cli_hub, "make_client", lambda _h: FakeSourceRemote())
    cli_out = await asyncio.to_thread(
        runner.invoke,
        cli_main.app,
        ["recall", "--source-id", short_id, "--hub-dir", str(hub)],
    )
    assert cli_out.exit_code == 0, cli_out.output
    assert "[当前来源]" in cli_out.output
    assert "第二轮：把超时改为 15 秒" in cli_out.output
    assert r1["result"]["memory_id"][:8] in cli_out.output


def test_detect_installed_hosts_and_auto_install(tmp_path: Path, monkeypatch) -> None:
    from memhub.hub.hosts import detect_installed_hosts

    home = tmp_path / "home"
    hub = tmp_path / "hub"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_bin.chmod(0o755)

    detected = detect_installed_hosts(home=home, path_env=str(bin_dir))
    assert detected == ["codex", "cursor"]

    monkeypatch.setattr(
        cli_hub.hub_hosts,
        "detect_installed_hosts",
        lambda **_kw: ["cursor"],
    )
    monkeypatch.setattr(
        cli_hub.hub_hosts,
        "install_host",
        lambda host, hub_dir=None: cli_hub.hub_hosts.HOSTS[host] and {"host": host},
    )
    out = runner.invoke(cli_main.app, ["install-host", "--hub-dir", str(hub)])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output) == [{"host": "cursor"}]


async def test_private_tags_secret_redaction_and_base64_image_elision(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    from memhub.hub.redact import sanitize_text, strip_image_payloads

    b64 = "iVBORw0KGgoAAAANSUhEUgAAAAUA" * 20
    raw_text = (
        "公开配置说明\n"
        "<private>这段银行卡号 622202123456789 绝不能入库</private>\n"
        f"截图：![shot](data:image/png;base64,{b64})\n"
        "OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmnop\n"
        "ANTHROPIC_API_KEY=sk-ant-api03-1234567890abcdefghijklmnop\n"
        "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz\n"
        "DATABASE_URL=postgres://admin:superSecretPass123@db.internal:5432/prod\n"
        "AWS_KEY=AKIAIOSFODNN7EXAMPLE"
    )

    # Default: <private> and base64 handling always run; secrets are preserved, because
    # a personal assistant must be able to remember a key it was told to remember.
    plain = sanitize_text(raw_text)
    assert "绝不能入库" not in plain
    assert b64 not in plain
    assert f"data:image/png;base64,<elided {len(b64)} chars>" in plain
    assert "sk-proj-1234567890abcdefghijklmnop" in plain
    assert "AKIAIOSFODNN7EXAMPLE" in plain

    # Opt-in redaction (MEMHUB_REDACT_SECRETS=true) is the only path that masks them.
    cleaned = sanitize_text(raw_text, redact=True)
    assert "绝不能入库" not in cleaned
    assert b64 not in cleaned
    assert f"data:image/png;base64,<elided {len(b64)} chars>" in cleaned
    assert "sk-proj-" not in cleaned and "<redacted type='openai_key'/>" in cleaned
    assert "sk-ant-" not in cleaned and "<redacted type='anthropic_key'/>" in cleaned
    assert "ghp_" not in cleaned and "<redacted type='github_pat'/>" in cleaned
    assert "superSecretPass123" not in cleaned
    assert "postgres://admin:<redacted type='url_credentials'/>@db.internal:5432/prod" in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned and "<redacted type='aws_access_key'/>" in cleaned

    # Multimodal content block dicts
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        {"type": "image", "file": {"base64": b64, "type": "image/jpeg"}},
        {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{b64}"}},
    ]
    stripped = strip_image_payloads(blocks)
    assert b64 not in json.dumps(stripped)
    assert stripped[0]["source"]["elided"] == "image data withheld"
    assert stripped[1]["file"]["elided"] == "image data withheld"
    assert stripped[2]["image_url"]["elided"] == "image data withheld"

    # End-to-end through Hub Core write: pure <private> message is skipped without error
    hub = tmp_path / "hub"
    core = _memory_client(hub, fake_embed, fake_llm, fake_jev)
    async with _attached_core(hub, core) as (_app, remote):
        skipped = await remote.write(
            content="<private>only secret</private>",
            request_id="priv-only",
            wait=True,
        )
        assert skipped["status"] == "done"
        assert skipped["result"] == {"skipped": "private"}
        # Only the tagged span is dropped: the assistant's answer is kept.
        kept_turn = await remote.write(
            messages=[
                {"role": "user", "content": "<private>整段私密提问</private>"},
                {"role": "assistant", "content": "好的，我记住了"},
            ],
            session_id="codex:priv",
            request_id="priv-turn",
            wait=True,
        )
        assert kept_turn["result"]["memory_id"]
        rows = [
            node
            for node in await core.list(
                user_id=HUB_USER_ID, limit=50, include_history=True
            )
            if node.session_id == "codex:priv"
        ]
        assert rows, "the answer outside <private> is kept"
        assert not any("整段私密提问" in node.content for node in rows)
        assert any("我记住了" in node.content for node in rows)
    await core.aclose()


async def test_remembered_api_key_is_actually_stored(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    """The product case: the user says "remember my key", so it must reach the store.

    Automatic secret redaction is off by default, and no ``<private>`` tag is
    involved; turning ``redact_secrets`` on is what masks it.
    """
    key = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    for enabled, expect_key in ((False, True), (True, False)):
        hub = tmp_path / f"hub-{enabled}"
        core = _memory_client(hub, fake_embed, fake_llm, fake_jev)
        core.settings.redact_secrets = enabled
        async with _attached_core(hub, core) as (_app, remote):
            await remote.write(
                content=f"我的全局 OpenAI key 是 {key}，以后都用它",
                request_id=f"key-{enabled}",
                wait=True,
            )
            stored = "\n".join(
                node.content
                for node in await core.list(
                    user_id=HUB_USER_ID, limit=50, include_history=True
                )
            )
            if expect_key:
                assert key in stored, "a key the user asked to remember must be kept"
            else:
                assert key not in stored
                assert "<redacted type='openai_key'/>" in stored
        await core.aclose()


def test_privacy_and_redaction_edge_cases() -> None:
    """The shapes a naive implementation gets wrong: prose after an image payload,
    an unclosed <private>, and words that look like a hex id."""
    from memhub.hub.mcp import _looks_like_l0_id
    from memhub.hub.redact import elide_data_image_urls, sanitize_messages, sanitize_text

    b64 = "iVBORw0KGgoAAAANSUhEUgAAAAUA" * 20
    # The base64 body must stop at its own delimiter; a `\s` in the class ran past
    # the newline and deleted the English paragraph that followed the screenshot.
    text = (
        f"Here is the screenshot: data:image/png;base64,{b64}\n"
        "The deployment uses canary rollouts with automatic rollback.\n"
        "See ops docs."
    )
    elided = elide_data_image_urls(text)
    assert "canary rollouts with automatic rollback" in elided
    assert "See ops docs." in elided
    assert b64 not in elided

    # An unclosed <private> keeps nothing after it: the user opened a private region.
    assert sanitize_text("a <private>secret tail") == "a"
    assert sanitize_text("a <private>tail</private> b") == "a  b"
    assert sanitize_text("a <PRIVATE>tail</PRIVATE> b") == "a  b"

    # Only the tagged span goes; the assistant's answer survives.
    kept = sanitize_messages(
        [
            {"role": "user", "content": "<private>x</private>"},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert kept == [{"role": "assistant", "content": "ok"}]
    # A partially private turn keeps the rest.
    assert sanitize_messages([{"role": "user", "content": "<private>x</private>保留"}]) == [
        {"role": "user", "content": "保留"}
    ]
    # Nothing left at all -> empty, so the Core reports the write as skipped.
    assert sanitize_messages([{"role": "user", "content": "<private>x</private>"}]) == []

    # Ids: 8+ hex chars with a letter and a digit. `decade` is a word, `1234567890`
    # is a number, and either would have been answered with a 404 instead of a search.
    assert _looks_like_l0_id("37c85fe9") == "37c85fe9"
    assert _looks_like_l0_id("L0 37c85fe9") == "37c85fe9"
    assert _looks_like_l0_id("decade") == ""
    assert _looks_like_l0_id("1234567890") == ""
    assert _looks_like_l0_id("authentication bug") == ""


async def test_doctor_command_and_hook_health_warning_on_failed_writes(
    tmp_path: Path, fake_embed, fake_llm, fake_jev, monkeypatch
) -> None:
    hub = tmp_path / "hub"
    core = _memory_client(hub, fake_embed, fake_llm, fake_jev)

    class FailingClient:
        settings = core.settings
        factory = core.factory

        async def add(self, **_kwargs):
            raise RuntimeError("401 invalid_api_key")

        async def search(self, **kwargs):
            return await core.search(**kwargs)

        async def search_conversation(self, **kwargs):
            return await core.search_conversation(**kwargs)

    app = create_hub_app(
        api_key="secret",
        hub_dir=hub,
        client=FailingClient(),  # type: ignore[arg-type]
        settings=core.settings,
        queue_retry_seconds=0.01,
    )
    async with app.router.lifespan_context(app):
        app.state.queue._max_attempts = 1
        transport = httpx.ASGITransport(app=app)
        async with HubClient(
            base_url="http://hub", api_key="secret", transport=transport
        ) as remote:
            job = await remote.write(
                content="测试写入失败告警", request_id="fail-1", wait=True
            )
            assert job["status"] == "failed"

            recall_res = await remote.recall(query="随便问问")
            assert recall_res.get("warnings")
            assert "1 条记忆写入失败" in recall_res["warnings"][0]
            assert "401 invalid_api_key" in recall_res["warnings"][0]
            status_res = await remote.status()
            assert status_res["writes"]["failed"] == 1
            assert "401 invalid_api_key" in status_res["writes"]["last_error"]
            assert status_res["warnings"]

        class FakeWarnRemote:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return None

            async def recall(self, **_kw):
                return recall_res

            async def status(self):
                return status_res

        monkeypatch.setattr(cli_hub, "make_client", lambda _h: FakeWarnRemote())
        # Both the hook and doctor are sync commands; run them off the loop they
        # would otherwise have to call asyncio.run() from.
        hook_out = await asyncio.to_thread(
            runner.invoke,
            cli_main.app,
            ["hook", "claude-code", "--hub-dir", str(hub)],
            input=json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "随便问问"},
                ensure_ascii=False,
            ),
        )
        assert hook_out.exit_code == 0, hook_out.output
        injected = json.loads(hook_out.output)["hookSpecificOutput"]["additionalContext"]
        assert "⚠️ MemHub 后台有 1 条记忆写入失败" in injected
        assert "memhub doctor" in injected

        # `doctor` probes the daemon over its socket; this Core lives inside the test
        # process, so hand it the live status the way the socket probe would.
        monkeypatch.setattr(cli_hub, "daemon_status", lambda _root: status_res)
        home = tmp_path / "home"
        (home / ".cursor").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        empty_path = tmp_path / "empty-bin"
        empty_path.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setenv("MEMHUB_JEV_API_KEY", "k")
        monkeypatch.setenv("MEMHUB_LLM_API_KEY", "k")
        monkeypatch.setenv("MEMHUB_EMBED_API_KEY", "k")
        doc_out = await asyncio.to_thread(
            runner.invoke, cli_main.app, ["doctor", "--json", "--hub-dir", str(hub)]
        )
        assert doc_out.exit_code == 0, doc_out.output
        report = json.loads(doc_out.output)
        by_name = {c["name"]: c for c in report["checks"]}
        assert report["ok"] is True
        assert by_name["Config & keys"]["status"] == "ok"
        assert by_name["Core daemon"]["status"] == "ok"
        assert by_name["Write queue"]["status"] == "warn"
        assert "1 failed write(s)" in by_name["Write queue"]["detail"]
        # Two hosts exist but were never wired: doctor points at the install command.
        assert by_name["Detected hosts"]["status"] == "warn"
        assert "memhub install-host" in by_name["Detected hosts"]["detail"]
    await core.aclose()


async def test_failed_writes_auto_recover_on_later_success_and_restart_without_poison_loop(
    tmp_path: Path,
) -> None:
    state = {"outage": True, "poison": True}
    calls: dict[str, int] = {}

    async def processor(payload: dict) -> dict:
        rid = payload["request_id"]
        calls[rid] = calls.get(rid, 0) + 1
        if state["outage"]:
            raise RuntimeError("502 Bad Gateway")
        if rid == "r-poison" and state["poison"]:
            raise RuntimeError("deterministic bad payload")
        return {"ok": rid}

    db_path = tmp_path / "hub.db"
    queue = HubWriteQueue(db_path, processor, max_attempts=2, retry_base_seconds=0.01)
    queue.start()

    def submit(rid: str) -> dict:
        return queue.submit(
            {
                "content": rid,
                "messages": None,
                "session_id": "s",
                "memory_at": 100,
                "request_id": rid,
                "raw_only": False,
            }
        )

    submit("r-transient")
    submit("r-poison")
    assert (await queue.wait("r-transient"))["status"] == "failed"
    assert (await queue.wait("r-poison"))["status"] == "failed"

    # Outage ends and a new turn succeeds: r-transient auto-heals without manual
    # `memhub retry`; r-poison gets 1 probe, fails (attempts=3), and stops looping.
    state["outage"] = False
    submit("r-healthy-1")
    assert (await queue.wait("r-healthy-1"))["status"] == "done"
    for _ in range(200):
        if (
            queue.get("r-transient")["status"] == "done"
            and queue.get("r-poison")["status"] == "failed"
            and queue.get("r-poison")["attempts"] == 3
        ):
            break
        await asyncio.sleep(0.01)
    assert queue.get("r-transient")["status"] == "done"
    assert (queue.get("r-poison")["status"], queue.get("r-poison")["attempts"]) == (
        "failed",
        3,
    )

    # Another healthy write does NOT re-trigger r-poison (no infinite loop).
    submit("r-healthy-2")
    assert (await queue.wait("r-healthy-2"))["status"] == "done"
    await asyncio.sleep(0.05)
    assert calls["r-poison"] == 3
    await queue.close()

    # Restarting the Core after a code/config fix automatically drains prior failures.
    state["poison"] = False
    restarted = HubWriteQueue(db_path, processor, max_attempts=2, retry_base_seconds=0.01)
    restarted.start()
    assert (await restarted.wait("r-poison"))["status"] == "done"
    assert restarted.failure_summary() == (0, None)
    await restarted.close()


def test_dsh_mcp_migrates_legacy_manager_and_bare_entry_to_insert_row(
    tmp_path: Path,
) -> None:
    """DeepSeek Harness requires ``- insert: [ {id: mcp-memhub, name: @deepseek-ai/dsh-mcp-client, ...} ]``.
    Legacy ``mcp-manager`` entries and bare non-insert ``mcp-memhub`` entries are upgraded cleanly,
    while coexisting ``insert`` entries for other MCP servers remain untouched."""
    home = tmp_path / "home"
    hub = tmp_path / "hub"
    patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text(
        "# User profile patch\n"
        "- id: ui-theme\n"
        '  name: "@deepseek-ai/dsh-client-ui-theme"\n'
        "  config:\n"
        "    preference: dark\n"
        "- insert:\n"
        "  - id: mcp-github\n"
        "    name: '@deepseek-ai/dsh-mcp-client'\n"
        "    config:\n"
        "      serverName: github\n"
        "      transport: stdio\n"
        "      command: npx\n"
        "- id: mcp-manager\n"
        "  name: '@deepseek-ai/dsh-mcp-manager'\n"
        "  config:\n"
        "    servers:\n"
        "      memhub:\n"
        "        transport: stdio\n"
        "        command: /old/python\n"
        "- id: mcp-memhub\n"
        "  name: '@deepseek-ai/dsh-mcp-client'\n"
        "  config:\n"
        "    serverName: memhub\n"
        "    transport: stdio\n"
        "    command: /bare/python\n",
        encoding="utf-8",
    )

    # Legacy/bare shapes are NOT counted as validly installed before migration.
    assert hosts_status(home=home)["dsh"]["mcp"] is False

    install_host("dsh", hub_dir=hub, home=home)
    install_host("dsh", hub_dir=hub, home=home)  # idempotent
    assert hosts_status(home=home)["dsh"]["mcp"] is True

    parsed = yaml.safe_load(patch.read_text(encoding="utf-8"))
    top_ids = [item.get("id") for item in parsed if "id" in item]
    assert top_ids == ["ui-theme"]
    inserted = [row for item in parsed if "insert" in item for row in item["insert"]]
    assert [row["id"] for row in inserted] == ["mcp-github", "mcp-memhub"]
    memhub_row = inserted[1]
    assert memhub_row["name"] == "@deepseek-ai/dsh-mcp-client"
    assert memhub_row["config"]["serverName"] == "memhub"
    assert memhub_row["config"]["transport"] == "stdio"
    assert memhub_row["config"]["args"] == ["-m", "memhub.hub.mcp", "--hub-dir", str(hub.resolve())]

    assert uninstall_host("dsh", home=home)["mcp"] is True
    assert hosts_status(home=home)["dsh"]["mcp"] is False
    after = yaml.safe_load(patch.read_text(encoding="utf-8"))
    assert [row["id"] for item in after if "insert" in item for row in item["insert"]] == [
        "mcp-github"
    ]




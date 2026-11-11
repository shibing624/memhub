# -*- coding: utf-8 -*-
"""Regression suite for budgeted progressive disclosure, live project view,
same-source L0 novelty compression, exact memory_get expansion, structured
execution signals, and device-local file drift hints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeJev, FakeLLMClient

from memhub import MemoryClient
from memhub.cli.hub import build_hub_prompt_context
from memhub.coding.preproc import extract_tool_signals
from memhub.config import Settings
from memhub.hub.browse import inspect_local_procedure_files
from memhub.hub.client import HubClient
from memhub.hub.config import CursorStore
from memhub.hub.server import (
    create_hub_app,
    project_sessions_and_view,
    retired_claims_by_source,
)
from memhub.hub.sources import SOURCE_SPECS, build_turns, default_source_roots, scan_file
from memhub.retrieval.hybrid_engine import novel_source_excerpt
from memhub.types import Layer, MemoryNode


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


async def test_mixed_tool_session_keeps_concrete_assistant_facts_and_toolless_architecture_session_keeps_decisions(
    tmp_storage: str, fake_embed
) -> None:
    """1) Tool presence must NOT blindly drop concrete assistant-owned facts in a
    mixed/lookup session. 2) A tool-less engineering architecture conversation
    still records user-confirmed rules & technical decisions."""

    def extract_fn(system: str, user: str) -> dict:
        # Prompt instructs omitting operational progress narration while keeping concrete details.
        assert "transient operational action or progress narration" in system
        if "hotel" in user:
            return {
                "facts": [
                    {
                        "content": "The user prefers quiet boutique hotels near subway lines.",
                        "speaker": "user",
                    },
                    {
                        "content": "The assistant recommended Parkside Hotel at 88 West Rd (+81-3-5555-0199).",
                        "speaker": "assistant",
                    },
                ],
                "identity": [],
                "intentions": [],
                "is_ephemeral": False,
                "basic_info": {},
            }
        return {
            "facts": [
                {
                    "content": "项目约定：所有账本表金额字段统一用整数分（BIGINT cents），严禁用 FLOAT。",
                    "speaker": "user",
                },
            ],
            "identity": [],
            "intentions": [],
            "is_ephemeral": False,
            "basic_info": {},
        }

    c = MemoryClient(
        settings=Settings(storage_dir=tmp_storage, mode="system1"),
        embed=fake_embed,
        llm=FakeLLMClient({"extract": extract_fn, "json": {"candidates": []}}),
        jev=FakeJev({"scene": "CHAT"}),
    )
    mixed = await c.add(
        messages=[
            {"role": "user", "content": "Find me a quiet boutique hotel near subway lines in Tokyo"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-web-1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query": "Tokyo quiet hotel"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-web-1", "content": "Parkside Hotel 88 West Rd"},
            {
                "role": "assistant",
                "content": "I recommend Parkside Hotel at 88 West Rd (+81-3-5555-0199).",
            },
        ],
        user_id="u",
    )
    stored = c.factory.vector.get_by_ids(mixed.extra_node_ids)
    owners = {n.owner: n.content for n in stored.values()}
    assert "user" in owners and "agent" in owners
    assert "+81-3-5555-0199" in owners["agent"]

    # Tool-less architecture discussion in a workspace.
    arch = await c.add(
        messages=[
            {"role": "user", "content": "定一下架构规矩：账本表金额一律用 BIGINT 存分，严禁 FLOAT。"},
            {"role": "assistant", "content": "明白，金额统一使用 BIGINT 整数分存储。"},
        ],
        user_id="default",
        session_id="cursor:arch-1",
        workspace_id="/w/ledger",
    )
    assert arch.scene == "chat" and len(arch.extra_node_ids) == 1
    await c.aclose()


async def test_core_memory_get_resolves_full_and_short_prefix_and_guards_errors(
    tmp_path: Path, fake_embed
) -> None:
    proc_draft = {
        "task": "如何在 ledger-svc 中验证 SQLite WAL 配置",
        "solution": "连接建立后执行 PRAGMA journal_mode; 断言返回 wal，严禁在单测中连接 /data/prod.db。",
        "search_keys": ["sqlite wal pragma check"],
        "boundary_envs": "Python 3.12 + SQLite 3.45",
        "boundary_scope": "project",
    }
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "store"), mode="system1"),
        embed=fake_embed,
        llm=FakeLLMClient({"json": {"candidates": [proc_draft]}}),
        jev=FakeJev({"scene": "CODING", "0:action": "ADD_SUPPLEMENT"}),
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core, settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret", transport=transport) as hub:
            receipt = await hub.write(
                messages=[
                    {"role": "user", "content": "怎么在单测里验证 WAL？"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {"name": "Read", "arguments": '{"path": "ledger/db.py"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "tc1", "content": "PRAGMA journal_mode=WAL"},
                    {"role": "assistant", "content": "执行 PRAGMA journal_mode 断言返回 wal。"},
                ],
                session_id="cursor:s1",
                request_id="req-proc-1",
                workspace_id="/w/ledger",
                branch="main",
                wait=True,
            )
            proc_id = receipt["result"]["procedure_ids"][0]
            raw_id = receipt["result"]["memory_id"]

            # Exact full ID and 8-char prefix both return the complete procedure.
            full = await hub.get_memory(proc_id)
            short = await hub.get_memory(proc_id[:8])
            assert full["memory_id"] == short["memory_id"] == proc_id
            assert short["procedure"]["solution"].startswith("连接建立后执行 PRAGMA")
            assert short["procedure"]["boundary_envs"] == "Python 3.12 + SQLite 3.45"
            assert short["procedure"]["files"] == ["ledger/db.py"]

            # L0_RAW ids belong to conversation_search, not memory_get.
            with pytest.raises(httpx.HTTPStatusError) as exc_raw:
                await hub.get_memory(raw_id)
            assert exc_raw.value.response.status_code == 404

            # Prefix shorter than 8 chars does not fuzzy-scan.
            with pytest.raises(httpx.HTTPStatusError) as exc_short:
                await hub.get_memory(proc_id[:6])
            assert exc_short.value.response.status_code == 404
    await core.aclose()


async def test_live_project_view_surfaces_wording_mismatched_rules_and_invalidates_immediately(
    tmp_path: Path,
) -> None:
    """When the user asks '给对账导出接口写一个回归用例' without mentioning '生产库' or 'tmp_path'
    (semantic + BM25 score = 0.0, so dynamic search returns empty!), the workspace's
    Project View still surfaces the active project testing rule and procedure index,
    stays isolated from other repos, and immediately updates/removes on edit or delete."""
    rule_text = "项目红线：测试永远使用 pytest tmp_path，严禁读写 /data/ledger/prod.db 生产库"
    other_repo_rule = "前端项目约定：组件统一使用 CSS Modules"

    class OrthogonalEmbed:
        """Distinct texts map to orthogonal basis vectors (cosine = 0.0 unless identical)."""

        def __init__(self) -> None:
            self._axes: dict[str, int] = {}

        def _vec(self, text: str) -> list[float]:
            axis = self._axes.setdefault(text, len(self._axes) % 64)
            v = [0.0] * 64
            v[axis] = 1.0
            return v

        async def embed(self, text: str) -> list[float]:
            return self._vec(text)

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [self._vec(t) for t in texts]

        async def embed_queued(self, text: str) -> list[float]:
            return self._vec(text)

    def extract_fn(system: str, user: str) -> dict:
        if "严禁读写" in user:
            return {"facts": [{"content": rule_text, "speaker": "user"}], "identity": [], "intentions": []}
        if "CSS Modules" in user:
            return {"facts": [{"content": other_repo_rule, "speaker": "user"}], "identity": [], "intentions": []}
        return {"facts": [], "identity": [], "intentions": []}

    proc_draft = {
        "task": "跨币种汇率精度截断排查",
        "solution": "先转 Decimal 再按目标币种 quantize，禁止中途转 float。",
        "search_keys": ["fx rounding"],
        "boundary_envs": "",
        "boundary_scope": "project",
    }
    core = MemoryClient(
        settings=Settings(
            storage_dir=str(tmp_path / "store"),
            mode="system1",
            embed_dim=64,
            hub_inject_max_chars=2400,
            hub_procedure_full_score=0.45,
        ),
        embed=OrthogonalEmbed(),
        llm=FakeLLMClient({"extract": extract_fn, "json": {"candidates": [proc_draft]}}),
        jev=FakeJev({"scene": "CODING", "0:action": "ADD_SUPPLEMENT"}),
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core, settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret", transport=transport) as hub:
            w_rule = await hub.write(
                messages=[
                    {"role": "user", "content": rule_text},
                    {"role": "assistant", "content": "已记下测试红线。"},
                ],
                session_id="claude-code:s-rule",
                request_id="r-rule",
                workspace_id="/w/ledger-svc",
                wait=True,
            )
            rule_id = w_rule["result"]["extra_node_ids"][0]

            w_proc = await hub.write(
                messages=[
                    {"role": "user", "content": "修一下汇率精度问题"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "edit_file", "arguments": '{"path": "ledger/fx.py"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                    {"role": "assistant", "content": "已改用 Decimal quantize。"},
                ],
                session_id="claude-code:s-proc",
                request_id="r-proc",
                workspace_id="/w/ledger-svc",
                branch="main",
                wait=True,
            )
            proc_id = w_proc["result"]["procedure_ids"][0]

            await hub.write(
                messages=[
                    {"role": "user", "content": other_repo_rule},
                    {"role": "assistant", "content": "好的"},
                ],
                session_id="claude-code:s-web",
                request_id="r-web",
                workspace_id="/w/web-ui",
                wait=True,
            )

            # Query has zero lexical or cosine overlap (dynamic recall is completely empty!).
            prompt = "给对账导出接口写一个回归用例"
            recalled = await hub.recall(query=prompt, workspace="/w/ledger-svc")
            assert recalled["memories"]["normal"] == []
            assert recalled["memories"]["procedures"] == []
            assert recalled["inject_policy"]["max_chars"] == 2400
            ctx = build_hub_prompt_context(recalled, prompt, workspace="/w/ledger-svc")
            assert "【项目约定与目录 Project View】" in ctx
            assert "严禁读写 /data/ledger/prod.db 生产库" in ctx
            assert f"id={proc_id[:8]}" in ctx
            assert "CSS Modules" not in ctx
            assert len(ctx) <= 2400

            # Updating the rule takes effect on the VERY NEXT recall (no 24h stale digest cache).
            updated = await hub.update_memory(
                rule_id, "项目新红线：集成测试统一连 Docker 临时库，严禁连生产库"
            )
            recalled_after_edit = await hub.recall(query=prompt, workspace="/w/ledger-svc")
            ctx_after_edit = build_hub_prompt_context(
                recalled_after_edit, prompt, workspace="/w/ledger-svc"
            )
            assert "Docker 临时库" in ctx_after_edit
            assert "pytest tmp_path" not in ctx_after_edit

            # Deleting the updated rule removes it immediately from Project View.
            await hub.delete_memory(updated["memory_id"])
            recalled_after_del = await hub.recall(query=prompt, workspace="/w/ledger-svc")
            ctx_after_del = build_hub_prompt_context(
                recalled_after_del, prompt, workspace="/w/ledger-svc"
            )
            assert "Docker 临时库" not in ctx_after_del
    await core.aclose()


def test_same_source_l0_compresses_duplicate_but_preserves_uncovered_details() -> None:
    """When an L0 batch contains both an already-recalled fact AND an uncovered
    release-freeze date/condition, same-source compression drops the repeated claim
    while preserving the uncovered date/condition."""
    l0_id = "88aa99bb-1111-2222-3333-444455556666"
    l0_batch = (
        "[user]: 以后这个项目的测试命令统一用 uv run pytest -q。\n"
        "[assistant]: 好的，统一使用 uv run pytest -q。\n"
        "[user]: 另外注意 2026-11-20 封版窗口期间冻结 schema 迁移脚本，紧急回滚找 oncall 李明。\n"
        "[assistant]: 明白，2026-11-20 封版期间不跑迁移，回滚联系李明。"
    )
    recall_payload = {
        "memories": {
            "profile": [],
            "proactive": [],
            "normal": [
                {
                    "memory_id": "fact-11112222",
                    "content": "项目的测试命令统一用 uv run pytest -q",
                    "category": "fact",
                    "layer": "L1_FACT",
                    "owner": "user",
                    "score": 0.65,
                    "source_node_id": l0_id,
                    "source_ids": [l0_id],
                }
            ],
            "raw": [],
            "schema": [],
            "catalog": [],
            "procedures": [],
        },
        "conversations": [
            {
                "memory_id": l0_id,
                "session_id": "cursor:s1",
                "content": l0_batch,
                "memory_at": 1_794_000_000,
            }
        ],
    }
    ctx = build_hub_prompt_context(recall_payload, "封版窗口迁移和回滚怎么处理？")
    assert "uv run pytest -q" in ctx
    assert "2026-11-20" in ctx and "李明" in ctx

    # Conversely, when the L0 turn contains ONLY the exact statement already recalled,
    # it is not duplicated in 历史对话.
    redundant_payload = {
        **recall_payload,
        "conversations": [
            {
                "memory_id": l0_id,
                "session_id": "cursor:s1",
                "content": "[user]: 项目的测试命令统一用 uv run pytest -q",
                "memory_at": 1_794_000_000,
            }
        ],
    }
    compact_ctx = build_hub_prompt_context(redundant_payload, "测试命令是什么？")
    assert "uv run pytest -q" in compact_ctx
    assert "历史对话" not in compact_ctx

    # When a compound L0 turn mixes a superseded value ("2500ms") with an uncovered
    # detail ("联系赵敏"), the retired clause is stripped so "2500ms" never resurrects
    # alongside the new "1500ms" rule, while "联系赵敏" still survives!
    compound_l0 = (
        "[user]: 默认超时设为 2500ms，故障升级请优先联系值班赵敏。\n"
        "[assistant]: 好的，超时设为 2500ms，故障联系赵敏。"
    )
    compound_payload = {
        "memories": {
            "profile": [],
            "proactive": [],
            "normal": [
                {
                    "memory_id": "rule-new-1500",
                    "content": "默认超时已改为 1500ms",
                    "category": "fact",
                    "layer": "L1_FACT",
                    "owner": "user",
                    "score": 0.70,
                    "source_node_id": l0_id,
                    "source_ids": [l0_id],
                }
            ],
            "raw": [],
            "schema": [],
            "catalog": [],
            "procedures": [],
        },
        "conversations": [
            {
                "memory_id": l0_id,
                "session_id": "codex:s2",
                "content": compound_l0,
                "memory_at": 1_794_000_100,
                "retired_claims": ["默认超时设为 2500ms"],
            }
        ],
    }
    evolved_ctx = build_hub_prompt_context(compound_payload, "故障升级联系谁？超时是多少？")
    assert "1500ms" in evolved_ctx
    assert "2500ms" not in evolved_ctx
    assert "赵敏" in evolved_ctx


def test_budget_demotes_full_procedures_to_index_without_slicing_commands() -> None:
    long_solution = (
        "Step 1: run `uv run alembic upgrade head --sql > migration.sql`\n"
        + ("Step detail: check lock timeout and replica lag before apply.\n" * 35)
        + "Final constraint: NEVER run DROP COLUMN online without dual-write."
    )
    payload = {
        "memories": {
            "profile": [],
            "proactive": [],
            "normal": [],
            "raw": [],
            "schema": [],
            "catalog": [],
            "procedures": [
                {
                    "memory_id": f"proc-{idx:04d}-aaaa-bbbb",
                    "content": f"Task {idx}\n{long_solution}",
                    "category": "fact",
                    "kind": "procedure",
                    "layer": "L1_FACT",
                    "score": 0.75 - idx * 0.05,
                    "source_ids": [f"raw-{idx:04d}-cccc"],
                    "procedure": {
                        "task": f"Zero-downtime migration playbook #{idx}",
                        "solution": long_solution,
                        "boundary_scope": "project",
                        "boundary_envs": "PostgreSQL 16",
                        "branch": "main",
                        "files": ["db/migrate.py"],
                    },
                }
                for idx in range(3)
            ],
        },
        "conversations": [],
        "inject_policy": {"max_chars": 1200, "max_lines": 50, "procedure_full_score": 0.40},
    }
    ctx = build_hub_prompt_context(payload, "how to run migration")
    assert len(ctx) <= 1200
    # Demoted procedures stay intact as 1-line index entries with ID, boundary, and L0 ref.
    assert "[procedure-index id=proc-000" in ctx
    assert "scope=project" in ctx and "env: PostgreSQL 16" in ctx and "files: db/migrate.py" in ctx
    assert "memory_get(memory_id=id)" in ctx


def test_local_drift_hint_is_device_local_recoverable_and_never_flags_global_rules(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/refactor\n", encoding="utf-8")
    target_file = repo / "src" / "legacy_worker.py"
    target_file.parent.mkdir(parents=True)
    target_file.write_text("x = 1\n", encoding="utf-8")

    proc_item = {
        "memory_id": "deadbeef-1111-2222-3333-444455556666",
        "content": "Worker retry\nAdjust backoff in legacy_worker.py",
        "category": "fact",
        "kind": "procedure",
        "layer": "L1_FACT",
        "score": 0.30,
        "source_ids": ["cafebabe-0000"],
        "procedure": {
            "task": "How to tune worker retry backoff",
            "solution": "Adjust backoff in legacy_worker.py",
            "boundary_scope": "project",
            "boundary_envs": "",
            "branch": "main",
            "workspace_id": str(repo),
            "files": ["src/legacy_worker.py"],
        },
    }

    # File exists -> no drift.
    assert inspect_local_procedure_files(proc_item["procedure"], str(repo)) is None

    # File removed on current branch -> local drift reported with branch & timestamp,
    # original dict untouched, user/global scope never flagged.
    target_file.unlink()
    drift = inspect_local_procedure_files(proc_item["procedure"], str(repo))
    assert drift is not None
    assert drift["branch"] == "feat/refactor"
    assert drift["missing_files"] == ["src/legacy_worker.py"]
    assert "_local_drift" not in proc_item

    global_proc = {**proc_item["procedure"], "boundary_scope": "user"}
    assert inspect_local_procedure_files(global_proc, str(repo)) is None

    # Another machine where ``repo`` does not exist -> None (never false-flags).
    assert inspect_local_procedure_files(
        {**proc_item["procedure"], "workspace_id": "/nonexistent/other/device/repo"},
        "/nonexistent/other/device/repo",
    ) is None

    # Rendered in Hub context as a local verification hint, then disappears once restored.
    ctx = build_hub_prompt_context(
        {"memories": {"procedures": [proc_item]}, "conversations": []},
        "worker retry",
        workspace=str(repo),
    )
    assert "本机核验提示" in ctx and "feat/refactor" in ctx

    target_file.write_text("x = 2\n", encoding="utf-8")
    ctx_restored = build_hub_prompt_context(
        {"memories": {"procedures": [proc_item]}, "conversations": []},
        "worker retry",
        workspace=str(repo),
    )
    assert "本机核验提示" not in ctx_restored


async def test_execution_signals_wired_across_agentica_claude_codex_and_sdk_with_redaction(
    tmp_path: Path, fake_embed
) -> None:
    roots = default_source_roots(tmp_path)

    # 1. Agentica transcript with failing tool followed by succeeding tool.
    ag_file = roots["agentica"] / "proj" / "ag-sig.jsonl"
    _jsonl(
        ag_file,
        [
            {
                "type": "user",
                "session_id": "ag1",
                "cwd": "/w/svc",
                "git_branch": "main",
                "timestamp": "2026-11-11T08:00:00Z",
                "content": "Run migration tests",
            },
            {
                "type": "assistant",
                "session_id": "ag1",
                "timestamp": "2026-11-11T08:00:02Z",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "Bash", "arguments": '{"command": "pytest tests/test_mig.py"}'},
                    }
                ],
            },
            {
                "type": "tool",
                "session_id": "ag1",
                "tool_call_id": "call-1",
                "name": "Bash",
                "is_error": True,
                "timestamp": "2026-11-11T08:00:05Z",
                "content": "OperationalError: table ledger_v2 missing\nExit code: 1",
            },
            {
                "type": "assistant",
                "session_id": "ag1",
                "timestamp": "2026-11-11T08:00:08Z",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "function": {"name": "Bash", "arguments": '{"command": "alembic upgrade head && pytest tests/test_mig.py"}'},
                    }
                ],
            },
            {
                "type": "tool",
                "session_id": "ag1",
                "tool_call_id": "call-2",
                "name": "Bash",
                "timestamp": "2026-11-11T08:00:12Z",
                "content": "1 passed\nExit code: 0",
            },
            {
                "type": "assistant",
                "session_id": "ag1",
                "timestamp": "2026-11-11T08:00:15Z",
                "content": "Ran alembic upgrade head first; tests pass now.",
            },
        ],
    )
    ag_msgs, _ = SOURCE_SPECS["agentica"].read(ag_file, 0)
    _, ag_open = build_turns(ag_msgs)
    assert ag_open is not None
    # Tool output never leaks into dialogue messages, while structured signals carry seq/call_id/target/error.
    assert "OperationalError" not in json.dumps(ag_open.messages())
    resolved = extract_tool_signals(ag_open.messages(), extra=ag_open.signals)
    assert [
        (s["seq"], s["call_id"], s["tool"], s["ok"], s.get("exit_code"))
        for s in resolved
    ] == [
        (0, "call-1", "Bash", False, 1),
        (1, "call-2", "Bash", True, 0),
    ]
    assert resolved[0]["target"] == "pytest tests/test_mig.py"
    assert "OperationalError: table ledger_v2 missing" in resolved[0]["error"]

    # 2. Claude Code & Codex transcripts -> end-to-end scan_file into Hub Core with secret redaction.
    cc_file = roots["claude-code"] / "proj" / "cc-sig.jsonl"
    _jsonl(
        cc_file,
        [
            {
                "type": "user",
                "sessionId": "cc1",
                "cwd": "/w/svc",
                "gitBranch": "main",
                "timestamp": "2026-11-11T09:00:00Z",
                "message": {"content": "deploy check"},
            },
            {
                "type": "assistant",
                "sessionId": "cc1",
                "timestamp": "2026-11-11T09:00:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-9",
                            "name": "Bash",
                            "input": {"command": "curl https://user:secretpass@api.internal/health"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "sessionId": "cc1",
                "timestamp": "2026-11-11T09:00:02Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-9",
                            "is_error": True,
                            "content": "Auth failed for sk-proj-1234567890abcdefghijklmnop",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "sessionId": "cc1",
                "timestamp": "2026-11-11T09:00:04Z",
                "message": {"content": [{"type": "text", "text": "Health check failed on auth."}]},
            },
        ],
    )

    cx_file = roots["codex"] / "2026" / "11" / "11" / "rollout-sig.jsonl"
    _jsonl(
        cx_file,
        [
            {"type": "session_meta", "payload": {"id": "cx1", "cwd": "/w/svc", "git": {"branch": "main"}}},
            {
                "type": "event_msg",
                "timestamp": "2026-11-11T09:10:00Z",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": "check lint"}]},
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-11T09:10:01Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "cx-call-1",
                    "name": "shell",
                    "arguments": '{"cmd": "ruff check ."}',
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-11T09:10:02Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "cx-call-1",
                    "output": json.dumps({"output": "F401 unused import", "metadata": {"exit_code": 1}}),
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-11-11T09:10:04Z",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "Fixed F401."}]},
                },
            },
        ],
    )

    core = MemoryClient(
        settings=Settings(
            storage_dir=str(tmp_path / "store"),
            mode="system1",
            redact_secrets=True,
        ),
        embed=fake_embed,
        llm=FakeLLMClient({"json": {"candidates": []}}),
        jev=FakeJev({"scene": "CODING"}),
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core, settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret", transport=transport) as hub:
            with CursorStore(tmp_path / "hub") as cursors:
                await scan_file(hub, source="claude-code", path=cc_file, cursors=cursors, complete=True)
                await scan_file(hub, source="codex", path=cx_file, cursors=cursors, complete=True)
            # Wait for queue worker to drain both writes.
            for _ in range(50):
                st = await hub.status()
                if st["writes"].get("done", 0) >= 2:
                    break
                await asyncio.sleep(0.05)

            raws = {
                n.session_id: n
                for n in core.factory.vector.get_many({"layer": Layer.L0_RAW.value}, limit=20)
            }
            cc_sig = raws["claude-code:cc1"].custom["tool_signals"][0]
            assert (cc_sig["call_id"], cc_sig["tool"], cc_sig["ok"]) == ("tu-9", "Bash", False)
            assert "secretpass" not in cc_sig["target"]
            assert "sk-proj-" not in cc_sig["error"]
            assert "<redacted" in cc_sig["target"] and "<redacted" in cc_sig["error"]

            cx_sig = raws["codex:cx1"].custom["tool_signals"][0]
            assert (cx_sig["call_id"], cx_sig["tool"], cx_sig["exit_code"], cx_sig["ok"]) == (
                "cx-call-1",
                "shell",
                1,
                False,
            )
            assert cx_sig["target"] == "ruff check ."
            assert "F401 unused import" in cx_sig["error"]
    await core.aclose()


def _zero(dim: int = 64) -> list[float]:
    return [0.0] * dim


def _stored(
    content: str,
    layer: Layer,
    *,
    session_id: str = "",
    agent_id: str = "",
    owner: str = "user",
    custom: dict | None = None,
    kind: str = "",
    is_latest: bool = True,
    memory_at: int = 1_700_000_000,
    node_id: str = "",
) -> MemoryNode:
    return MemoryNode(
        content=content,
        layer=layer,
        app_id="default",
        user_id="default",
        agent_id=agent_id,
        session_id=session_id,
        owner=owner,
        kind=kind,
        is_latest=is_latest,
        memory_at=memory_at,
        embedding=_zero(),
        custom=custom,
        **({"node_id": node_id} if node_id else {}),
    )


def _procedure_custom(workspace: str, task: str, source_id: str) -> dict:
    return {
        "source_node_id": source_id,
        "source_ids": [source_id],
        "procedure": {
            "task": task,
            "solution": "do the thing",
            "search_keys": ["k"],
            "boundary_envs": "",
            "boundary_scope": "project",
            "files": [],
            "workspace_id": workspace,
            "branch": "main",
            "confidence": 0.7,
        },
    }


async def test_memory_get_full_id_rejects_other_agent(tmp_path: Path, fake_embed) -> None:
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "store"), mode="system1", embed_dim=64),
        embed=fake_embed,
        llm=FakeLLMClient({"json": {"candidates": []}}),
        jev=FakeJev(),
    )
    own = _stored("本机个人记忆：提交信息只写一行", Layer.L1_FACT)
    other = _stored(
        "其他 agent 的账单规则",
        Layer.L1_FACT,
        agent_id="billing-bot",
    )
    core.factory.vector.upsert([own, other])
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core, settings=core.settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with HubClient(base_url="http://hub", api_key="secret", transport=transport) as hub:
            got = await hub.get_memory(own.node_id)
            assert got["memory_id"] == own.node_id
            with pytest.raises(httpx.HTTPStatusError) as exc_full:
                await hub.get_memory(other.node_id)
            assert exc_full.value.response.status_code == 404
            with pytest.raises(httpx.HTTPStatusError) as exc_prefix:
                await hub.get_memory(other.node_id[:8])
            assert exc_prefix.value.response.status_code == 404
    await core.aclose()


async def test_project_view_keeps_workspace_boundary_across_a_shared_session(
    tmp_path: Path, fake_embed
) -> None:
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "store"), mode="system1", embed_dim=64),
        embed=fake_embed,
        llm=FakeLLMClient({"json": {"candidates": []}}),
        jev=FakeJev(),
    )
    l0_a = _stored(
        "[user]: 项目 A",
        Layer.L0_RAW,
        session_id="cursor:shared",
        custom={"workspace_id": "/w/proj-a"},
    )
    l0_b = _stored(
        "[user]: 项目 B",
        Layer.L0_RAW,
        session_id="cursor:shared",
        custom={"workspace_id": "/w/proj-b"},
    )
    fact_a = _stored(
        "项目A红线：金额用整数分",
        Layer.L1_FACT,
        session_id="cursor:shared",
        custom={"source_node_id": l0_a.node_id, "source_ids": [l0_a.node_id]},
    )
    fact_b = _stored(
        "项目B红线：日志必须 JSON",
        Layer.L1_FACT,
        session_id="cursor:shared",
        custom={"source_node_id": l0_b.node_id, "source_ids": [l0_b.node_id]},
    )
    loose = _stored(
        "这条没有项目边界，跟 session 走",
        Layer.L1_FACT,
        session_id="cursor:shared",
        custom={},
    )
    proc_b = _stored(
        "B procedure",
        Layer.L1_FACT,
        session_id="cursor:shared",
        kind="procedure",
        custom=_procedure_custom("/w/proj-b", "如何给项目 B 打日志", l0_b.node_id),
    )
    old = _stored(
        "项目要求上线前审批",
        Layer.L1_FACT,
        session_id="cursor:shared",
        is_latest=False,
        custom={"source_node_id": l0_a.node_id, "source_ids": [l0_a.node_id]},
    )
    negated = _stored(
        "项目不再要求审批",
        Layer.L1_FACT,
        session_id="cursor:shared",
        memory_at=1_700_000_100,
        custom={
            "negation": True,
            "update_type": "NEGATE",
            "source_node_id": l0_a.node_id,
            "source_ids": [l0_a.node_id],
        },
    )
    profiles = [
        _stored(
            f"用户画像 {i}：喜欢第 {i} 种编辑器主题",
            Layer.L2_PROFILE,
            session_id="cursor:shared",
            memory_at=1_600_000_000 + i,
            custom={"source_node_id": l0_a.node_id, "source_ids": [l0_a.node_id]},
        )
        for i in range(12)
    ]
    red_line = _stored(
        "项目A新红线：测试永远使用 pytest tmp_path，严禁读写生产库",
        Layer.L1_FACT,
        session_id="cursor:shared",
        memory_at=1_800_000_000,
        custom={"source_node_id": l0_a.node_id, "source_ids": [l0_a.node_id]},
    )
    core.factory.vector.upsert(
        [l0_a, l0_b, fact_a, fact_b, loose, proc_b, old, negated, red_line, *profiles]
    )

    _, view_a = project_sessions_and_view(core, "/w/proj-a")
    assert view_a is not None
    texts = [item["content"] for item in view_a["conventions"]]
    assert "项目A红线：金额用整数分" in texts
    assert "项目不再要求审批" in texts
    assert "项目A新红线：测试永远使用 pytest tmp_path，严禁读写生产库" in texts
    assert "项目B红线：日志必须 JSON" not in texts
    assert "项目要求上线前审批" not in texts
    assert any("没有项目边界" in text for text in texts)
    assert sum(1 for text in texts if text.startswith("用户画像")) <= 4
    proc_tasks = [item["procedure"]["task"] for item in view_a["procedures"]]
    assert proc_tasks == []

    _, view_b = project_sessions_and_view(core, "/w/proj-b")
    assert view_b is not None
    texts_b = [item["content"] for item in view_b["conventions"]]
    assert "项目B红线：日志必须 JSON" in texts_b
    assert "项目A红线：金额用整数分" not in texts_b
    assert [item["procedure"]["task"] for item in view_b["procedures"]] == ["如何给项目 B 打日志"]

    retired = retired_claims_by_source(
        core.factory.vector.get_many({"user_id": "default"}, limit=100),
        {l0_a.node_id},
    )
    assert retired[l0_a.node_id] == ["项目要求上线前审批"]
    await core.aclose()


def test_budget_returns_dropped_rule_to_project_view_without_slicing() -> None:
    red_line = "项目红线：测试只用 tmp_path。严禁读写 /data/prod.db"
    fillers = [
        {
            "memory_id": f"fill-{idx}",
            "content": f"填充事实 {idx} " + ("细节 " * 40),
            "category": "fact",
            "layer": "L1_FACT",
            "owner": "user",
            "score": 0.9 - idx * 0.01,
        }
        for idx in range(3)
    ]
    payload = {
        "memories": {
            "profile": [],
            "proactive": [],
            "normal": [
                *fillers,
                {
                    "memory_id": "rule-red",
                    "content": "项目红线：测试只用 tmp_path。\n严禁读写 /data/prod.db",
                    "category": "fact",
                    "layer": "L1_FACT",
                    "owner": "user",
                    "score": 0.2,
                },
            ],
            "procedures": [],
        },
        "project_view": {
            "workspace": "/w/ledger",
            "conventions": [
                {
                    "memory_id": "rule-red",
                    "content": red_line,
                    "layer": "L1_FACT",
                    "owner": "user",
                }
            ],
            "procedures": [],
        },
        "conversations": [],
        "inject_policy": {"max_chars": 420, "max_lines": 40, "procedure_full_score": 0.40},
    }
    ctx = build_hub_prompt_context(payload, "补一个回归用例")
    assert "严禁读写 /data/prod.db" in ctx
    assert len(ctx) <= 420

    huge = "开头保留的说明。\n" + ("中间填充。\n" * 40) + "末尾禁止：严禁 DROP。"
    sliced = build_hub_prompt_context(
        {
            "memories": {
                "normal": [
                    {
                        "memory_id": "huge-1",
                        "content": huge,
                        "category": "fact",
                        "layer": "L1_FACT",
                        "score": 0.5,
                    }
                ]
            },
            "conversations": [],
            "inject_policy": {"max_chars": 180, "max_lines": 8, "procedure_full_score": 0.40},
        },
        "能不能直接删列",
    )
    if "开头保留的说明" in sliced:
        assert "严禁 DROP" in sliced
    assert len(sliced) <= 180


def test_retired_wording_does_not_delete_an_unrelated_same_source_fact() -> None:
    kept = novel_source_excerpt(
        "[user]: 测试使用生产数据库。生产数据库每晚备份到异地冷存。",
        "备份策略是什么？",
        [],
        retired_texts=["测试使用生产数据库"],
    )
    assert "每晚备份" in kept
    assert "测试使用生产数据库" not in kept

    untouched = novel_source_excerpt(
        "[user]: 生产数据库每晚备份到异地冷存。",
        "备份策略是什么？",
        [],
        retired_texts=["测试使用生产数据库"],
    )
    assert "每晚备份" in untouched
    assert "已废止说法" in untouched


def test_pi_bash_execution_signal_reads_message_role(tmp_path: Path) -> None:
    path = tmp_path / "2026-11-08T10-00-00-000Z_pi-bash.jsonl"
    _jsonl(
        path,
        [
            {"type": "session", "id": "pi-bash", "cwd": "/Users/me/pi"},
            {
                "type": "message",
                "timestamp": "2026-11-08T10:00:01.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "跑一下"}],
                    "timestamp": 1794144606000,
                },
            },
            {
                "type": "message",
                "id": "b1",
                "timestamp": "2026-11-08T10:00:02.000Z",
                "message": {
                    "role": "bashExecution",
                    "command": "pytest -q",
                    "output": "2 failed",
                    "exitCode": 2,
                },
            },
            {
                "type": "message",
                "timestamp": "2026-11-08T10:00:03.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "失败了"}],
                    "timestamp": 1794144609000,
                },
            },
        ],
    )
    messages, _ = SOURCE_SPECS["pi"].read(path, 0)
    _, turn = build_turns(messages)
    assert turn is not None
    assert "2 failed" not in turn.assistant
    sig = turn.signals[0]
    assert (sig["tool"], sig["exit_code"], sig["ok"], sig["target"]) == (
        "bash",
        2,
        False,
        "pytest -q",
    )
    assert isinstance(sig["at"], int) and sig["at"] > 0
    assert "2 failed" in sig["error"]

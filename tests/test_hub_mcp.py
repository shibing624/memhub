# -*- coding: utf-8 -*-
"""The host-facing Hub MCP proxy must make a memory request a one-call action."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="the Hub MCP proxy requires mcp 2.x (install the 'mcp' extra)",
)

from memhub.cli import hub as cli_hub  # noqa: E402
from memhub.cli import main as cli_main  # noqa: E402
from memhub.hub.client import HubClient  # noqa: E402
from memhub.hub.hosts import CLI_COMMANDS, render_skill  # noqa: E402
from memhub.hub.mcp import build_hub_mcp  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

cli_runner = CliRunner()


class StubCore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rows = [
            {
                "id": "m-current",
                "content": "用户撤销了不要用浏览器截图验证的约定",
                "layer": "L1_FACT",
                "kind": "fact",
                "at": 3,
                "is_latest": True,
                "deleted": False,
            },
            {
                "id": "m-stale",
                "content": "用户要求不要用浏览器截图验证",
                "layer": "L1_FACT",
                "kind": "fact",
                "at": 2,
                "is_latest": False,
                "deleted": False,
            },
            {
                "id": "m-deleted",
                "content": "浏览器截图相关的旧规矩",
                "layer": "L1_FACT",
                "kind": "fact",
                "at": 1,
                "is_latest": True,
                "deleted": True,
            },
        ]

    async def write(self, *, content: str, request_id: str) -> dict:
        self.calls.append(("write", content))
        return {"request_id": request_id, "status": "pending"}

    async def recall(
        self,
        *,
        query: str,
        limit: int = 10,
        raw_limit: int = 5,
        procedure_limit: int = 3,
        workspace: str | None = None,
    ) -> dict:
        self.calls.append(("recall", query, limit, raw_limit))
        return {
            "memories": {
                "profile": [],
                "normal": [
                    {
                        "memory_id": "m-current",
                        "content": "用户住在北京",
                        "category": "fact",
                        "score": 0.62,
                        "layer": "L1_FACT",
                        "source_node_id": "37c85fe9-1111-2222-3333-444455556666",
                    }
                ],
            },
            "conversations": [
                {"session_id": "codex:abc", "content": "我之前问过什么"}
            ],
        }

    async def get_conversation(
        self, source_id: str, *, context_turns: int = 1
    ) -> dict:
        self.calls.append(("get_conversation", source_id, context_turns))
        return {
            "source_id": source_id[:8],
            "memory_id": f"{source_id}-full",
            "session_id": "codex:abc",
            "conversations": [
                {
                    "memory_id": f"{source_id}-full",
                    "source_id": source_id[:8],
                    "session_id": "codex:abc",
                    "content": "[user]: 我搬到北京了\n[assistant]: 好的",
                    "is_anchor": True,
                }
            ],
        }

    async def get_memory(self, memory_id: str) -> dict:
        self.calls.append(("get_memory", memory_id))
        return {
            "memory_id": "a1b2c3d4-0000-1111-2222-333344445555",
            "content": "How to publish SQLite transactions safely\nUse BEGIN IMMEDIATE",
            "layer": "L1_FACT",
            "kind": "procedure",
            "is_latest": True,
            "source_ids": ["37c85fe9-1111-2222-3333-444455556666"],
            "procedure": {
                "task": "How to publish SQLite transactions safely",
                "solution": "Use BEGIN IMMEDIATE",
                "boundary_scope": "project",
                "boundary_envs": "SQLite 3.45+",
                "files": ["memhub/store.py"],
            },
        }

    async def list_memories(self) -> list[dict]:
        self.calls.append(("list_memories",))
        return self.rows

    async def update_memory(self, memory_id: str, content: str) -> dict:
        self.calls.append(("update_memory", memory_id, content))
        return {"success": True, "memory_id": "m-new"}

    async def delete_memory(self, memory_id: str) -> dict:
        self.calls.append(("delete_memory", memory_id))
        return {"success": True, "memory_id": memory_id}

    async def status(self) -> dict:
        self.calls.append(("status",))
        return {"instance_id": "stub", "writes": {"pending": 0}}


def _payload(result) -> dict:
    assert result.is_error is False, result.content
    if result.structured_content is not None:
        return result.structured_content["result"]
    (block,) = result.content
    return json.loads(block.text)


async def test_tools_cover_the_full_memory_lifecycle() -> None:
    tools = {t.name: t for t in await build_hub_mcp(client=StubCore()).list_tools()}
    assert set(tools) == {
        "memory_add",
        "memory_get",
        "memory_search",
        "memory_list",
        "memory_update",
        "memory_delete",
        "conversation_search",
        "memory_status",
    }
    assert "pending" in tools["memory_add"].description
    assert "procedure-index" in tools["memory_get"].description
    assert "memory_id" in tools["memory_update"].description
    assert "找不到匹配项" in tools["memory_list"].description
    assert "session_id" in tools["conversation_search"].description
    assert "codex:<id>" in tools["conversation_search"].description
    assert "已绑定本机个人记忆" in tools["memory_status"].description


async def test_remember_is_one_call_and_a_terminal_result() -> None:
    core = StubCore()
    server = build_hub_mcp(client=core)
    body = _payload(
        await server.call_tool("memory_add", {"content": "用户要求前端改动在真实页面验证"})
    )
    assert body["ok"] is True
    assert body["status"] == "pending"
    assert "不需要轮询" in body["hint"]
    assert core.calls == [("write", "用户要求前端改动在真实页面验证")]


async def test_search_returns_flat_rows_with_ids() -> None:
    server = build_hub_mcp(client=StubCore())
    body = _payload(await server.call_tool("memory_search", {"query": "住址"}))
    assert body["ok"] is True
    assert body["memories"][0]["id"] == "m-current"
    assert body["memories"][0]["content"] == "用户住在北京"


async def test_memory_get_expands_procedure_by_short_id() -> None:
    core = StubCore()
    server = build_hub_mcp(client=core)
    body = _payload(await server.call_tool("memory_get", {"memory_id": "a1b2c3d4"}))
    assert body["ok"] is True
    mem = body["memory"]
    assert mem["memory_id"].startswith("a1b2c3d4")
    assert mem["procedure"]["solution"] == "Use BEGIN IMMEDIATE"
    assert mem["source_id"] == "37c85fe9"
    assert core.calls == [("get_memory", "a1b2c3d4")]


async def test_list_only_returns_editable_current_rows() -> None:
    core = StubCore()
    server = build_hub_mcp(client=core)
    body = _payload(await server.call_tool("memory_list", {"query": "截图"}))
    assert [row["id"] for row in body["memories"]] == ["m-current"]
    assert "memory_update" in body["hint"]
    assert core.calls == [("list_memories",)]


async def test_update_and_delete_are_one_call_each() -> None:
    core = StubCore()
    server = build_hub_mcp(client=core)
    updated = _payload(
        await server.call_tool(
            "memory_update",
            {"memory_id": "m-current", "content": "用户鼓励用浏览器验证前端"},
        )
    )
    assert updated["ok"] is True and updated["memory_id"] == "m-new"
    deleted = _payload(
        await server.call_tool("memory_delete", {"memory_id": "m-current"})
    )
    assert deleted["ok"] is True
    assert deleted["hint"] == "记忆已删除。"
    assert core.calls == [
        ("update_memory", "m-current", "用户鼓励用浏览器验证前端"),
        ("delete_memory", "m-current"),
    ]


async def test_conversation_search_and_status() -> None:
    core = StubCore()
    server = build_hub_mcp(client=core)
    found = _payload(
        await server.call_tool("conversation_search", {"query": "之前问过什么"})
    )
    assert found["conversations"][0]["session_id"] == "codex:abc"
    by_source = _payload(
        await server.call_tool(
            "conversation_search",
            {"source_id": "37c85fe9", "context_turns": 2},
        )
    )
    assert by_source["source_id"] == "37c85fe9"
    assert by_source["conversations"][0]["is_anchor"] is True
    assert ("get_conversation", "37c85fe9", 2) in core.calls
    status = _payload(await server.call_tool("memory_status", {}))
    assert status["instance_id"] == "stub"


async def test_core_http_error_is_a_readable_tool_result() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "memory not found"})

    async with HubClient(
        base_url="http://hub",
        api_key="secret",
        transport=httpx.MockTransport(reply),
    ) as client:
        server = build_hub_mcp(client=client)
        body = _payload(
            await server.call_tool(
                "memory_update", {"memory_id": "gone", "content": "新内容"}
            )
        )
    assert body["ok"] is False
    assert "404" in body["error"]
    assert "只重试一次" in body["hint"]


async def test_hub_client_memory_endpoints() -> None:
    seen: list[tuple[str, str, str]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.content.decode()))
        if request.method == "GET" and str(request.url).endswith("/v1/hub/memories"):
            return httpx.Response(200, json={"memories": [{"id": "m1"}]})
        return httpx.Response(200, json={"memory_id": "m1", "success": True})

    async with HubClient(
        base_url="http://hub",
        api_key="secret",
        transport=httpx.MockTransport(reply),
    ) as client:
        assert await client.list_memories() == [{"id": "m1"}]
        assert (await client.get_memory("a1b2c3d4"))["memory_id"] == "m1"
        await client.update_memory("a/b", "新内容")
        await client.delete_memory("a/b")
    assert seen[0][1] == "http://hub/v1/hub/memories"
    assert seen[1][:2] == ("GET", "http://hub/v1/hub/memories/a1b2c3d4")
    assert seen[2][0] == "PUT"
    assert seen[2][1] == "http://hub/v1/hub/memories/a%2Fb"
    assert json.loads(seen[2][2]) == {"content": "新内容"}
    assert seen[3][:2] == ("DELETE", "http://hub/v1/hub/memories/a%2Fb")


def test_installed_skill_lists_mcp_and_a_complete_cli_fallback() -> None:
    commands = {
        name: ["memhub", name, "--hub-dir", "/Users/xuming/.memhub"]
        for name in CLI_COMMANDS
    }
    skill = render_skill(
        host="codebuddy",
        cli_prefix=["python3", "-m", "memhub.cli"],
        commands=commands,
    )
    assert "记住 / 别忘了" in skill
    assert "## 硬约束" not in skill
    assert "## 优先：宿主内置的 MCP 工具" in skill
    assert "优先调用宿主内置的 `memhub` MCP 工具" in skill
    assert "python3 -m memhub.cli <子命令>" in skill
    assert "本机等价入口是 `python3 -m memhub.cli`" in skill
    assert "codex:<id>" in skill
    for needle in (
        "memory_add",
        "memory_get",
        "memory_list",
        "memory_update",
        "memory_delete",
        "pending",
        "memhub recall --hub-dir /Users/xuming/.memhub",
        "memhub remember --hub-dir /Users/xuming/.memhub",
        "memhub memory-get --hub-dir /Users/xuming/.memhub",
        "memhub memory-list --hub-dir /Users/xuming/.memhub",
        "memhub memory-update --hub-dir /Users/xuming/.memhub",
        "memhub memory-delete --hub-dir /Users/xuming/.memhub",
        "memhub status --hub-dir /Users/xuming/.memhub",
    ):
        assert needle in skill


class FakeCliCore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_memories(self) -> list[dict]:
        self.calls.append(("list_memories",))
        return StubCore().rows

    async def update_memory(self, memory_id: str, content: str) -> dict:
        self.calls.append(("update_memory", memory_id, content))
        return {"success": True, "memory_id": "m-new"}

    async def delete_memory(self, memory_id: str) -> dict:
        self.calls.append(("delete_memory", memory_id))
        return {"success": True, "memory_id": memory_id}

    async def get_memory(self, memory_id: str) -> dict:
        self.calls.append(("get_memory", memory_id))
        return await StubCore().get_memory(memory_id)


def test_cli_memory_list_only_shows_editable_rows(monkeypatch) -> None:
    core = FakeCliCore()
    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: core)
    out = cli_runner.invoke(cli_main.app, ["memory-list", "截图"])
    assert out.exit_code == 0, out.output
    assert "m-current" in out.output
    assert "m-stale" not in out.output
    assert "m-deleted" not in out.output
    assert core.calls == [("list_memories",)]


def test_cli_memory_list_flattens_long_content(monkeypatch) -> None:
    class OneRow:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def list_memories(self) -> list[dict]:
            return [
                {
                    "id": "m-long",
                    "content": "第一行\n第二行\n" + "x" * 300,
                    "layer": "L0_RAW",
                    "kind": "raw",
                    "at": 1,
                    "is_latest": True,
                    "deleted": False,
                }
            ]

    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: OneRow())
    out = cli_runner.invoke(cli_main.app, ["memory-list", "第一行"])
    assert out.exit_code == 0, out.output
    assert len(out.output.strip().splitlines()) == 1
    assert "m-long" in out.output and "第一行 第二行" in out.output
    assert "…" in out.output


def test_cli_memory_list_json(monkeypatch) -> None:
    core = FakeCliCore()
    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: core)
    out = cli_runner.invoke(cli_main.app, ["memory-list", "截图", "--json"])
    assert out.exit_code == 0, out.output
    rows = json.loads(out.output)
    assert [row["id"] for row in rows] == ["m-current"]


def test_cli_memory_update_and_delete(monkeypatch) -> None:
    core = FakeCliCore()
    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: core)
    updated = cli_runner.invoke(
        cli_main.app, ["memory-update", "m-current", "用户鼓励用浏览器验证前端"]
    )
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.output)["memory_id"] == "m-new"
    deleted = cli_runner.invoke(cli_main.app, ["memory-delete", "m-current"])
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.output)["success"] is True
    assert core.calls == [
        ("update_memory", "m-current", "用户鼓励用浏览器验证前端"),
        ("delete_memory", "m-current"),
    ]


def test_cli_memory_get_formats_procedure_and_json(monkeypatch) -> None:
    core = FakeCliCore()
    monkeypatch.setattr(cli_hub, "make_client", lambda _hub: core)
    human = cli_runner.invoke(cli_main.app, ["memory-get", "a1b2c3d4"])
    assert human.exit_code == 0, human.output
    assert "How to publish SQLite transactions safely" in human.output
    assert "Use BEGIN IMMEDIATE" in human.output
    assert "SQLite 3.45+" in human.output
    assert "Source: L0 37c85fe9" in human.output

    as_json = cli_runner.invoke(cli_main.app, ["memory-get", "a1b2c3d4", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["procedure"]["files"] == ["memhub/store.py"]

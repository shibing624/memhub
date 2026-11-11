# -*- coding: utf-8 -*-
"""Local stdio MCP proxy for one bearer-authenticated MemHub Core."""

from __future__ import annotations

import argparse
import re
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from memhub.hub.browse import current_rows
from memhub.hub.client import HubClient

_SCOPE_NOTE = "已绑定本机个人记忆，不需要也不能传 user_id / app_id / agent_id。"
_MEMORY_GROUPS = (
    "profile",
    "proactive",
    "normal",
    "raw",
    "schema",
    "catalog",
    "procedures",
)


async def _call(awaitable: Awaitable[Any]) -> Any:
    """Turn a Core HTTP failure into a readable tool result instead of a traceback."""
    try:
        return await awaitable
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "error": f"Hub Core 返回 {exc.response.status_code}: {exc.response.text[:200]}",
            "hint": "按错误信息修正参数后只重试一次。",
        }


_L0_ID_RE = re.compile(
    r"^(?:L0\s+)?([0-9a-f]{8,36}(?:-[0-9a-f-]+)*)$", re.IGNORECASE
)


def _looks_like_l0_id(query: str) -> str:
    """The L0 id inside ``query``, or ``""``.

    Needs 8+ hex chars, at least one letter and at least one digit. With a bare hex
    class, an English word such as ``decade`` matched and the tool answered a
    search with a 404; without the digit rule, a purely numeric query like
    ``1234567890`` would too.
    """
    match = _L0_ID_RE.match(query.strip())
    if match is None:
        return ""
    value = match.group(1)
    has_letter = any(ch in "abcdefABCDEF" for ch in value)
    has_digit = any(ch.isdigit() for ch in value)
    return value if has_letter and has_digit else ""


def _source_ref(item: dict[str, Any]) -> str:
    """The 8-char L0 ref for a hit: ``source_node_id``, else its ``source_ids`` head.

    Facts get ``custom.source_node_id`` from the reconciler, but a procedure keeps
    only ``custom.source_ids``, so keying on ``source_node_id`` alone left every
    engineering memory without a ref to open.
    """
    candidates = [item.get("source_node_id"), *(item.get("source_ids") or [])]
    for candidate in candidates:
        if candidate:
            return str(candidate)[:8]
    return ""


def _search_rows(memories: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten grouped recall results into one agent-readable list with ids."""
    rows = [
        {
            "id": item.get("memory_id", ""),
            "content": item.get("content", ""),
            "category": item.get("category", group),
            "layer": item.get("layer", ""),
            "score": item.get("score", 0.0),
            "group": group,
            **({"source_id": ref} if (ref := _source_ref(item)) else {}),
        }
        for group in _MEMORY_GROUPS
        for item in (memories.get(group) or [])
    ]
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows


def _list_rows(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "layer": row["layer"],
            "kind": row["kind"],
            "at": row["at"],
        }
        for row in current_rows(rows, query=query, limit=limit)
    ]


def build_hub_mcp(
    *,
    hub_dir: str | Path | None = None,
    client: HubClient | None = None,
):
    """Build the MCP server. The Core is resolved per tool call, so a host that
    starts before ``hub serve`` still gets working tools once the Core is up."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise ImportError(
            "MemHub MCP requires: pip install memhub[mcp]"
        ) from exc

    @asynccontextmanager
    async def core() -> AsyncIterator[HubClient]:
        if client is not None:
            yield client
            return
        async with HubClient.from_config(hub_dir) as connected:
            yield connected

    # WARNING, not the INFO default: every tool call is an HTTP request to the Core,
    # and an INFO line per request floods the host's MCP log (stderr).
    mcp = MCPServer("memhub-hub", log_level="WARNING")

    @mcp.tool(
        description=(
            "记住用户明确要求长期保留的一件事：偏好、约定、规矩、决策"
            "（「记住」「别忘了」「以后都按这个」）。"
            "content 写成一条完整、可独立理解的第三人称陈述句，例如"
            "「用户要求前端改动在真实页面里验证」。"
            "返回 ok=true 即写入已接收，status=pending 是正常终态，后台自动抽取。"
            "普通聊天内容由宿主 Hook 自动采集；撤销或修改旧约定请用 memory_list + memory_update。"
            + _SCOPE_NOTE
        )
    )
    async def memory_add(content: str) -> dict:
        async with core() as hub:
            receipt = await _call(
                hub.write(content=content, request_id=f"hub-mcp:{uuid.uuid4()}")
            )
        if not isinstance(receipt, dict) or receipt.get("ok") is False:
            return receipt
        return {
            "ok": True,
            "request_id": receipt.get("request_id", ""),
            "status": receipt.get("status", "pending"),
            "hint": "写入已接收，后台会自动抽取；status=pending 是正常终态，不需要轮询或重复写入。",
        }

    @mcp.tool(
        description=(
            "语义检索当前生效的个人记忆（画像、事实、意图、工程经验）。"
            "返回 memories 列表，每项含 id / content / score / layer。"
            "每轮提问注入的 <relevant-memories> 已包含相关记忆，先用注入内容，不够再查；"
            "要编辑记忆请用 memory_list。" + _SCOPE_NOTE
        )
    )
    async def memory_search(query: str, limit: int = 10) -> dict:
        async with core() as hub:
            result = await _call(hub.recall(query=query, limit=limit, raw_limit=0))
        if not isinstance(result, dict) or result.get("ok") is False:
            return result
        return {
            "ok": True,
            "memories": _search_rows(result["memories"]),
            "hint": "没有匹配结果说明当前没有相关记忆；要编辑或删除请用 memory_list。",
        }

    @mcp.tool(
        description=(
            "按记忆 ID（完整 ID 或 >=8 位前缀，如 [procedure-index id=a1b2c3d4] 或项目视图中的 id）"
            "精确读取单条记忆的完整内容（含 procedure 的 task / solution / boundary_envs / files 及来源 L0 短 ID）。"
            "当注入上下文仅展示目录标题索引且与当前任务相关时调用。" + _SCOPE_NOTE
        )
    )
    async def memory_get(memory_id: str) -> dict:
        async with core() as hub:
            item = await _call(hub.get_memory(memory_id))
        if not isinstance(item, dict) or item.get("ok") is False:
            return item
        ref = _source_ref(item)
        return {
            "ok": True,
            "memory": {
                **item,
                **({"source_id": ref} if ref else {}),
            },
        }

    @mcp.tool(
        description=(
            "列出当前生效的记忆条目及其 id，用于「改掉 / 撤销 / 更新 / 忘掉 / 删掉」"
            "某条约定时定位目标。query 为可选关键词（内容子串匹配），不传则按时间倒序返回最近 limit 条；"
            "返回的 id 可交给 memory_update / memory_delete。"
            "找不到匹配项时直接 memory_add 写新约定。" + _SCOPE_NOTE
        )
    )
    async def memory_list(query: str = "", limit: int = 50) -> dict:
        async with core() as hub:
            rows = await _call(hub.list_memories())
        if not isinstance(rows, list):
            return rows
        return {
            "ok": True,
            "memories": _list_rows(rows, query, limit),
            "hint": "用返回的 id 调用 memory_update 或 memory_delete；没有匹配项时直接 memory_add 写新约定。",
        }

    @mcp.tool(
        description=(
            "把一条已有记忆改成新内容，旧版本保留为历史。"
            "memory_id 取自 memory_list / memory_search 的 id，content 是完整的第三人称陈述句。"
            "404 表示 id 不存在或已不是当前版本。" + _SCOPE_NOTE
        )
    )
    async def memory_update(memory_id: str, content: str) -> dict:
        async with core() as hub:
            result = await _call(hub.update_memory(memory_id, content))
        if not isinstance(result, dict) or result.get("ok") is False:
            return result
        return {
            "ok": True,
            "memory_id": result.get("memory_id", ""),
            "hint": "记忆已更正并生效。",
        }

    @mcp.tool(
        description=(
            "删除（归档）一条已失效的记忆，memory_id 取自 memory_list / memory_search 的 id。"
            "仅在用户明确要求忘掉/删除时使用；给出新约定时优先用 memory_update。" + _SCOPE_NOTE
        )
    )
    async def memory_delete(memory_id: str) -> dict:
        async with core() as hub:
            result = await _call(hub.delete_memory(memory_id))
        if not isinstance(result, dict) or result.get("ok") is False:
            return result
        return {
            "ok": True,
            "memory_id": result.get("memory_id", memory_id),
            "hint": "记忆已删除。",
        }

    @mcp.tool(
        description=(
            "检索历史对话原文，或按 L0 来源 ID 展开完整原话与同会话前后上下文（渐进式披露）。"
            "1) 传 query 检索「我之前说过/问过什么」的原话、时间和来源；"
            "2) 传 source_id（如 <relevant-memories> 里的 `(来源: L0 37c85fe9)` 或 memory_search 返回的 source_id）"
            "拉取该条记忆对应的完整 L0 原始对话及前后 context_turns 轮对话。"
            "返回 conversations 列表；session_id 形如 codex:<id>，冒号前是来源工具。"
            + _SCOPE_NOTE
        )
    )
    async def conversation_search(
        query: str = "",
        limit: int = 10,
        source_id: str = "",
        context_turns: int = 1,
    ) -> dict:
        target_id = source_id.strip()
        if not target_id:
            target_id = _looks_like_l0_id(query)
        async with core() as hub:
            if target_id:
                result = await _call(
                    hub.get_conversation(target_id, context_turns=context_turns)
                )
            else:
                result = await _call(hub.recall(query=query, limit=0, raw_limit=limit))
        if not isinstance(result, dict) or result.get("ok") is False:
            return result
        return {
            "ok": True,
            **({"source_id": result["source_id"]} if "source_id" in result else {}),
            "conversations": result["conversations"],
            "hint": "这里只有历史对话原文，不是可编辑的记忆条目。",
        }

    @mcp.tool(
        description=("查看 Hub Core、记忆数量和写入队列状态，用于排查 memhub 自身故障。" + _SCOPE_NOTE)
    )
    async def memory_status() -> dict:
        async with core() as hub:
            status = await _call(hub.status())
        if not isinstance(status, dict) or status.get("ok") is False:
            return status
        return {**status, "hint": "状态查询不影响记忆内容。"}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="memhub-hub-mcp")
    parser.add_argument("--hub-dir")
    args = parser.parse_args()
    build_hub_mcp(hub_dir=args.hub_dir).run(transport="stdio")


if __name__ == "__main__":
    main()

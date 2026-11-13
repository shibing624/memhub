# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Read each CLI's own transcript store into complete user/assistant
turns and ingest them incrementally into the MemHub store.

Readers only use the structure each host writes (record types, roles, text
blocks, harness markup tags); they never guess meaning from wording:

- agentica    ``~/.agentica/projects/**/<session>.jsonl``  (type user/assistant)
- claude-code ``~/.claude/projects/**/<session>.jsonl``    (message.content text blocks)
- cline       ``~/.cline/data/sessions/<session>/<session>.messages.json`` (messages array; cursor = message index)
- codebuddy   ``~/.codebuddy/projects/**/<session>.jsonl`` (type message/function_call)
- codex       ``~/.codex/sessions/**/rollout-*.jsonl``     (event_msg UserMessage/AgentMessage
  items — the real input, unlike response_item which also carries AGENTS.md)
- copilot     ``~/.copilot/session-state/<session>/events.jsonl`` (session.start / user.message / assistant.message / tool.execution_start)
- cursor      ``~/.cursor/projects/*/agent-transcripts/**/*.jsonl``
- dsh         ``~/.dsh/sessions/**/session.v4.jsonl.zstd`` (zstd JSONL events
  ``user/message`` / ``assistant/message`` / ``tool/call``; cursor = decompressed offset)
- hermes      ``~/.hermes/state.db`` table ``messages`` (cursor = message id)
- kimi-cli    ``~/.kimi/sessions/<md5(work dir)>/<session>/context.jsonl`` (role user/assistant)
- kimi-code   ``~/.kimi-code/sessions/*/<session>/agents/main/wire.jsonl`` (append_message / step events)
- kiro        ``~/.kiro/sessions/**/sess_*/messages.jsonl`` (payload.type user/assistant/tool_call)
- openclaw    ``~/.openclaw/agents/<agent>/sessions/<session>.jsonl`` (pi session format v3)
- opencode    ``~/.local/share/opencode/opencode.db`` tables ``part``/``message``/``session`` (cursor = part rowid)
- pi          ``~/.pi/agent/sessions/--<cwd>--/<time>_<session>.jsonl`` (session format v3)
- qoder       ``~/.qoder/projects/**/<session>.jsonl`` (Claude-style JSONL)
- workbuddy   ``~/.workbuddy/projects/**/<session>.jsonl`` (type message/function_call)
- zcode       ``~/.zcode/cli/db/db.sqlite`` tables ``part``/``message``/``session`` (cursor = part rowid)

A turn is the user's message plus the assistant's *last* text before the next
user message — the answer, not the tool-call narration in between. Where a host
records tool calls structurally (Claude Code ``tool_use`` blocks, Codex
``function_call`` items) they are kept as the turn's steps so the Core knows the
files a session touched; tool *output* is never kept — it is how an answer was
reached, not what a later session needs. The working directory and git branch
the host records become the procedure's version boundary.
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import zstandard

from memhub.client import strip_memory_injection
from memhub.coding.preproc import extract_tool_signals
from memhub.hub.browse import canonical_workspace
from memhub.hub.config import CursorStore
from memhub.hub.redact import sanitize_text, strip_image_payloads

logger = logging.getLogger(__name__)
_SIGNAL_EXIT_RE = re.compile(r"(?m)^(?:Exit code|exit_code):\s*(-?\d+)\s*$")

SOURCES = (
    "agentica",
    "claude-code",
    "cline",
    "codebuddy",
    "codex",
    "copilot",
    "cursor",
    "dsh",
    "hermes",
    "kimi-cli",
    "kimi-code",
    "kiro",
    "openclaw",
    "opencode",
    "pi",
    "qoder",
    "workbuddy",
    "zcode",
)

_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text", "Text"}
# Harness markup Claude Code writes into user records (reminders, slash-command echo).
_CLAUDE_MARKUP = (
    "system-reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
)
_CODEBUDDY_MARKUP = (
    *_CLAUDE_MARKUP,
    "conversation_history_summary",
    "task-notification",
)
# Harness markup Agentica writes into user records on context-window cuts / budget reminders.
_AGENTICA_MARKUP = (
    "context_window",
    "session_notes",
    "dropped_span",
)
_AGENTICA_WINDOW_MARK = (
    "New context window started. Continue from session notes and search_session."
)
_CURSOR_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# Cursor stamps each prompt: ``<timestamp>Wednesday, Oct 14, 2026, 1:16 PM (UTC+8)</timestamp>``.
_CURSOR_TIME_RE = re.compile(
    r"<timestamp>\w+, (\w{3} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M) "
    r"\(UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?\)</timestamp>"
)
# Tool arguments that carry a file's text: output, not memory material. A
# string input (``ApplyPatch``) is a whole patch and is dropped the same way.
_CURSOR_WRITE_KEYS = ("contents", "old_string", "new_string")


class TranscriptError(RuntimeError):
    """A host transcript this reader cannot parse (half-written, or a shape it does
    not know). Raised by ``scan_file``; ``scan_sources`` skips the file and goes on."""


class HubWriter(Protocol):
    async def write(
        self,
        *,
        content: str = "",
        messages: list[dict] | None = None,
        session_id: str = "",
        memory_at: int | None = None,
        request_id: str,
        raw_only: bool = False,
        workspace_id: str | None = None,
        branch: str | None = None,
        tool_signals: list[dict] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    session_id: str
    at: int | None
    # Reader positions before and after this record; a cursor stores ``end``.
    start: int
    end: int
    # Tool calls / results of this record as OpenAI-style messages.
    steps: tuple[dict, ...] = ()
    workspace: str | None = None
    branch: str | None = None
    signals: tuple[dict, ...] = ()


@dataclass
class Turn:
    session_id: str
    user: str
    assistant: str
    at: int | None
    start: int
    end: int
    steps: list[dict] = field(default_factory=list)
    workspace: str | None = None
    branch: str | None = None
    signals: list[dict] = field(default_factory=list)

    def messages(self) -> list[dict]:
        out: list[dict] = [{"role": "user", "content": self.user}, *self.steps]
        if self.assistant:
            out.append({"role": "assistant", "content": self.assistant})
        return out


def _ms_seconds(value: int | None) -> int | None:
    """CodeBuddy / WorkBuddy / OpenCode record epoch milliseconds."""
    return value // 1000 if value is not None else None


def _iso_seconds(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in _TEXT_BLOCK_TYPES
        and isinstance(block.get("text"), str)
    )


def _strip_tags(text: str, tags: tuple[str, ...]) -> str:
    names = "|".join(re.escape(t) for t in tags)
    return re.sub(rf"<({names})\b[^>]*>.*?</\1\s*>", "", text, flags=re.DOTALL)


# A record parser gets (record, state) and returns (role, text, session_id, at) or None.
# ``state`` carries per-file context such as the Codex session id from session_meta,
# plus ``steps`` (tool calls / results of the record), ``workspace`` and ``branch``.
RecordParser = Callable[[dict, dict], "tuple[str, str, str, int | None] | None"]


def _clean_text(text: str) -> str:
    return sanitize_text(strip_memory_injection(text)).strip()


def _tool_call(call_id: str, name: str, arguments: Any) -> dict:
    arguments = strip_image_payloads(arguments)
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }



def _error_preview(raw: Any) -> str:
    """Sanitized single-line failure summary (<=200 chars) from a failed tool output."""
    text = ""
    if isinstance(raw, dict):
        for key in ("error", "stderr", "message", "detail", "output"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                text = val
                break
    elif isinstance(raw, list):
        text = _blocks_text(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{") and stripped.endswith("}") and len(stripped) <= 32_768:
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                return _error_preview(loaded)
        text = raw
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    lines = [
        ln.strip()
        for ln in cleaned.splitlines()
        if ln.strip() and not _SIGNAL_EXIT_RE.match(ln.strip())
    ]
    if not lines:
        return ""
    joined = " ".join(" | ".join(lines[-2:]).split())
    return joined[:199] + "…" if len(joined) > 200 else joined


def _strip_agentica_markup(text: str) -> str:
    """Strip Agentica context-window preamble blocks while keeping any folded user prose."""
    if not any(f"<{tag}" in text for tag in _AGENTICA_MARKUP):
        return text
    stripped = _strip_tags(text, _AGENTICA_MARKUP)
    return stripped.replace(_AGENTICA_WINDOW_MARK, "")


def _parse_agentica(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    kind = record.get("type")
    if kind not in ("user", "assistant", "tool"):
        return None
    if record.get("cwd"):
        state["workspace"] = record["cwd"]
    if record.get("git_branch"):
        state["branch"] = record["git_branch"]
    session_id = record.get("session_id") or state["session_id"]
    at = _iso_seconds(record.get("timestamp"))
    if kind == "user":
        if record.get("automated") is True or record.get("cron_job") is not None:
            if not record.get("steer"):
                state["automated_turn"] = True
            return None
        text = _strip_agentica_markup(_blocks_text(record.get("content")))
        if not _clean_text(text):
            return None
        state["automated_turn"] = False
        return "user", text, session_id, at
    if state.get("automated_turn"):
        return None
    if kind == "tool":
        raw_out = _blocks_text(record.get("content")) or record.get("content") or record.get("output")
        is_err = (
            record.get("is_error") is True
            or record.get("status") in ("error", "failed")
            or bool(record.get("error"))
        )
        code = _output_exit_code(raw_out)
        if not is_err and code is None:
            return None
        ok = (not is_err) and (code in (None, 0))
        sig: dict[str, Any] = {
            "call_id": str(record.get("tool_call_id") or record.get("id") or ""),
            "tool": str(record.get("name") or record.get("tool_name") or ""),
            "at": at,
            "ok": ok,
        }
        if code is not None:
            sig["exit_code"] = code
        if not ok:
            err = _error_preview(record.get("error") or raw_out)
            if err:
                sig["error"] = err
        state["signals"].append(sig)
        return "assistant", "", session_id, at
    for call in record.get("tool_calls") or []:
        function = call.get("function") or {}
        state["steps"].append(
            _tool_call(str(call.get("id") or ""), str(function.get("name") or ""), function.get("arguments") or {})
        )
    return (
        kind,
        _blocks_text(record.get("content")),
        session_id,
        at,
    )


def _output_exit_code(raw: Any) -> int | None:
    """Extract an explicit integer exit_code from structured JSON or shell header."""
    if isinstance(raw, dict):
        code = raw.get("exit_code")
        if code is None and isinstance(raw.get("metadata"), dict):
            code = raw["metadata"].get("exit_code")
        return code if isinstance(code, int) and not isinstance(code, bool) else None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{") and stripped.endswith("}") and len(stripped) <= 32_768:
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                found = _output_exit_code(loaded)
                if found is not None:
                    return found
        match = _SIGNAL_EXIT_RE.search(raw)
        if match:
            return int(match.group(1))
    return None


def _parse_claude(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    if record.get("type") not in ("user", "assistant") or record.get("isSidechain") or record.get("isMeta"):
        return None
    at = _iso_seconds(record.get("timestamp"))
    content = (record.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            state["steps"].append(
                _tool_call(str(block.get("id") or ""), str(block.get("name") or ""), block.get("input") or {})
            )
        elif block.get("type") == "tool_result":
            raw_res = _blocks_text(block.get("content")) or block.get("content")
            is_err = block.get("is_error") is True
            code = _output_exit_code(raw_res)
            if is_err or code is not None:
                ok = (not is_err) and (code in (None, 0))
                sig: dict[str, Any] = {
                    "call_id": str(block.get("tool_use_id") or ""),
                    "at": at,
                    "ok": ok,
                }
                if code is not None:
                    sig["exit_code"] = code
                if not ok:
                    err = _error_preview(raw_res)
                    if err:
                        sig["error"] = err
                state["signals"].append(sig)
    if record.get("cwd"):
        state["workspace"] = record["cwd"]
    if record.get("gitBranch"):
        state["branch"] = record["gitBranch"]
    text = _blocks_text(content)
    return (
        record["type"],
        _strip_tags(text, _CLAUDE_MARKUP),
        record.get("sessionId") or state["session_id"],
        at,
    )


def _parse_codebuddy(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """CodeBuddy and WorkBuddy share one JSONL format: ``message`` and ``function_call``."""
    if record.get("cwd"):
        state["workspace"] = record["cwd"]
    if record.get("gitBranch"):
        state["branch"] = record["gitBranch"]
    kind = record.get("type")
    if kind == "session-meta":
        state["session_id"] = record.get("sessionId") or state["session_id"]
        return None
    session_id = record.get("sessionId") or state["session_id"]
    at = _ms_seconds(record.get("timestamp"))
    if kind == "function_call":
        state["steps"].append(
            _tool_call(
                str(record.get("callId") or record.get("id") or ""),
                str(record.get("name") or ""),
                record.get("arguments") or {},
            )
        )
        return "assistant", "", session_id, at
    if kind == "function_call_result":
        raw_out = record.get("output")
        is_err = record.get("isError") is True or record.get("status") in ("error", "failed")
        code = _output_exit_code(raw_out)
        if not is_err and code is None:
            return None
        ok = (not is_err) and (code in (None, 0))
        sig = {
            "call_id": str(record.get("callId") or record.get("id") or ""),
            "tool": str(record.get("name") or ""),
            "at": at,
            "ok": ok,
        }
        if code is not None:
            sig["exit_code"] = code
        if not ok:
            err = _error_preview(raw_out)
            if err:
                sig["error"] = err
        state["signals"].append(sig)
        return "assistant", "", session_id, at
    role = record.get("role")
    if kind != "message" or role not in ("user", "assistant"):
        return None
    text = _blocks_text(record.get("content"))
    if role == "user":
        query = _CURSOR_QUERY_RE.search(text)
        text = query.group(1) if query else _strip_tags(text, _CODEBUDDY_MARKUP)
    return role, text, session_id, at


def _parse_codex(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    payload = record.get("payload") or {}
    if record.get("type") == "session_meta":
        state["session_id"] = payload.get("id") or state["session_id"]
        state["workspace"] = payload.get("cwd") or state["workspace"]
        state["branch"] = (payload.get("git") or {}).get("branch") or state["branch"]
        return None
    if record.get("type") == "turn_context":
        state["workspace"] = payload.get("cwd") or state["workspace"]
        return None
    if record.get("type") == "response_item":
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call"):
            state["steps"].append(_tool_call(
                str(payload.get("call_id") or ""),
                str(payload.get("name") or ""),
                payload.get("arguments", payload.get("input")),
            ))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            raw_out = payload.get("output")
            at = _iso_seconds(record.get("timestamp"))
            code = _output_exit_code(raw_out)
            is_err = payload.get("is_error") is True or payload.get("status") in ("error", "failed")
            if not is_err and isinstance(raw_out, str) and raw_out.strip().startswith("{"):
                try:
                    loaded = json.loads(raw_out)
                except json.JSONDecodeError:
                    loaded = None
                if isinstance(loaded, dict) and (loaded.get("is_error") is True or bool(loaded.get("error"))):
                    is_err = True
            if code is None and not is_err:
                return None
            ok = (not is_err) and (code in (None, 0))
            sig = {
                "call_id": str(payload.get("call_id") or ""),
                "at": at,
                "ok": ok,
            }
            if code is not None:
                sig["exit_code"] = code
            if not ok:
                err = _error_preview(raw_out)
                if err:
                    sig["error"] = err
            state["signals"].append(sig)
        else:
            return None
        return "assistant", "", state["session_id"], _iso_seconds(record.get("timestamp"))
    if record.get("type") != "event_msg":
        return None
    at = _iso_seconds(record.get("timestamp"))
    kind = payload.get("type")
    if kind == "item_completed":
        item = payload.get("item") or {}
        role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(
            str(item.get("type"))
        )
        if role is None:
            return None
        return role, _blocks_text(item.get("content")), state["session_id"], at
    # Rollouts written before item events existed.
    role = {"user_message": "user", "agent_message": "assistant"}.get(str(kind))
    if role is None:
        return None
    return role, str(payload.get("message") or ""), state["session_id"], at


def _parse_cursor(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    role = record.get("role")
    if role not in ("user", "assistant"):
        return None
    content = (record.get("message") or {}).get("content")
    text = _blocks_text(content)
    if role == "user":
        # Cursor wraps what the user typed in <user_query>; user records without
        # it carry harness context (tool / MCP catalogs).
        query = _CURSOR_QUERY_RE.search(text)
        if query is None:
            return None
        state["at"] = _cursor_time(text) or state["at"]
        text = query.group(1)
    for block in content if isinstance(content, list) else []:
        if block.get("type") != "tool_use":
            continue
        args = block.get("input")
        args = (
            {k: v for k, v in args.items() if k not in _CURSOR_WRITE_KEYS}
            if isinstance(args, dict)
            else {}
        )
        if state["workspace"] is None:
            state["workspace"] = _workspace_in_args(args, state["project"])
        state["steps"].append(_tool_call("", str(block.get("name") or ""), args))
    return role, text, state["session_id"], state["at"]


def _cursor_time(text: str) -> int | None:
    match = _CURSOR_TIME_RE.search(text)
    if match is None:
        return None
    stamp, sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
    tz = timezone(-offset if sign == "-" else offset)
    return int(datetime.strptime(stamp, "%b %d, %Y, %I:%M %p").replace(tzinfo=tz).timestamp())


def _slug(name: str) -> str:
    """Cursor names a project directory by its path with every non-alphanumeric run as ``-``."""
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")


def _workspace_of_slug(slug: str) -> str | None:
    """The one existing directory whose path Cursor encodes as ``slug``."""
    found: list[str] = []

    def walk(directory: str, rest: str) -> None:
        try:
            entries = [e for e in os.scandir(directory) if e.is_dir()]
        except OSError:
            return
        for entry in entries:
            head = _slug(entry.name)
            if not head:
                continue
            if rest == head:
                found.append(entry.path)
            elif rest.startswith(f"{head}-"):
                walk(entry.path, rest[len(head) + 1:])

    walk("/", slug)
    # ``a-b`` is both ``/a-b`` and ``/a/b``; two real directories leave it undecided.
    return found[0] if len({os.path.realpath(p) for p in found}) == 1 else None


def _workspace_in_args(args: dict, slug: str) -> str | None:
    """A directory named by a tool's path argument that ``slug`` encodes — how a
    project that has since moved or been deleted is still placed."""
    for value in args.values():
        if isinstance(value, str) and value.startswith("/") and "\n" not in value:
            named = Path(value)
            for candidate in (named, *named.parents):
                if _slug(str(candidate)) == slug:
                    return str(candidate)
    return None


def _read_cursor(path: Path, start: int) -> tuple[list[Message], int]:
    """``~/.cursor/projects/<slug>/agent-transcripts/...``: records carry no working
    directory, so the project comes from ``<slug>``; turns without a timestamp tag
    take the previous one, or the file's mtime before the first."""
    slug = path.parts[path.parts.index("agent-transcripts") - 1]
    return _read_jsonl(
        path,
        start,
        _parse_cursor,
        project=slug,
        workspace=_workspace_of_slug(slug),
        at=int(path.stat().st_mtime),
    )


def _record(line: bytes) -> dict | None:
    """A complete JSONL line as an object; partial or malformed lines are None."""
    if not line.endswith(b"\n"):
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _read_stream(
    fp: BinaryIO, size: int, start: int, parse: RecordParser, state: dict[str, Any]
) -> tuple[list[Message], int]:
    """Messages after byte ``start`` of a JSONL stream of ``size`` bytes.

    Stops before a trailing line still being written.
    """
    if start > size:
        start = 0  # the transcript was rewritten or truncated
    messages: list[Message] = []
    position = start
    if start:
        # Codex names the session in its first record; resumed reads need it too.
        head = _record(fp.readline())
        if head is not None:
            parse(head, state)
        fp.seek(start)
    for line in fp:
        if not line.endswith(b"\n"):
            break
        line_start, position = position, position + len(line)
        record = _record(line)
        state["steps"] = []
        state["signals"] = []
        parsed = parse(record, state) if record is not None else None
        if parsed is None:
            continue
        role, text, session_id, at = parsed
        text = _clean_text(text)
        steps = tuple(state["steps"])
        signals = tuple(state["signals"])
        if text or steps or signals:
            messages.append(Message(
                role, text, session_id, at, line_start, position,
                steps, state["workspace"], state["branch"], signals,
            ))
    return messages, position


def _read_jsonl(
    path: Path, start: int, parse: RecordParser, **seed: Any
) -> tuple[list[Message], int]:
    """Messages after byte ``start`` of a JSONL file.

    ``seed`` adds per-file context to the parser state (Cursor's project and time).
    """
    state: dict[str, Any] = {
        "session_id": path.stem, "steps": [], "signals": [], "workspace": None, "branch": None,
        **seed,
    }
    with path.open("rb") as fp:
        return _read_stream(fp, path.stat().st_size, start, parse, state)


def _unzstd(path: Path) -> bytes:
    """The text of a zstd file, up to the frame the writer has not finished."""
    chunks: list[bytes] = []
    with path.open("rb") as fp:
        reader = zstandard.ZstdDecompressor().stream_reader(fp, read_across_frames=True)
        try:
            while chunk := reader.read(1 << 20):
                chunks.append(chunk)
        except zstandard.ZstdError:
            # A session still being written ends mid-frame; what decoded is complete.
            pass
    return b"".join(chunks)


def _kimi_cli_workspace(path: Path) -> str | None:
    """kimi-cli names a session directory ``md5(work dir)``; ``kimi.json`` lists them."""
    index = path.parents[3] / "kimi.json"
    if not index.is_file():
        return None
    for item in json.loads(index.read_text(encoding="utf-8"))["work_dirs"]:
        if hashlib.md5(item["path"].encode()).hexdigest() == path.parents[1].name:
            return item["path"]
    return None


def _parse_kimi_cli(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """kimi-cli ``context.jsonl``: ``user`` / ``assistant`` records; ``tool`` output,
    ``_checkpoint`` and ``_usage`` are not conversation."""
    role = record.get("role")
    if role not in ("user", "assistant"):
        return None
    for call in record.get("tool_calls") or []:
        function = call["function"]
        state["steps"].append(_tool_call(call["id"], function["name"], function["arguments"]))
    return role, _blocks_text(record.get("content")), state["session_id"], state["at"]


def _read_kimi_cli(path: Path, start: int) -> tuple[list[Message], int]:
    # context.jsonl has no clock; the session's last write is the best time it records.
    meta = path.with_name("metadata.json")
    wire_mtime = (
        json.loads(meta.read_text(encoding="utf-8")).get("wire_mtime")
        if meta.is_file()
        else None
    )
    at = int(wire_mtime if wire_mtime is not None else path.stat().st_mtime)
    return _read_jsonl(
        path,
        start,
        _parse_kimi_cli,
        session_id=path.parent.name,
        workspace=_kimi_cli_workspace(path),
        at=at,
    )


# pi's ``write`` carries the file in ``content``, ``edit`` the text in ``oldText`` /
# ``newText``: output, not memory material.
_PI_WRITE_KEYS = ("content", "oldText", "newText")


def _parse_pi(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """pi / OpenClaw session (pi session format v3). The header names the session and
    the project; ``message`` entries are ``user`` / ``assistant`` (``text`` blocks are
    the answer, ``toolCall`` blocks are steps, ``thinking`` is not kept); ``toolResult``
    and ``bashExecution`` are how the answer was reached, and ``custom_message`` is an
    extension's injected context, none of them conversation."""
    kind = record.get("type")
    if kind == "session":
        state["session_id"] = record["id"]
        state["workspace"] = record.get("cwd")
        return None
    if kind != "message":
        return None
    message = record["message"]
    role = message["role"]
    if role == "bashExecution":
        code = message.get("exitCode")
        if isinstance(code, bool) or not isinstance(code, int):
            return None
        raw_at = message.get("timestamp")
        if isinstance(raw_at, int) and not isinstance(raw_at, bool):
            at = _ms_seconds(raw_at)
        elif isinstance(raw_at, str):
            at = _iso_seconds(raw_at)
        else:
            at = _iso_seconds(record.get("timestamp"))
        sig = {
            "tool": "bash",
            "target": str(message.get("command") or "")[:120],
            "at": at,
            "ok": code == 0,
            "exit_code": code,
        }
        if code != 0:
            err = _error_preview(message.get("output"))
            if err:
                sig["error"] = err
        state["signals"].append(sig)
        return "assistant", "", state["session_id"], at
    if role == "toolResult" and message.get("isError") is True:
        at = _ms_seconds(message.get("timestamp"))
        sig = {
            "call_id": str(message.get("toolCallId") or ""),
            "tool": str(message.get("toolName") or "tool"),
            "at": at,
            "ok": False,
        }
        err = _error_preview(message.get("content"))
        if err:
            sig["error"] = err
        state["signals"].append(sig)
        return "assistant", "", state["session_id"], at
    if role not in ("user", "assistant"):
        return None
    at = _ms_seconds(message.get("timestamp"))
    content = message.get("content")
    if role == "user":
        return role, _blocks_text(content), state["session_id"], at
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            raw_args = block.get("arguments") or {}
            args = (
                {k: v for k, v in raw_args.items() if k not in _PI_WRITE_KEYS}
                if isinstance(raw_args, dict)
                else {}
            )
            state["steps"].append(
                _tool_call(str(block.get("id") or ""), str(block.get("name") or ""), args)
            )
    return role, _blocks_text(content), state["session_id"], at


def _read_pi(path: Path, start: int) -> tuple[list[Message], int]:
    return _read_jsonl(path, start, _parse_pi)


_COPILOT_WRITE_KEYS = ("file_text", "old_str", "new_str", "patch", "content")


def _parse_copilot(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """GitHub Copilot ``events.jsonl``: ``session.start`` names the session and workspace;
    ``user.message`` (skipping ``source == "system"`` hook injections), ``assistant.message``
    and ``tool.execution_start`` steps."""
    if record.get("ephemeral"):
        return None
    kind = record.get("type")
    data = record.get("data") or {}
    at = _iso_seconds(record.get("timestamp"))
    if kind == "session.start":
        state["session_id"] = data.get("sessionId") or state["session_id"]
        ctx = data.get("context") or {}
        state["workspace"] = ctx.get("cwd") or data.get("cwd") or state["workspace"]
        state["branch"] = ctx.get("branch") or state["branch"]
        return None
    if kind == "user.message":
        if data.get("source") == "system":
            return None
        return "user", str(data.get("content") or ""), state["session_id"], at
    if kind == "tool.execution_start":
        raw_args = data.get("arguments") or {}
        args = (
            {k: v for k, v in raw_args.items() if k not in _COPILOT_WRITE_KEYS}
            if isinstance(raw_args, dict)
            else {}
        )
        state["steps"].append(
            _tool_call(str(data.get("toolCallId") or ""), str(data.get("toolName") or ""), args)
        )
        return "assistant", "", state["session_id"], at
    if kind == "tool.execution_complete" and data.get("success") is False:
        sig = {
            "call_id": str(data.get("toolCallId") or ""),
            "tool": str(data.get("toolName") or "tool"),
            "at": at,
            "ok": False,
        }
        err = _error_preview(data.get("error") or data.get("result"))
        if err:
            sig["error"] = err
        state["signals"].append(sig)
        return "assistant", "", state["session_id"], at
    if kind == "assistant.message":
        for req in data.get("toolRequests") or []:
            if not isinstance(req, dict):
                continue
            raw_args = req.get("arguments") or {}
            args = (
                {k: v for k, v in raw_args.items() if k not in _COPILOT_WRITE_KEYS}
                if isinstance(raw_args, dict)
                else {}
            )
            state["steps"].append(
                _tool_call(str(req.get("toolCallId") or ""), str(req.get("name") or ""), args)
            )
        return "assistant", str(data.get("content") or ""), state["session_id"], at
    return None


def _read_copilot(path: Path, start: int) -> tuple[list[Message], int]:
    return _read_jsonl(path, start, _parse_copilot, session_id=path.parent.name)


_KIRO_WRITE_KEYS = ("text", "content", "oldStr", "newStr", "old_string", "new_string")


def _parse_kiro(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """Kiro ``messages.jsonl``: structured ``payload.type`` (``user`` / ``assistant`` /
    ``tool_call``), skipping synthetic workflow prompts."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    meta = (record.get("_meta") or {}).get("kiro") or {}
    if meta.get("syntheticUserMessageReason"):
        return None
    kind = payload.get("type")
    at = _iso_seconds(record.get("timestamp")) or state["at"]
    if kind in ("user", "assistant"):
        return kind, _blocks_text(payload.get("content")), state["session_id"], at
    if kind == "tool_call":
        raw_args = payload.get("args") if "args" in payload else payload.get("arguments")
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                raw_args = {}
        args = (
            {k: v for k, v in raw_args.items() if k not in _KIRO_WRITE_KEYS}
            if isinstance(raw_args, dict)
            else {}
        )
        state["steps"].append(
            _tool_call(str(payload.get("id") or ""), str(payload.get("name") or ""), args)
        )
        return "assistant", "", state["session_id"], at
    return None


def _read_kiro(path: Path, start: int) -> tuple[list[Message], int]:
    meta_file = path.with_name("session.json")
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    session_id = meta.get("id") or path.parent.name
    workspaces = meta.get("workspacePaths") or []
    workspace = workspaces[0] if workspaces else None
    at = _iso_seconds(meta.get("createdAt")) or int(path.stat().st_mtime)
    return _read_jsonl(
        path, start, _parse_kiro, session_id=session_id, workspace=workspace, at=at
    )


_CLINE_WRITE_KEYS = ("content", "diff", "old_str", "new_str", "old_string", "new_string")


def _read_cline(path: Path, start: int) -> tuple[list[Message], int]:
    """Cline SDK ``<id>.messages.json``: JSON object whose ``messages`` array is rewritten
    as turns complete; cursor is the index into ``messages``."""
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if data.get("agent") in ("subagent", "teammate"):
        return [], start
    session_id = data.get("sessionId") or path.parent.name
    manifest_file = path.with_name(f"{session_id}.json")
    manifest = (
        json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest_file.is_file()
        else {}
    )
    workspace = manifest.get("cwd") or manifest.get("workspace_root")
    base_at = _iso_seconds(manifest.get("started_at") or data.get("updated_at")) or int(
        path.stat().st_mtime
    )
    items = data.get("messages") or []
    if start > len(items):
        start = 0
    messages: list[Message] = []
    for idx in range(start, len(items)):
        msg = items[idx]
        role = msg.get("role")
        meta = msg.get("metadata") or {}
        if role not in ("user", "assistant") or meta.get("displayOnly") or meta.get("displayRole") == "system":
            continue
        at = _ms_seconds(msg.get("ts")) or base_at
        content = msg.get("content")
        steps = []
        signals = []
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    raw_inp = block.get("input") or {}
                    inp = (
                        {k: v for k, v in raw_inp.items() if k not in _CLINE_WRITE_KEYS}
                        if isinstance(raw_inp, dict)
                        else {}
                    )
                    steps.append(
                        _tool_call(
                            str(block.get("id") or block.get("call_id") or ""),
                            str(block.get("name") or ""),
                            inp,
                        )
                    )
        elif role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") is True:
                    sig = {"call_id": str(block.get("tool_use_id") or ""), "at": at, "ok": False}
                    err = _error_preview(block.get("content"))
                    if err:
                        sig["error"] = err
                    signals.append(sig)
        text = _clean_text(_blocks_text(content))
        if text or steps or signals:
            messages.append(
                Message(role, text, session_id, at, idx, idx + 1, tuple(steps), workspace, None, tuple(signals))
            )
    return messages, len(items)


def _parse_kimi_code(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """kimi-code ``agents/main/wire.jsonl``: the user's message is a
    ``context.append_message`` whose origin is the user (the rest are injected
    reminders); the answer streams as ``text`` parts of a step and is complete at
    ``step.end``; ``tool.call`` is a step. ``think`` parts and tool results are not kept."""
    at = _ms_seconds(record.get("time"))
    if record.get("type") == "context.append_message":
        message = record["message"]
        if message["role"] == "user" and (message.get("origin") or {}).get("kind") == "user":
            return "user", _blocks_text(message["content"]), state["session_id"], at
        return None
    if record.get("type") != "context.append_loop_event":
        return None
    event = record["event"]
    if event["type"] == "content.part" and event["part"]["type"] == "text":
        state["text"] += event["part"]["text"]
    elif event["type"] == "tool.call":
        state["steps"].append(_tool_call(event["toolCallId"], event["name"], event["args"]))
        return "assistant", "", state["session_id"], at
    elif event["type"] == "step.end":
        text, state["text"] = state["text"], ""
        return "assistant", text, state["session_id"], at
    return None


def _read_kimi_code(path: Path, start: int) -> tuple[list[Message], int]:
    session = path.parents[2]
    state_file = session / "state.json"
    workspace = (
        json.loads(state_file.read_text(encoding="utf-8")).get("cwd")
        if state_file.is_file()
        else None
    )
    return _read_jsonl(
        path, start, _parse_kimi_code, session_id=session.name, workspace=workspace, text=""
    )


def _parse_dsh(record: dict, state: dict) -> tuple[str, str, str, int | None] | None:
    """DeepSeek Harness events: the session header names the project and whether it is
    a subagent's; ``user/message`` from the user, ``assistant/message`` text blocks (the
    ``reasoning`` blocks are not the answer) and ``tool/call`` steps."""
    kind = record.get("type")
    if kind == "session":
        state["session_id"] = record["id"]
        state["workspace"] = record.get("cwd")
        state["subagent"] = record.get("origin") == "subagent"
        return None
    if state.get("subagent"):
        return None
    data = record.get("data") or {}
    at = _ms_seconds(record.get("time"))
    if kind == "tool/call":
        state["steps"].append(_tool_call(data["callId"], data["name"], data["arguments"]))
        return "assistant", "", state["session_id"], at
    if kind == "user/message" and data["source"]["kind"] == "user":
        return "user", _blocks_text(data["content"]), state["session_id"], at
    if kind == "assistant/message":
        return "assistant", _blocks_text(data["message"]["content"]), state["session_id"], at
    return None


def _read_dsh(path: Path, start: int) -> tuple[list[Message], int]:
    """Cursor positions are offsets into the decompressed events."""
    raw = _unzstd(path)
    state: dict[str, Any] = {
        "session_id": path.parent.name, "steps": [], "signals": [], "workspace": None, "branch": None,
    }
    return _read_stream(io.BytesIO(raw), len(raw), start, _parse_dsh, state)


def _read_hermes(path: Path, start: int) -> tuple[list[Message], int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, content, timestamp FROM messages "
            "WHERE id > ? AND role IN ('user', 'assistant') AND active = 1 "
            "AND _compressed_summary = 0 ORDER BY id",
            (start,),
        ).fetchall()
    finally:
        conn.close()
    messages = [
        Message(role, text, session_id, int(ts), row_id - 1, row_id)
        for row_id, session_id, role, content, ts in rows
        if (text := _clean_text(content or ""))
    ]
    return messages, rows[-1][0] if rows else start


def _read_opencode(path: Path, start: int) -> tuple[list[Message], int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT p.rowid, s.id, s.directory, m.time_created, m.data, p.data "
            "FROM part p "
            "JOIN message m ON m.id = p.message_id "
            "JOIN session s ON s.id = p.session_id "
            "WHERE p.rowid > ? AND s.parent_id IS NULL "
            "ORDER BY p.rowid",
            (start,),
        ).fetchall()
    finally:
        conn.close()
    messages: list[Message] = []
    for row_id, session_id, directory, created_ms, msg_json, part_json in rows:
        msg = json.loads(msg_json)
        role = msg.get("role")
        if role not in ("user", "assistant") or msg.get("summary") is True:
            continue
        part = json.loads(part_json)
        at = _ms_seconds(created_ms)
        workspace = directory or (msg.get("path") or {}).get("cwd") or None
        kind = part.get("type")
        if kind == "text" and not part.get("synthetic"):
            text = _clean_text(part.get("text") or "")
            if text:
                messages.append(
                    Message(role, text, session_id, at, row_id - 1, row_id, (), workspace)
                )
        elif role == "assistant" and kind == "tool":
            # Arguments locate the files touched; a write's ``content`` is output, not kept.
            state_obj = part["state"]
            inp = {k: v for k, v in state_obj["input"].items() if k != "content"}
            call_id = str(part.get("callID") or "")
            tool_name = str(part.get("tool") or "")
            step = _tool_call(call_id, tool_name, inp)
            sig = ()
            if state_obj.get("status") == "error":
                err = _error_preview(state_obj.get("error") or state_obj.get("output"))
                sig = (
                    {
                        "call_id": call_id,
                        "tool": tool_name or "tool",
                        "at": at,
                        "ok": False,
                        **({"error": err} if err else {}),
                    },
                )
            messages.append(
                Message("assistant", "", session_id, at, row_id - 1, row_id, (step,), workspace, None, sig)
            )
    return messages, rows[-1][0] if rows else start


@dataclass(frozen=True)
class SourceSpec:
    name: str
    # Relative to $HOME.
    root: str
    discover: Callable[[Path], list[Path]]
    read: Callable[[Path, int], tuple[list[Message], int]]


_AGENTICA_EXCLUDED_SOURCES = frozenset({"sdk", "subagent", "cron", "workflow"})


def _is_agentica_excluded(path: Path) -> bool:
    """True when ``<session>.meta.json`` marks a non-human RunSource.

    SDK, subagent, cron, and workflow runs are machine-driven and excluded;
    CLI, Web, Desktop, and IM transcripts (``cli``, ``gateway``, channel names, or
    legacy transcripts without a sidecar) belong to the user and are ingested.
    """
    meta_path = path.with_name(f"{path.stem}.meta.json")
    if not meta_path.is_file():
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    src = str(data.get("source") or "").strip().lower()
    if src:
        return src in _AGENTICA_EXCLUDED_SOURCES
    origin = str(data.get("origin") or "").strip().lower()
    return origin in _AGENTICA_EXCLUDED_SOURCES


def _agentica_project_workspace(path: Path) -> str:
    proj_file = path.parent / "project.json"
    if not proj_file.is_file():
        return ""
    try:
        data = json.loads(proj_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return canonical_workspace(data.get("work_dir"))


def _jsonl_files(
    pattern: str,
    *,
    inside: str | None = None,
    exclude: Callable[[Path], bool] | None = None,
) -> Callable[[Path], list[Path]]:
    def discover(root: Path) -> list[Path]:
        files = [
            p
            for p in root.rglob(pattern)
            if "subagents" not in p.parts
            and (inside is None or inside in p.parts)
            and (exclude is None or not exclude(p))
        ]
        return sorted(files, key=lambda p: p.stat().st_mtime_ns)

    return discover


SOURCE_SPECS: dict[str, SourceSpec] = {
    "agentica": SourceSpec(
        "agentica",
        ".agentica/projects",
        _jsonl_files("*.jsonl", exclude=_is_agentica_excluded),
        lambda p, s: _read_jsonl(p, s, _parse_agentica),
    ),
    "claude-code": SourceSpec(
        "claude-code",
        ".claude/projects",
        _jsonl_files("*.jsonl"),
        lambda p, s: _read_jsonl(p, s, _parse_claude),
    ),
    "cline": SourceSpec(
        "cline",
        ".cline/data/sessions",
        lambda root: sorted(
            (p for p in root.glob("*/*.messages.json") if "__" not in p.name),
            key=lambda p: p.stat().st_mtime_ns,
        ),
        _read_cline,
    ),
    "codebuddy": SourceSpec(
        "codebuddy",
        ".codebuddy/projects",
        _jsonl_files("*.jsonl"),
        lambda p, s: _read_jsonl(p, s, _parse_codebuddy),
    ),
    "codex": SourceSpec(
        "codex",
        ".codex/sessions",
        _jsonl_files("rollout-*.jsonl"),
        lambda p, s: _read_jsonl(p, s, _parse_codex),
    ),
    "copilot": SourceSpec(
        "copilot",
        ".copilot/session-state",
        lambda root: sorted(root.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime_ns),
        _read_copilot,
    ),
    "cursor": SourceSpec(
        "cursor",
        ".cursor/projects",
        _jsonl_files("*.jsonl", inside="agent-transcripts"),
        _read_cursor,
    ),
    "dsh": SourceSpec(
        "dsh",
        ".dsh/sessions",
        lambda root: sorted(root.rglob("session.v4.jsonl.zstd"), key=lambda p: p.stat().st_mtime_ns),
        _read_dsh,
    ),
    "hermes": SourceSpec(
        "hermes",
        ".hermes/state.db",
        lambda root: [root] if root.is_file() else [],
        _read_hermes,
    ),
    "kimi-cli": SourceSpec(
        "kimi-cli",
        ".kimi/sessions",
        lambda root: sorted(root.glob("*/*/context.jsonl"), key=lambda p: p.stat().st_mtime_ns),
        _read_kimi_cli,
    ),
    "kimi-code": SourceSpec(
        "kimi-code",
        ".kimi-code/sessions",
        lambda root: sorted(
            root.glob("*/session_*/agents/main/wire.jsonl"), key=lambda p: p.stat().st_mtime_ns
        ),
        _read_kimi_code,
    ),
    "kiro": SourceSpec(
        "kiro",
        ".kiro/sessions",
        lambda root: sorted(
            root.rglob("sess_*/messages.jsonl"), key=lambda p: p.stat().st_mtime_ns
        ),
        _read_kiro,
    ),
    "openclaw": SourceSpec(
        "openclaw",
        ".openclaw/agents",
        lambda root: sorted(
            root.glob("*/sessions/*.jsonl"), key=lambda p: p.stat().st_mtime_ns
        ),
        _read_pi,
    ),
    "opencode": SourceSpec(
        "opencode",
        ".local/share/opencode/opencode.db",
        lambda root: [root] if root.is_file() else [],
        _read_opencode,
    ),
    "pi": SourceSpec(
        "pi",
        ".pi/agent/sessions",
        lambda root: sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime_ns),
        _read_pi,
    ),
    "qoder": SourceSpec(
        "qoder",
        ".qoder/projects",
        _jsonl_files("*.jsonl"),
        lambda p, s: _read_jsonl(p, s, _parse_claude),
    ),
    "workbuddy": SourceSpec(
        "workbuddy",
        ".workbuddy/projects",
        _jsonl_files("*.jsonl"),
        lambda p, s: _read_jsonl(p, s, _parse_codebuddy),
    ),
    "zcode": SourceSpec(
        "zcode",
        ".zcode/cli/db/db.sqlite",
        lambda root: [root] if root.is_file() else [],
        _read_opencode,
    ),
}


def default_source_roots(home: Path | None = None) -> dict[str, Path]:
    base = home or Path.home()
    return {name: base / spec.root for name, spec in SOURCE_SPECS.items()}


def build_turns(messages: list[Message]) -> tuple[list[Turn], Turn | None]:
    """Group messages into turns; the last turn is returned apart as still open.

    Consecutive user messages form one request; an assistant message replaces the
    turn's answer, so the final text wins. Tool calls and results in between are
    the turn's steps, in order. Messages with no user message before them in this
    slice belong to a turn that was already ingested.
    """
    turns: list[Turn] = []
    current: Turn | None = None
    # Resolving a workspace walks the filesystem, and every message of a turn carries
    # the same few directories; keep this call's answers instead of repeating it.
    resolved: dict[str | None, str | None] = {}
    for msg in messages:
        if msg.workspace not in resolved:
            resolved[msg.workspace] = canonical_workspace(msg.workspace) or None
        workspace = resolved[msg.workspace]
        if msg.role == "user" and msg.text:
            if current and (
                current.assistant or current.steps or current.session_id != msg.session_id
            ):
                turns.append(current)
                current = None
            if current is None:
                current = Turn(
                    msg.session_id, msg.text, "", msg.at, msg.start, msg.end,
                    workspace=workspace, branch=msg.branch,
                )
            else:
                current.user = f"{current.user}\n{msg.text}"
                current.end = msg.end
                current.workspace = workspace or current.workspace
        elif current is not None and current.session_id == msg.session_id:
            current.steps.extend(msg.steps)
            current.signals.extend(msg.signals)
            if msg.role == "assistant" and msg.text:
                current.assistant = msg.text
            current.end = msg.end
            current.workspace = workspace or current.workspace
            current.branch = msg.branch or current.branch
    return [t for t in turns if t.assistant], current


def _batches(turns: list[Turn], size: int) -> Iterator[list[Turn]]:
    batch: list[Turn] = []
    for turn in turns:
        if batch and (len(batch) >= size or batch[-1].session_id != turn.session_id):
            yield batch
            batch = []
        batch.append(turn)
    if batch:
        yield batch


async def scan_file(
    client: HubWriter,
    *,
    source: str,
    path: Path,
    cursors: CursorStore,
    complete: bool = False,
    idle_seconds: int = 300,
    batch_turns: int = 5,
    max_turns: int = 0,
    raw_only: bool = False,
) -> int:
    """Ingest the turns appended to ``path`` since its cursor; returns turns written.

    The open last turn is held back (cursor stays before it) unless the caller
    knows it ended (``complete``: a Stop / run.completed hook) or the transcript
    has been idle for ``idle_seconds``. Each batch of up to ``batch_turns`` turns of
    one session is one write with a content-derived request id. The cursor moves
    only past batches the Core holds durably; an unreachable Core raises and the
    next scan resends from there (same ids, so nothing is stored twice).

    A transcript the reader cannot parse raises ``TranscriptError`` with the cursor
    unmoved: a half-written file parses on a later scan.
    """
    spec = SOURCE_SPECS[source]
    if source == "agentica" and _is_agentica_excluded(path):
        return 0
    key = str(path.resolve())
    with cursors.lock(source, key):
        cursor = cursors.get(source, key)
        try:
            messages, reached = spec.read(path, cursor.position if cursor else 0)
        except Exception as exc:
            # Host formats are third-party and rewritten in place; whatever the
            # parser trips on is a property of this file, not of the scan.
            raise TranscriptError(f"{source} {path}: {type(exc).__name__}: {exc}") from exc
        turns, open_turn = build_turns(messages)
        ended = complete or time.time() - path.stat().st_mtime >= idle_seconds
        if open_turn and open_turn.assistant and ended:
            turns.append(open_turn)
            open_turn = None
        stop = open_turn.start if open_turn else reached
        if max_turns and len(turns) > max_turns:
            turns = turns[:max_turns]
            stop = turns[-1].end
        for batch in _batches(turns, batch_turns):
            messages_in = [m for turn in batch for m in turn.messages()]
            raw_signals = [s for turn in batch for s in turn.signals]
            resolved_signals = (
                extract_tool_signals(messages_in, extra=raw_signals)
                if raw_signals
                else []
            )
            extra_kw = {"tool_signals": resolved_signals} if resolved_signals else {}
            session_id = f"{source}:{batch[0].session_id}"
            request_id = hashlib.sha256(
                json.dumps(
                    [
                        source,
                        batch[0].session_id,
                        batch[0].start,
                        batch[-1].end,
                        messages_in,
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()[:32]
            # Returns once the Core holds the evidence (it retries model work
            # itself); an unreachable Core raises here and the cursor stays.
            await client.write(
                messages=messages_in,
                session_id=session_id,
                memory_at=batch[-1].at,
                request_id=f"memhub:{request_id}",
                raw_only=raw_only,
                workspace_id=batch[-1].workspace,
                branch=batch[-1].branch,
                **extra_kw,
            )
            cursors.advance(source, key, batch[-1].end, len(batch))
        cursors.advance(source, key, stop, 0)
    return len(turns)


async def scan_sources(
    client: HubWriter,
    *,
    hub_dir: str | Path | None = None,
    sources: list[str] | None = None,
    roots: dict[str, Path] | None = None,
    max_turns: int = 0,
    **scan_kwargs: Any,
) -> dict[str, dict[str, int]]:
    """Scan every transcript of ``sources``; ``max_turns`` caps this run in total.

    A transcript that cannot be parsed is skipped and counted in ``errors``; the
    other files are still ingested. An unreachable Core still aborts the scan.
    """
    roots = roots or default_source_roots()
    summary: dict[str, dict[str, int]] = {}
    remaining = max_turns
    with CursorStore(hub_dir) as cursors:
        for source in sources or SOURCES:
            files = SOURCE_SPECS[source].discover(roots[source]) if roots[source].exists() else []
            written = 0
            errors = 0
            for path in files:
                if max_turns and remaining <= 0:
                    break
                try:
                    n = await scan_file(
                        client,
                        source=source,
                        path=path,
                        cursors=cursors,
                        max_turns=remaining if max_turns else 0,
                        **scan_kwargs,
                    )
                except TranscriptError as exc:
                    logger.warning("skipped unreadable transcript: %s", exc)
                    errors += 1
                    continue
                written += n
                remaining -= n
            summary[source] = {"files": len(files), "turns": written, "errors": errors}
    return summary


def locate_transcript(
    source: str, payload: dict, roots: dict[str, Path] | None = None
) -> Path | None:
    """The transcript a host hook payload refers to.

    Claude Code / CodeBuddy / Codex / Cursor / WorkBuddy hooks carry ``transcript_path``.
    Agentica's ``run.completed``, OpenCode's ``session.idle`` and Kimi's ``Stop`` carry
    ``session_id``; Cursor names the conversation ``conversation_id``.
    """
    if payload.get("transcript_path") or payload.get("transcriptPath"):
        target = Path(payload.get("transcript_path") or payload["transcriptPath"]).expanduser()
        if source == "agentica" and _is_agentica_excluded(target):
            return None
        return target
    root = (roots or default_source_roots())[source]
    if source in ("hermes", "opencode", "zcode"):
        return root if root.is_file() else None
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
    )
    if not session_id or not root.is_dir():
        return None
    escaped = glob.escape(str(session_id))
    if source == "cline":
        return next(root.glob(f"{escaped}/{escaped}.messages.json"), None)
    if source == "copilot":
        return next(root.glob(f"{escaped}/events.jsonl"), None)
    if source == "kimi-cli":
        return next(root.glob(f"*/{escaped}/context.jsonl"), None)
    if source == "kimi-code":
        return next(root.glob(f"*/{escaped}/agents/main/wire.jsonl"), None)
    if source == "kiro":
        return next(root.rglob(f"{escaped}/messages.jsonl"), None)
    if source == "openclaw":
        return next(root.glob(f"*/sessions/{escaped}*.jsonl"), None)
    if source == "pi":
        return next(root.rglob(f"*_{escaped}.jsonl"), None)
    candidates = [
        p
        for p in root.rglob(f"{escaped}.jsonl")
        if "subagents" not in p.parts
        and (source != "agentica" or not _is_agentica_excluded(p))
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    wanted_ws = canonical_workspace(payload.get("cwd"))
    return max(
        candidates,
        key=lambda p: (
            bool(wanted_ws and _agentica_project_workspace(p) == wanted_ws)
            if source == "agentica"
            else False,
            p.stat().st_mtime_ns,
        ),
    )

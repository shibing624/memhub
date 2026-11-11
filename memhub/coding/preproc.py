# -*- coding: utf-8 -*-
"""Coding-session preprocessing — structural message routing, zero LLM.

Messages are OpenAI-style dicts: role/content, optional assistant ``tool_calls``
(flat ``name``/``arguments`` or nested under ``function``), and ``role="tool"``
results.

What a coding session is remembered by is what the user asked and what the
assistant finally told them. The tool trajectory in between (reads, greps, test
runs and their output) is how the answer was reached, not what is worth
recalling, and it is most of the bytes. So the trajectory contributes only two
structured facts — that this was tool work, and which files it touched — and
the memory pipeline sees the dialogue.
"""
import json
import re
from typing import Any

_EXIT_CODE_RE = re.compile(r"(?m)^(?:Exit code|exit_code):\s*(-?\d+)\s*$")
_MAX_TOOL_SIGNALS = 24

PATH_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "filePath",
    "filename",
    "file",
    "filepath",
    "target_path",
    "notebook_path",
    "src_path",
    "dst_path",
)
_TARGET_KEYS: tuple[str, ...] = (
    "command",
    "cmd",
    *PATH_KEYS,
    "query",
    "pattern",
    "url",
)
# Space a clipped message's elision marker takes; ``fit_dialogue`` budgets for it.
_MARKER_RESERVE = 48


def has_any_tool_message(messages: list[dict]) -> bool:
    """Check if messages contain tool messages or assistant tool_calls."""
    return any(
        m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls"))
        for m in messages
    )


def extract_files(messages: list[dict]) -> list[str]:
    """File paths named by the well-known path arguments of tool calls."""
    paths: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            args = tc.get("arguments", tc.get("function", {}).get("arguments"))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    continue
            if not isinstance(args, dict):
                continue
            for k in PATH_KEYS:
                v = args.get(k)
                if isinstance(v, str) and v:
                    paths.add(v)
    return sorted(paths)


def _int_or_none(val: Any) -> int | None:
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


def _one_line_clip(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _call_target_summary(arguments: Any) -> str:
    """Bounded (<=120 char) command or path summary from tool call arguments."""
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return _one_line_clip(args, 120)
    if not isinstance(args, dict):
        return ""
    for key in _TARGET_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return _one_line_clip(val, 120)
    return ""


def _failure_summary(raw_content: Any, parsed_obj: dict[str, Any] | None) -> str:
    """Bounded (<=200 char) failure summary when a tool call failed."""
    if parsed_obj is not None:
        for key in ("error", "stderr", "message", "detail", "output"):
            val = parsed_obj.get(key)
            if isinstance(val, str) and val.strip():
                return _one_line_clip(val, 200)
    if isinstance(raw_content, str) and raw_content.strip():
        lines = [
            ln.strip()
            for ln in raw_content.splitlines()
            if ln.strip() and not _EXIT_CODE_RE.match(ln.strip())
        ]
        if lines:
            # Keep the tail error line(s), where tracebacks and shell errors land.
            tail = " | ".join(lines[-2:])
            return _one_line_clip(tail, 200)
    return ""


def extract_tool_signals(
    messages: list[dict] | None,
    extra: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Structured execution evidence (call_id, seq, tool, target, at, ok, exit_code, error)."""
    signals: list[dict[str, Any]] = []
    call_meta: dict[str, dict[str, str]] = {}
    for m in messages or []:
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or "")
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(tc.get("name") or fn.get("name") or "")
            raw_args = tc.get("arguments") if "arguments" in tc else fn.get("arguments")
            target = _call_target_summary(raw_args)
            if cid:
                call_meta[cid] = {"tool": name, "target": target}

    seq = 0
    for m in messages or []:
        if m.get("role") != "tool":
            continue
        cid = str(m.get("tool_call_id") or "")
        meta = call_meta.get(cid, {})
        tool = str(m.get("name") or meta.get("tool") or "tool")
        target = meta.get("target") or ""
        at = _int_or_none(m.get("at") or m.get("timestamp"))
        explicit_err = m.get("is_error") if isinstance(m.get("is_error"), bool) else None
        status = m.get("status")
        exit_code = _int_or_none(m.get("exit_code"))
        if exit_code is None:
            exit_code = _int_or_none(m.get("returncode"))

        raw_content = m.get("content")
        parsed_obj: dict[str, Any] | None = None
        if isinstance(raw_content, dict):
            parsed_obj = raw_content
        elif isinstance(raw_content, str):
            stripped = raw_content.strip()
            if stripped.startswith("{") and stripped.endswith("}") and len(stripped) <= 32_768:
                try:
                    loaded = json.loads(stripped)
                    if isinstance(loaded, dict):
                        parsed_obj = loaded
                except json.JSONDecodeError:
                    parsed_obj = None
            if exit_code is None:
                match = _EXIT_CODE_RE.search(raw_content)
                if match:
                    exit_code = int(match.group(1))

        if parsed_obj is not None:
            if explicit_err is None and isinstance(parsed_obj.get("is_error"), bool):
                explicit_err = parsed_obj["is_error"]
            elif explicit_err is None and (
                parsed_obj.get("ok") is False or bool(parsed_obj.get("error"))
            ):
                explicit_err = True
            if exit_code is None:
                exit_code = _int_or_none(parsed_obj.get("exit_code"))
            if exit_code is None:
                exit_code = _int_or_none(parsed_obj.get("returncode"))
            p_meta = parsed_obj.get("metadata")
            if exit_code is None and isinstance(p_meta, dict):
                exit_code = _int_or_none(p_meta.get("exit_code"))

        has_signal = (
            explicit_err is not None
            or exit_code is not None
            or status in ("error", "failed", "completed")
        )
        if not has_signal:
            continue
        failed = (
            explicit_err is True
            or status in ("error", "failed")
            or (exit_code is not None and exit_code != 0)
        )
        entry: dict[str, Any] = {"seq": seq, "tool": tool, "ok": not failed}
        seq += 1
        if cid:
            entry["call_id"] = cid
        if target:
            entry["target"] = target
        if at is not None:
            entry["at"] = at
        if exit_code is not None:
            entry["exit_code"] = exit_code
        if failed:
            err = _failure_summary(raw_content, parsed_obj)
            if err:
                entry["error"] = err
        signals.append(entry)

    for item in extra or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("call_id") or "")
        meta = call_meta.get(cid, {})
        tool = str(item.get("tool") or meta.get("tool") or "tool")
        target = str(item.get("target") or meta.get("target") or "")
        at = _int_or_none(item.get("at"))
        exit_code = _int_or_none(item.get("exit_code"))
        ok = bool(item.get("ok", exit_code == 0 if exit_code is not None else True))
        if exit_code is not None and exit_code != 0:
            ok = False
        entry = {"seq": seq, "tool": tool, "ok": ok}
        seq += 1
        if cid:
            entry["call_id"] = cid
        if target:
            entry["target"] = _one_line_clip(target, 120)
        if at is not None:
            entry["at"] = at
        if exit_code is not None:
            entry["exit_code"] = exit_code
        if not ok and item.get("error"):
            entry["error"] = _one_line_clip(str(item["error"]), 200)
        signals.append(entry)

    if len(signals) > _MAX_TOOL_SIGNALS:
        # Preserve original seq numbers while bounding total stored entries.
        signals = [*signals[:6], *signals[-(_MAX_TOOL_SIGNALS - 6) :]]
    return signals


def _text(content: Any) -> str:
    """Text of a message content: strings as-is, structured parts as JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def clip_text(text: str, max_chars: int) -> str:
    """Keep head and tail of an oversized text; the middle is elided."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} chars elided …]\n{text[-half:]}"


def coding_dialogue(messages: list[dict]) -> list[dict]:
    """Every user message, and the assistant's last text before the next one.

    Tool results, tool-call messages and the narration between calls are
    dropped; what is left is the request and the answer the user read.
    """
    out: list[dict] = []
    answer: dict | None = None
    for m in messages:
        role = m.get("role")
        text = _text(m.get("content")).strip()
        if role == "user":
            if answer is not None:
                out.append(answer)
                answer = None
            if text:
                out.append({"role": "user", "content": text})
        elif role == "assistant" and text:
            answer = {"role": "assistant", "content": text}
    if answer is not None:
        out.append(answer)
    return out


def _caps(lengths: list[int], budget: int) -> list[int]:
    """Water-filling: short items stay whole, long ones share what is left equally."""
    caps = list(lengths)
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    left, remaining = budget, len(lengths)
    for i in order:
        share = max(left // remaining, 0) if remaining else 0
        caps[i] = min(lengths[i], share)
        left -= caps[i]
        remaining -= 1
    return caps


def fit_dialogue(messages: list[dict], max_chars: int) -> list[dict]:
    """Bound a dialogue to ``max_chars`` of content, keeping every message.

    The user's words carry the memory, so they keep up to three quarters of the
    budget (all of it they need, if less); the answers share the rest. An
    oversized message keeps its head and tail.
    """
    if max_chars <= 0:
        return messages
    lengths = [len(m["content"]) for m in messages]
    if sum(lengths) <= max_chars:
        return messages
    budget = max(max_chars - _MARKER_RESERVE * len(messages), 0)
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    others = [i for i, m in enumerate(messages) if m["role"] != "user"]
    user_caps = _caps([lengths[i] for i in users], min(sum(lengths[i] for i in users), budget * 3 // 4))
    other_caps = _caps([lengths[i] for i in others], budget - sum(user_caps))
    caps = dict(zip(users, user_caps)) | dict(zip(others, other_caps))
    return [
        dict(m, content=clip_text(m["content"], caps[i])) for i, m in enumerate(messages)
    ]

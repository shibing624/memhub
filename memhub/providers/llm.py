# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Async OpenAI-compatible LLM client wrapper offering JSON (optionally JSON mode)
and text chat helpers, plus language detection, tolerant JSON parsing and per-call INFO logging.
"""
import itertools
import json
import logging
import re
import time
from collections.abc import Callable
from json.decoder import JSONDecoder
from typing import Any

from openai import AsyncOpenAI
import httpx

from memhub.providers.usage import UsageCallback, UsageEvent, tokens_from_usage

_CJK = re.compile(r"[一-鿿]")

logger = logging.getLogger("memhub.llm")

# Process-wide monotonically increasing counter so every real LLM request gets a
# stable sequence number in the logs (helps eyeball "how many calls did one add cost").
_call_seq = itertools.count(1)

# Default cap for chat_json. Models with json_mode disabled (e.g. Volces ds-v4-flash)
# rely on prose JSON which silently truncates without an explicit max_tokens.
DEFAULT_CHAT_JSON_MAX_TOKENS = 4096

TRUNC_MARKER = "\n...[truncated]...\n"


class LLMOutputTruncatedError(ValueError):
    """The completion stopped at ``max_tokens`` (``finish_reason == "length"``) and the
    cut-off text is not the JSON asked for. Retrying the same request cannot fit; the
    caller must ask for less output (e.g. split the source)."""


def truncate_middle(text: str, max_len: int) -> str:
    """Keep head and tail; drop the middle when ``text`` exceeds ``max_len``.

    Legacy helper for benchmark QA clients; SDK write-path uses chunk+merge instead.
    """
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= len(TRUNC_MARKER) + 2:
        return text[:max_len]
    body = max_len - len(TRUNC_MARKER)
    head_len = body // 2
    tail_len = body - head_len
    return text[:head_len] + TRUNC_MARKER + text[-tail_len:]


def fit_chat_prompt(system: str, user: str, *, max_chars: int) -> tuple[str, str]:
    """Keep ``system`` intact; middle-truncate ``user`` to fit ``max_chars``.

    Used by benchmark QA clients only; SDK ``LLMClient`` chunks long prompts instead.
    """
    if max_chars <= 0 or len(system) + len(user) <= max_chars:
        return system, user
    user_budget = max_chars - len(system)
    if user_budget <= 0:
        return system, ""
    return system, truncate_middle(user, user_budget)


def chunk_text_for_llm(text: str, max_chars: int) -> list[str]:
    """Split *text* into non-overlapping chunks, preferring paragraph/line boundaries."""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            split_at = text.rfind("\n\n", start, end)
            if split_at <= start:
                split_at = text.rfind("\n", start, end)
            if split_at <= start:
                split_at = end
        else:
            split_at = end
        chunk = text[start:split_at]
        if not chunk and split_at < n:
            split_at = min(start + max_chars, n)
            chunk = text[start:split_at]
        if chunk:
            chunks.append(chunk)
        start = max(split_at, start + 1) if split_at == start else split_at
    return chunks or [text[:max_chars]]


def _dedupe_memory_items(items: list) -> list:
    seen: set[str] = set()
    out: list = []
    for item in items:
        if isinstance(item, str):
            key = item.strip()
            payload: Any = item
        elif isinstance(item, dict):
            key = str(item.get("content") or "").strip()
            payload = item
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(payload)
    return out


def merge_extract_results(parts: list[dict]) -> dict:
    """Merge chunked extractor JSON payloads (union lists, dedupe by content)."""
    merged: dict[str, Any] = {
        "identity": [],
        "facts": [],
        "intentions": [],
        "is_ephemeral": True,
        "basic_info": {},
    }
    for part in parts:
        if not isinstance(part, dict) or not part:
            continue
        if not part.get("is_ephemeral", False):
            merged["is_ephemeral"] = False
        merged["identity"].extend(part.get("identity") or [])
        merged["facts"].extend(part.get("facts") or [])
        merged["intentions"].extend(part.get("intentions") or [])
        basic_info = part.get("basic_info")
        if isinstance(basic_info, dict):
            merged["basic_info"].update(basic_info)
    merged["identity"] = _dedupe_memory_items(merged["identity"])
    merged["facts"] = _dedupe_memory_items(merged["facts"])
    merged["intentions"] = _dedupe_memory_items(merged["intentions"])
    return merged


def merge_text_chunks(parts: list[str]) -> str:
    """Join chunked text completions."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def is_chinese(text: str) -> bool:
    """Heuristically decide whether text is Chinese by CJK character ratio."""
    if not text:
        return False
    chinese = len(_CJK.findall(text))
    return chinese / len(text) > 0.1


def _parse_json(content: str):
    """Parse JSON from model output, with partial-recovery fallbacks."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            text = match.group(1)

    # 数组优先用精确正则（hy-memory 方式）：`[ { ... } ]` —— 贪婪 `\[.*\]` 在长输出
    # 会跨到尾部 prose 导致 parse 失败（4B 模型 single-shot ops 实测 5996 字符失败）。
    arr_match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass

    recovered = _recover_partial_object(text)
    if recovered is not None:
        return recovered
    # 裸数组截断恢复（max_tokens 截断时没有闭合 ]）：逐个提取完整 {...} 对象，
    # 丢掉最后半个（取证确认：S2 建图 json_array=True 输出 9-10k 字符被 4096 cap 截断）。
    recovered_array = _recover_partial_array(text)
    if recovered_array is not None:
        return recovered_array
    logger.warning(
        "chat_json output unparseable, returning empty object (len=%d)", len(content)
    )
    return {}


_RECOVERABLE_KEYS = ("facts", "identity", "intentions", "memories", "updates", "ops")


def _recover_partial_object(text: str) -> dict | None:
    """Recover string list values for known keys from truncated JSON."""
    out: dict = {}
    for key in _RECOVERABLE_KEYS:
        m = re.search(rf'"{key}"\s*:\s*\[', text)
        if not m:
            continue
        rest = text[m.end() :]
        decoder = JSONDecoder()
        idx = 0
        items: list[str] = []
        while idx < len(rest):
            while idx < len(rest) and rest[idx] in " \t\n\r,":
                idx += 1
            if idx >= len(rest) or rest[idx] == "]":
                break
            if rest[idx] != '"':
                break
            try:
                value, end = decoder.raw_decode(rest[idx:])
            except json.JSONDecodeError:
                break
            if isinstance(value, str) and value.strip():
                items.append(value.strip())
            idx += end
        if items:
            out[key] = items
    return out or None


def _recover_partial_array(text: str) -> list | None:
    """Recover a truncated bare JSON array of objects (missing closing ``]``).

    Extracts each complete ``{...}`` object via raw_decode and drops the final
    partial object. Used when max_tokens truncation cuts mid-array.
    Also tolerant of a leading ``[`` already stripped by the greedy ``\\{.*\\}``
    branch in _parse_json (fall back to scanning from the first ``{``).
    """
    m = re.search(r"\[\s*\{", text, re.DOTALL)
    if not m:
        m2 = re.match(r"\s*\{", text, re.DOTALL)
        if not m2:
            return None
        rest = text[m2.start() :]
    else:
        rest = text[m.start() + 1 :]
    decoder = JSONDecoder()
    idx = 0
    items: list[dict] = []
    while idx < len(rest):
        while idx < len(rest) and rest[idx] in " \t\n\r,":
            idx += 1
        if idx >= len(rest) or rest[idx] == "]":
            break
        if rest[idx] != "{":
            break
        try:
            value, end = decoder.raw_decode(rest[idx:])
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            items.append(value)
        idx += end
    return items or None


class LLMClient:
    """Async wrapper over an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
        json_mode: bool = True,
        extra_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        usage_callback: UsageCallback | None = None,
        input_max_chars: int = 0,
    ):
        self.model = model
        self.json_mode = json_mode
        self.extra_body = extra_body or {}
        self.usage_callback = usage_callback
        self.input_max_chars = input_max_chars
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            default_headers=extra_headers or None,
            http_client=httpx.AsyncClient(timeout=timeout),
        )

    def _content_char_budget(self, build_system: Callable[[str], str]) -> int:
        """Per-chunk char budget when system+user both embed the same content."""
        if self.input_max_chars <= 0:
            return 0
        overhead = len(build_system(""))
        return max(1, (self.input_max_chars - overhead) // 2)

    def _completion_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        """Merge caller kwargs with configured extra_body (e.g. thinking depth)."""
        out = dict(kwargs)
        if self.extra_body:
            out["extra_body"] = self.extra_body
        return out

    def _log_call(self, kind: str, elapsed_ms: float) -> None:
        """Emit an INFO log per real LLM request with a global sequence number."""
        seq = next(_call_seq)
        logger.info(
            "LLM call #%d kind=%s model=%s took=%.0fms",
            seq,
            kind,
            self.model,
            elapsed_ms,
        )

    def _after_call(self, kind: str, resp: Any, start: float) -> None:
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._log_call(kind, elapsed_ms)
        if self.usage_callback is None:
            return
        prompt_tokens, completion_tokens = tokens_from_usage(resp)
        details = getattr(
            getattr(resp, "usage", None), "completion_tokens_details", None
        )
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if not isinstance(reasoning_tokens, int):
            reasoning_tokens = None
        reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
        self.usage_callback(
            UsageEvent(
                kind=kind,
                model=self.model,
                latency_ms=elapsed_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                reasoning_chars=len(reasoning) if isinstance(reasoning, str) else None,
            )
        )

    async def aclose(self) -> None:
        await self.client.close()

    async def _chat_json_once(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        json_object: bool | None,
        json_array: bool = False,
        max_tokens: int,
    ) -> tuple[dict | list, bool]:
        """The parsed reply, and whether the completion hit ``max_tokens``."""
        # json_array=True disables json_object mode (which only allows objects)
        use_json_mode = (
            False
            if json_array
            else (self.json_mode if json_object is None else json_object)
        )
        kwargs: dict = {"max_tokens": max_tokens}
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            # Some gateways only scan the user/input message, and reject
            # response_format=json_object unless that text contains "json".
            if "json" not in user.lower():
                user = f"{user}\nReply with json."
        # Deterministic decoding: pin seed so batching/composition is the ONLY remaining
        # source of cross-batch variance (env MEMHUB_LLM_SEED, empty = off).
        import os as _os

        _seed = _os.environ.get("MEMHUB_LLM_SEED", "").strip()
        if _seed:
            try:
                kwargs["seed"] = int(_seed)
            except ValueError:
                pass
        start = time.perf_counter()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            **self._completion_kwargs(**kwargs),
        )
        self._after_call("chat_json", resp, start)
        choice = resp.choices[0]
        content = choice.message.content or ""
        truncated = choice.finish_reason == "length"
        parsed = _parse_json(content)
        if json_array:
            return (parsed if isinstance(parsed, (dict, list)) else {}), truncated
        return (parsed if isinstance(parsed, dict) else {}), truncated

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        json_object: bool | None = None,
        json_array: bool = False,
        max_tokens: int = DEFAULT_CHAT_JSON_MAX_TOKENS,
        retries: int = 1,
    ) -> dict | list:
        """Run a chat completion and parse the reply as JSON (single-shot, no chunking).

        Raises ``LLMOutputTruncatedError`` when the reply was cut at ``max_tokens``
        and did not parse: the same request would be cut again.
        """
        parsed, truncated = await self._chat_json_once(
            system=system,
            user=user,
            temperature=temperature,
            json_object=json_object,
            json_array=json_array,
            max_tokens=max_tokens,
        )
        # 解析失败重试：空结果（{}/[]）时，模型输出可能是 markdown 包裹/前言导致
        # _parse_json 退化（实测 8027 字符 unparseable -> 0 ops）。追加严格指令重试一次。
        # 注意：模型真吐空（无内容可归纳）也会重试一次，无害（多一次免费调用）。
        for _ in range(max(0, retries)):
            is_empty = parsed == {} or parsed == []
            if not is_empty or truncated:
                break
            parsed, truncated = await self._chat_json_once(
                system=system,
                user=user
                + "\n\nImportant: reply with ONLY a valid JSON "
                + ("array" if json_array else "object")
                + ". No markdown fences, no explanation, no extra text.",
                temperature=temperature,
                json_object=json_object,
                json_array=json_array,
                max_tokens=max_tokens,
            )
        if truncated and (parsed == {} or parsed == []):
            raise LLMOutputTruncatedError(
                f"chat_json reply cut at max_tokens={max_tokens} before the JSON closed"
            )
        return parsed

    async def chat_json_for_content(
        self,
        *,
        content: str,
        build_system: Callable[[str], str],
        merge_results: Callable[[list[dict]], dict],
        user: str | None = None,
        temperature: float = 0.2,
        json_object: bool | None = None,
        max_tokens: int = DEFAULT_CHAT_JSON_MAX_TOKENS,
    ) -> dict:
        """Chunk long *content*, run JSON extract per chunk, merge with *merge_results*."""
        user_text = content if user is None else user
        budget = self._content_char_budget(build_system)
        if (
            self.input_max_chars <= 0
            or len(build_system(user_text)) + len(user_text) <= self.input_max_chars
        ):
            system = build_system(user_text)
            parsed, _ = await self._chat_json_once(
                system=system,
                user=user_text,
                temperature=temperature,
                json_object=json_object,
                max_tokens=max_tokens,
            )
            return parsed

        chunks = chunk_text_for_llm(user_text, budget)
        if len(chunks) == 1:
            system = build_system(chunks[0])
            parsed, _ = await self._chat_json_once(
                system=system,
                user=chunks[0],
                temperature=temperature,
                json_object=json_object,
                max_tokens=max_tokens,
            )
            return parsed

        logger.info(
            "chat_json_for_content chunked %d parts (budget=%d chars)",
            len(chunks),
            budget,
        )
        parts: list[dict] = []
        for chunk in chunks:
            system = build_system(chunk)
            parsed, _ = await self._chat_json_once(
                system=system,
                user=chunk,
                temperature=temperature,
                json_object=json_object,
                max_tokens=max_tokens,
            )
            parts.append(parsed)
        return merge_results(parts)

    async def _chat_text_once(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
    ) -> str:
        start = time.perf_counter()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            **self._completion_kwargs(),
        )
        self._after_call("chat_text", resp, start)
        return resp.choices[0].message.content or ""

    async def chat_text(
        self, *, system: str, user: str, temperature: float = 0.2
    ) -> str:
        """Run a chat completion and return the raw text reply (single-shot)."""
        return await self._chat_text_once(
            system=system, user=user, temperature=temperature
        )

    async def chat_text_for_content(
        self,
        *,
        content: str,
        build_system: Callable[[str], str],
        merge_text: Callable[[list[str]], str] = merge_text_chunks,
        temperature: float = 0.2,
    ) -> str:
        """Chunk long *content*, summarize each chunk, merge text (map-reduce)."""
        budget = self._content_char_budget(build_system)
        if (
            self.input_max_chars <= 0
            or len(build_system(content)) + len(content) <= self.input_max_chars
        ):
            return await self._chat_text_once(
                system=build_system(content),
                user=content,
                temperature=temperature,
            )

        chunks = chunk_text_for_llm(content, budget)
        if len(chunks) == 1:
            return await self._chat_text_once(
                system=build_system(chunks[0]),
                user=chunks[0],
                temperature=temperature,
            )

        logger.info(
            "chat_text_for_content chunked %d parts (budget=%d chars)",
            len(chunks),
            budget,
        )
        parts: list[str] = []
        for chunk in chunks:
            parts.append(
                await self._chat_text_once(
                    system=build_system(chunk),
                    user=chunk,
                    temperature=temperature,
                )
            )
        return merge_text(parts)

    async def chat_with_tools(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> dict:
        """Run a tool-calling chat completion; return ``{content, tool_calls}`` from one turn."""
        start = time.perf_counter()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice=tool_choice,  # type: ignore[arg-type]
            temperature=temperature,
            **self._completion_kwargs(),
        )
        self._after_call("chat_tools", resp, start)
        msg = resp.choices[0].message
        tool_calls: list[dict] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.function
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": fn.name,
                            "arguments": fn.arguments or "",
                        },
                    }
                )
        return {"content": msg.content or "", "tool_calls": tool_calls}

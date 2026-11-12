import httpx
import pytest
import respx

from memhub.providers.llm import (
    LLMClient,
    TRUNC_MARKER,
    chunk_text_for_llm,
    fit_chat_prompt,
    is_chinese,
    merge_extract_results,
    truncate_middle,
)
from memhub.providers.usage import UsageEvent


def _make_client():
    return LLMClient(base_url="https://api.test/v1", api_key="sk-x", model="gpt-test")


def _completion(message: dict) -> dict:
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


@respx.mock
async def test_chat_json_parses_fenced_content():
    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_completion(
                {"role": "assistant", "content": '```json\n{"facts": ["a"]}\n```'}
            ),
        )
    )
    result = await _make_client().chat_json(system="s", user="u")
    assert result == {"facts": ["a"]}
    assert route.called


@respx.mock
async def test_chat_json_regex_fallback():
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_completion(
                {"role": "assistant", "content": 'noise before {"k": 1} trailing'}
            ),
        )
    )
    result = await _make_client().chat_json(system="s", user="u")
    assert result == {"k": 1}


@respx.mock
async def test_chat_json_sends_json_mode_by_default():
    import json as _json

    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_completion({"role": "assistant", "content": "{}"})
        )
    )
    await _make_client().chat_json(system="s", user="u")
    body = _json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert "json" in body["messages"][1]["content"].lower()


@respx.mock
async def test_chat_json_passes_extra_body():
    from unittest.mock import AsyncMock, MagicMock

    client = LLMClient(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="gpt-test",
        extra_body={"thinking": {"type": "disabled"}},
    )
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="{}"))]
    create = AsyncMock(return_value=mock_resp)
    client.client.chat.completions.create = create  # type: ignore[method-assign]
    await client.chat_json(system="s", user="u")
    assert create.call_args.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@respx.mock
async def test_chat_json_usage_callback():
    """The usage callback must report the parsed call's tokens exactly once.

    The reply must be non-empty: `chat_json` retries once on an empty result (`{}` / `[]`),
    which would legitimately produce two usage events and makes the assertion below
    measure the retry policy instead of the callback.
    """
    events: list[UsageEvent] = []

    def _cb(event: UsageEvent) -> None:
        events.append(event)

    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                **_completion(
                    {
                        "role": "assistant",
                        "content": '{"facts": []}',
                        "reasoning_content": "",
                    }
                ),
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
    )
    client = LLMClient(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="gpt-test",
        usage_callback=_cb,
    )
    await client.chat_json(system="s", user="u")
    assert route.called
    assert route.call_count == 1
    assert len(events) == 1
    assert events[0].kind == "chat_json"
    assert events[0].prompt_tokens == 11
    assert events[0].completion_tokens == 7
    assert events[0].latency_ms >= 0
    assert events[0].reasoning_tokens == 0
    assert events[0].reasoning_chars == 0


@respx.mock
async def test_chat_json_disabled_json_mode_omits_response_format():
    import json as _json

    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_completion({"role": "assistant", "content": "[]"})
        )
    )
    # client-level json_mode off
    await LLMClient(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="gpt-test",
        json_mode=False,
    ).chat_json(system="s", user="u")
    body = _json.loads(route.calls.last.request.content)
    assert "response_format" not in body

    # per-call override off
    await _make_client().chat_json(system="s", user="u", json_object=False)
    body2 = _json.loads(route.calls.last.request.content)
    assert "response_format" not in body2


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你好，世界，这是一段中文文本", True),
        ("This is purely English text only", False),
        ("", False),
        ("hello 你好 world test more english here", False),
    ],
)
def test_is_chinese(text, expected):
    assert is_chinese(text) is expected


def test_fit_chat_prompt_keeps_system_truncates_user_middle():
    system = "s" * 3000
    user = "u" * 5000
    out_system, out_user = fit_chat_prompt(system, user, max_chars=6000)
    assert out_system == system
    assert len(out_system) + len(out_user) <= 6000
    assert out_user.startswith("u")
    assert out_user.endswith("u")
    assert TRUNC_MARKER in out_user


def test_truncate_middle_head_tail():
    text = "a" * 100
    out = truncate_middle(text, 40)
    assert len(out) == 40
    assert out.startswith("a")
    assert out.endswith("a")
    assert TRUNC_MARKER in out


@respx.mock
async def test_chat_json_chunks_oversized_content():
    import json as _json

    calls: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        calls.append(body)
        idx = len(calls)
        letter = chr(ord("a") + idx - 1)
        content = f'{{"facts": [{{"content": "fact-{letter}", "tags": []}}]}}'
        return httpx.Response(
            200, json=_completion({"role": "assistant", "content": content})
        )

    respx.post("https://api.test/v1/chat/completions").mock(side_effect=_record)
    client = LLMClient(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="gpt-test",
        input_max_chars=80,
    )
    tmpl = "SYS:{content}"
    result = await client.chat_json_for_content(
        content="y" * 120,
        build_system=lambda c: tmpl.format(content=c),
        merge_results=merge_extract_results,
    )
    assert len(calls) >= 2
    assert len(result["facts"]) >= 2
    assert TRUNC_MARKER not in calls[0]["messages"][1]["content"]


def test_chunk_text_for_llm_prefers_newlines():
    text = ("line\n" * 30).strip()
    chunks = chunk_text_for_llm(text, 40)
    assert len(chunks) >= 2
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


@respx.mock
async def test_chat_json_raises_when_reply_is_cut_at_max_tokens():
    """A reply stopped by ``max_tokens`` mid-JSON is not an empty answer: retrying the
    same request is cut again, so it raises instead of returning ``{}``."""
    from memhub.providers.llm import LLMOutputTruncatedError

    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                **_completion({"role": "assistant", "content": '{"candidates": [{"task": "a'}),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"candidates": [{"task": "a'},
                        "finish_reason": "length",
                    }
                ],
            },
        )
    )
    with pytest.raises(LLMOutputTruncatedError):
        await _make_client().chat_json(system="s", user="u", max_tokens=16)
    assert route.call_count == 1

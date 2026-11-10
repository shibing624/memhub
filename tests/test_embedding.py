import json

import httpx
import respx
import pytest
from openai import APIStatusError

from memhub.config import EMBED_CHARS_PER_TOKEN, EMBED_MAX_TOKENS
from memhub.providers.embedding import EmbedService
from memhub.providers.usage import UsageEvent


def _make_service(dim: int = 4, model: str = "embed-test") -> EmbedService:
    return EmbedService(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model=model,
        dim=dim,
    )


def _embed_response(vectors: list[list[float]]) -> dict:
    return {
        "object": "list",
        "model": "embed-test",
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@respx.mock
async def test_embed_single():
    respx.post("https://api.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_embed_response([[0.1, 0.2, 0.3, 0.4]]))
    )
    vec = await _make_service().embed("hello")
    assert vec == [0.1, 0.2, 0.3, 0.4]


@respx.mock
async def test_embed_batch():
    respx.post("https://api.test/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json=_embed_response([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])
        )
    )
    vecs = await _make_service().embed_batch(["a", "b"])
    assert vecs == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]


async def test_embed_batch_empty_returns_empty():
    assert await _make_service().embed_batch([]) == []


@respx.mock
async def test_embed_batch_never_sends_dimensions_param():
    captured: dict = {}

    def _record_request(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json=_embed_response([[0.1, 0.2, 0.3, 0.4]])
        )

    respx.post("https://api.test/v1/embeddings").mock(side_effect=_record_request)
    await _make_service(dim=4096, model="qwen3-embedding-8b").embed("hello")
    assert "dimensions" not in captured["json"]
    assert captured["json"]["model"] == "qwen3-embedding-8b"


@respx.mock
async def test_embed_batch_bge_m3_omits_dimensions():
    captured: dict = {}

    def _record_request(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=_embed_response([[0.1] * 4]))

    respx.post("https://api.test/v1/embeddings").mock(side_effect=_record_request)
    await _make_service(dim=1024, model="embed").embed("hello")
    assert "dimensions" not in captured["json"]


@respx.mock
async def test_embed_batch_usage_callback():
    events: list[UsageEvent] = []

    def _cb(event: UsageEvent) -> None:
        events.append(event)

    respx.post("https://api.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_embed_response([[0.1, 0.2, 0.3, 0.4]]))
    )
    svc = _make_service()
    svc.usage_callback = _cb
    await svc.embed_batch(["hello"])
    assert len(events) == 1
    assert events[0].kind == "embed_batch"
    assert events[0].text_chars == 5
    assert events[0].batch_size == 1


@respx.mock
async def test_embed_batch_long_text_head_truncated():
    long_text = "徐" * (EMBED_MAX_TOKENS + 100)
    captured: dict = {}

    def _record_request(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        n = len(captured["json"]["input"])
        return httpx.Response(
            200,
            json=_embed_response([[float(i + 1)] * 4 for i in range(n)]),
        )

    respx.post("https://api.test/v1/embeddings").mock(side_effect=_record_request)
    svc = EmbedService(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="embed-test",
        dim=4,
        input_max_tokens=EMBED_MAX_TOKENS,
        chars_per_token=EMBED_CHARS_PER_TOKEN,
    )
    max_bytes = svc.input_max_bytes
    vecs = await svc.embed_batch([long_text, "hi"])
    sent = captured["json"]["input"]
    assert len(sent) == 2
    assert len(sent[0].encode("utf-8")) <= max_bytes
    assert len(sent[0].encode("utf-8")) < len(long_text.encode("utf-8"))
    assert sent[0] == long_text[: len(sent[0])]
    assert sent[1] == "hi"
    assert len(vecs) == 2
    assert len(vecs[0]) == 4


def test_embed_max_tokens_derived_char_budget():
    svc = EmbedService(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="embed-test",
        input_max_tokens=8192,
        chars_per_token=2.5,
    )
    assert svc.input_max_chars == 20480
    assert svc.input_max_bytes == 8192
    assert svc.input_max_bytes <= svc.input_max_tokens
    assert svc.input_max_tokens == 8192


def test_default_embed_budget_fits_dense_text_under_the_token_limit():
    """The byte cap really keeps dense Unicode within cl100k_base's token limit.

    Character-only truncation used to send more tokens than the configured limit.
    """
    tiktoken = pytest.importorskip("tiktoken", reason="requires tiktoken tokenizer")
    encoding = tiktoken.get_encoding("cl100k_base")
    svc = EmbedService(
        base_url="https://api.test/v1",
        api_key="sk-x",
        model="embed-test",
        input_max_tokens=8192,
        chars_per_token=2.5,
    )
    for dense_text in ("徐" * 10_000, "\U00010000" * 10_000, "a!?~" * 10_000):
        chopped = svc._truncate_for_embedding(
            dense_text, svc.input_max_chars, svc.input_max_bytes
        )
        assert len(chopped.encode("utf-8")) <= svc.input_max_bytes
        assert len(encoding.encode(chopped)) <= svc.input_max_tokens


@respx.mock
async def test_embedding_transport_failure_is_not_retried():
    route = respx.post("https://api.test/v1/embeddings").mock(return_value=httpx.Response(503))
    svc = _make_service()
    with pytest.raises(APIStatusError):
        await svc.embed("hello")
    assert route.call_count == 1 and svc.client.max_retries == 0
    await svc.aclose()


@respx.mock
async def test_generation_transport_failure_is_not_retried():
    from memhub.providers.llm import LLMClient
    route = respx.post("https://api.test/v1/chat/completions").mock(return_value=httpx.Response(503))
    llm = LLMClient(base_url="https://api.test/v1", api_key="sk-x", model="test")
    with pytest.raises(APIStatusError):
        await llm.chat_json(system="test", user="test")
    assert route.call_count == 1 and llm.client.max_retries == 0
    await llm.aclose()


@respx.mock
async def test_embed_cache_keeps_the_most_recently_used_texts():
    """A full cache evicts the least recently used text, not new ones."""
    route = respx.post("https://api.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_embed_response([[0.1, 0.2, 0.3, 0.4]]))
    )
    svc = EmbedService(
        base_url="https://api.test/v1", api_key="sk-x", model="embed-test", dim=4,
        cache_size=2,
    )
    await svc.embed("a")
    await svc.embed("b")
    await svc.embed("a")  # hit: "a" is now the most recent
    await svc.embed("c")  # evicts "b"
    assert route.call_count == 3
    await svc.embed("a")
    await svc.embed("c")
    assert route.call_count == 3
    await svc.embed("b")
    assert route.call_count == 4


@respx.mock
async def test_embed_cache_size_zero_caches_nothing():
    route = respx.post("https://api.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_embed_response([[0.1, 0.2, 0.3, 0.4]]))
    )
    svc = EmbedService(
        base_url="https://api.test/v1", api_key="sk-x", model="embed-test", dim=4,
        cache_size=0,
    )
    await svc.embed("a")
    await svc.embed("a")
    assert route.call_count == 2

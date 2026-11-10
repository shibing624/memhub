# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Async OpenAI-compatible embedding service with both a direct embed/embed_batch
path (search-side, low-latency) and a write-side embed_queued path that coalesces concurrent
single requests into one batch within a 200ms window or 32-item threshold (whichever first).
"""
import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

from openai import AsyncOpenAI
import httpx

from memhub.config import (
    EMBED_CHARS_PER_TOKEN,
    EMBED_MAX_TOKENS,
)
from memhub.providers.usage import UsageCallback, UsageEvent

logger = logging.getLogger("memhub.embed")


def embedding_api_dimensions(model: str, dim: int) -> int | None:
    """Return ``dimensions`` for OpenAI embeddings API, or None to omit the parameter.

    qwen3-embedding / bge-m3 (vLLM served as ``embed``) reject matryoshka ``dimensions``.
    """
    name = (model or "").lower()
    if "qwen3-embedding" in name or "-for-online" in name:
        return None
    if name in ("embed",) or "bge-m3" in name or "bge_m3" in name:
        return None
    return dim if dim > 0 else None


class EmbedService:
    """Async embedding client with optional write-path batching queue."""

    DEFAULT_BATCH_SIZE = 32
    DEFAULT_BATCH_WINDOW_MS = 200.0
    DEFAULT_CACHE_SIZE = 10000

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int = 1536,
        timeout: int = 30,
        queue_batch_size: int = DEFAULT_BATCH_SIZE,
        queue_batch_window_ms: float = DEFAULT_BATCH_WINDOW_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
        input_max_tokens: int = EMBED_MAX_TOKENS,
        chars_per_token: float = EMBED_CHARS_PER_TOKEN,
        usage_callback: UsageCallback | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        self.input_max_tokens = input_max_tokens
        self.chars_per_token = chars_per_token
        self.input_max_chars = int(input_max_tokens * chars_per_token)
        # cl100k_base can spend four tokens on one Unicode codepoint and each
        # UTF-8 byte can contribute a token. Byte truncation therefore has a
        # tokenizer-independent upper bound on the number of tokens sent.
        self.input_max_bytes = input_max_tokens
        self.usage_callback = usage_callback
        self._api_dimensions = embedding_api_dimensions(model, dim)
        if self._api_dimensions is None:
            logger.info(
                "EmbedService model=%r: omitting API dimensions param (native model dims); "
                "set embed_dim to the model's output size for vector store alignment.",
                model,
            )
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            http_client=httpx.AsyncClient(timeout=timeout),
        )

        # LRU: a long-running process keeps embedding new texts, and the recent
        # ones (the query a recall embeds for each of its searches) must stay.
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size

        self._queue_batch_size = queue_batch_size
        self._queue_batch_window_s = queue_batch_window_ms / 1000.0
        self._queue_lock: asyncio.Lock | None = None
        self._queue_pending: list[tuple[str, asyncio.Future]] = []
        self._queue_flush_task: asyncio.Task | None = None

    @staticmethod
    def _cache_key(text: str) -> str:
        """Stable cache key for a text (md5 over utf-8 bytes)."""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _cached(self, text: str) -> list[float] | None:
        """The cached vector of ``text``, marked as most recently used."""
        key = self._cache_key(text)
        vector = self._cache.get(key)
        if vector is not None:
            self._cache.move_to_end(key)
        return vector

    def _maybe_cache(self, text: str, vector: list[float]) -> None:
        """Insert (text, vector), evicting the least recently used entry when full."""
        if self._cache_size <= 0:
            return
        key = self._cache_key(text)
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def aclose(self) -> None:
        await self.client.close()

    async def _embed_prepared_batch(self, prepared: list[str]) -> list[list[float]]:
        """Call the embeddings API for *prepared* strings (already chunked)."""
        kwargs: dict = {"model": self.model, "input": prepared}
        if self._api_dimensions is not None:
            kwargs["dimensions"] = self._api_dimensions

        start = time.perf_counter()
        resp = await self.client.embeddings.create(**kwargs)
        vectors = [item.embedding for item in resp.data]
        if self.usage_callback is not None:
            self.usage_callback(UsageEvent(
                kind="embed_batch", model=self.model,
                latency_ms=(time.perf_counter() - start) * 1000,
                text_chars=sum(len(p) for p in prepared), batch_size=len(prepared),
            ))
        return vectors

    @staticmethod
    def _truncate_for_embedding(text: str, max_chars: int, max_bytes: int) -> str:
        head = text[:max_chars] if max_chars > 0 else text
        raw = head.encode("utf-8")
        if max_bytes > 0 and len(raw) > max_bytes:
            return raw[:max_bytes].decode("utf-8", errors="ignore")
        return head

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts and return one vector per input; empty list short-circuits.

        Long texts are head-truncated to both a character and UTF-8 byte budget before
        the API call. Full source documents remain stored and are passed to candidate
        generation; raw anchors are excluded from normal recall.
        """
        if not texts:
            return []
        max_chars = self.input_max_chars
        max_bytes = self.input_max_bytes
        prepared: list[str] = []
        n_truncated = 0
        for t in texts:
            if (max_chars > 0 and len(t) > max_chars) or (
                max_bytes > 0 and len(t.encode("utf-8")) > max_bytes
            ):
                prepared.append(self._truncate_for_embedding(t, max_chars, max_bytes))
                n_truncated += 1
            else:
                prepared.append(t)
        if n_truncated:
            logger.info(
                "embed_batch: %d of %d inputs over limit, head-truncated to ~%d chars "
                "and %d UTF-8 bytes (≈%d tokens); raw L1 anchors only, full text "
                "kept for extraction",
                n_truncated,
                len(texts),
                max_chars,
                max_bytes,
                self.input_max_tokens,
            )
        return await self._embed_prepared_batch(prepared)

    async def embed(self, text: str) -> list[float]:
        """Embed a single text directly (no queue), using the cache when possible."""
        cached = self._cached(text)
        if cached is not None:
            return cached
        vectors = await self.embed_batch([text])
        vector = vectors[0]
        self._maybe_cache(text, vector)
        return vector

    async def embed_queued(self, text: str) -> list[float]:
        """Embed a single text via the write-side queue: batches concurrent calls."""
        cached = self._cached(text)
        if cached is not None:
            return cached

        if self._queue_lock is None:
            self._queue_lock = asyncio.Lock()

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async with self._queue_lock:
            self._queue_pending.append((text, future))
            pending_count = len(self._queue_pending)
            if pending_count >= self._queue_batch_size:
                await self._flush_locked()
            elif self._queue_flush_task is None or self._queue_flush_task.done():
                self._queue_flush_task = asyncio.ensure_future(self._delayed_flush())

        vector = await future
        self._maybe_cache(text, vector)
        return vector

    async def _delayed_flush(self) -> None:
        """Wait the configured window then flush whatever is pending in the queue."""
        await asyncio.sleep(self._queue_batch_window_s)
        if self._queue_lock is not None:
            async with self._queue_lock:
                if self._queue_pending:
                    await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Issue one batch embeddings call for the pending queue (caller holds the lock)."""
        if not self._queue_pending:
            return
        batch = self._queue_pending[:]
        self._queue_pending.clear()

        texts = [item[0] for item in batch]
        futures = [item[1] for item in batch]

        try:
            vectors = await self.embed_batch(texts)
        except Exception as exc:
            for fut in futures:
                if not fut.done():
                    fut.set_exception(exc)
            return

        for fut, vec in zip(futures, vectors):
            if not fut.done():
                fut.set_result(vec)

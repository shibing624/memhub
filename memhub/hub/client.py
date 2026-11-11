# -*- coding: utf-8 -*-
"""Async HTTP client for the single-writer MemHub Core."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from memhub.hub.config import resolve_hub_config


class HubClient:
    """Thin bearer-authenticated client; it never opens the memory store."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Hub API key missing: start local `memhub hub serve`"
            )
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_config(
        cls,
        hub_dir: str | Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> HubClient:
        cfg = resolve_hub_config(hub_dir)
        return cls(base_url=cfg.base_url, api_key=cfg.api_key, transport=transport)

    async def __aenter__(self) -> HubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def identity(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/hub/identity")

    async def write(
        self,
        *,
        content: str = "",
        messages: list[dict] | None = None,
        session_id: str = "",
        memory_at: int | None = None,
        request_id: str,
        raw_only: bool = False,
        wait: bool = False,
        workspace_id: str | None = None,
        branch: str | None = None,
        tool_signals: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Submit evidence. Returns once it is durable in the Core (a receipt with
        status ``pending``); ``wait=True`` instead blocks until it is processed."""
        body: dict[str, Any] = {
            "content": content,
            "messages": messages,
            "session_id": session_id,
            "memory_at": memory_at,
            "request_id": request_id,
            "raw_only": raw_only,
            "wait": wait,
            "workspace_id": workspace_id,
            "branch": branch,
        }
        if tool_signals is not None:
            body["tool_signals"] = tool_signals
        return await self._request("POST", "/v1/hub/writes", json=body)

    async def write_status(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/hub/writes/{request_id}")

    async def extract_raw(self) -> dict[str, Any]:
        """Queue extraction for every write that was stored raw-only."""
        return await self._request("POST", "/v1/hub/extract")

    async def retry_failed(self) -> dict[str, Any]:
        return await self._request("POST", "/v1/hub/retry")

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        """Fetch one Hub memory by full ID or unique >=8-char prefix (for expanding index entries)."""
        path = f"/v1/hub/memories/{quote(memory_id.strip(), safe='')}"
        return await self._request("GET", path)

    async def recall(
        self,
        *,
        query: str,
        limit: int = 10,
        raw_limit: int = 5,
        procedure_limit: int = 3,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/hub/recall",
            json={
                "query": query,
                "limit": limit,
                "raw_limit": raw_limit,
                "procedure_limit": procedure_limit,
                "workspace": workspace,
            },
        )

    async def get_conversation(
        self, source_id: str, *, context_turns: int = 1
    ) -> dict[str, Any]:
        """Fetch an L0_RAW dialogue by full ID or 8-char prefix, plus neighboring turns."""
        path = f"/v1/hub/conversations/{quote(source_id.strip(), safe='')}"
        return await self._request(
            "GET", path, params={"context_turns": context_turns}
        )

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/hub/status")

    async def list_memories(self) -> list[dict[str, Any]]:
        """All versions of the personal scope, newest first (browse rows)."""
        return (await self._request("GET", "/v1/hub/memories"))["memories"]

    async def update_memory(self, memory_id: str, content: str) -> dict[str, Any]:
        """Replace one current memory with a corrected version; history is kept."""
        path = f"/v1/hub/memories/{quote(memory_id, safe='')}"
        return await self._request("PUT", path, json={"content": content})

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        """Retire one current memory; the version stays in the history."""
        path = f"/v1/hub/memories/{quote(memory_id, safe='')}"
        return await self._request("DELETE", path)

    async def sync(self, *, compact: bool = False) -> dict[str, Any]:
        """Replicate with the sync target now and return the cycle's counts.

        ``compact`` also folds the target's log into a snapshot (digester only).
        """
        return await self._request(
            "POST", "/v1/hub/sync", params={"compact": "true"} if compact else None
        )

    async def relay(self, method: str, path: str, body: Any = None) -> Any:
        """Forward one Core API call (``hub relay``: sync over ssh)."""
        return await self._request(method, path, json=body)

    async def aclose(self) -> None:
        await self._http.aclose()


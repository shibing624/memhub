"""Dual adds explicit cross-evidence cognition to the shared System1 writer."""

from __future__ import annotations

import asyncio
import time

from memhub.sdk_models import ChatMessage
from memhub.registry import ComponentFactory
from memhub.system2.system2_agent import System2Agent
from memhub.writer.memory_writer import MemoryWriter, WriterOutcome


class System2Writer:
    def __init__(self, *, factory: ComponentFactory) -> None:
        self.factory = factory
        self.inner = MemoryWriter(factory=factory)
        self.last_digest_stats: dict[str, float] = {}
        self._digest_lock = asyncio.Lock()

    async def write(
        self,
        *,
        content: str,
        app_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        request_id: str,
        memory_at: int | None = None,
        messages: list[ChatMessage] | None = None,
        workspace_id: str | None = None,
        workspace_remote: str | None = None,
        tool_signals: list[dict] | None = None,
    ) -> WriterOutcome:
        result = await self.inner.write(
            content=content,
            app_id=app_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            memory_at=memory_at,
            messages=messages,
            workspace_id=workspace_id,
            workspace_remote=workspace_remote,
            tool_signals=tool_signals,
        )
        if result.commit_passed:
            self.factory.cache.enqueue_s2_task(
                user_id=user_id, app_id=app_id, agent_id=agent_id
            )
        return result

    async def write_raw(
        self,
        *,
        content: str,
        app_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        request_id: str,
        memory_at: int | None = None,
        workspace_id: str | None = None,
        workspace_remote: str | None = None,
        tool_signals: list[dict] | None = None,
    ) -> WriterOutcome:
        return await self.inner.write_raw(
            content=content,
            app_id=app_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            memory_at=memory_at,
            workspace_id=workspace_id,
            workspace_remote=workspace_remote,
            tool_signals=tool_signals,
        )

    async def distill(
        self,
        *,
        content: str,
        app_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        request_id: str,
        source_node_ids: list[str],
        memory_at: int | None = None,
        messages: list[ChatMessage] | None = None,
    ) -> WriterOutcome:
        result = await self.inner.distill(
            content=content,
            app_id=app_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            source_node_ids=source_node_ids,
            memory_at=memory_at,
            messages=messages,
        )
        if result.commit_passed:
            self.factory.cache.enqueue_s2_task(
                user_id=user_id, app_id=app_id, agent_id=agent_id
            )
        return result

    async def digest_pending(self) -> int:
        """Run System2 once per pending scope; returns the number of acknowledged scopes."""
        async with self._digest_lock:
            tasks = self.factory.cache.list_pending_s2_scopes()
            scopes: dict[tuple[str, str, str], list[int]] = {}
            for task in tasks:
                key = (task["app_id"], task["user_id"], task["agent_id"])
                scopes.setdefault(key, []).append(task["id"])
            started = time.perf_counter()
            sem = asyncio.Semaphore(self.factory.settings.reconcile_concurrency)
            totals: dict[str, float] = {}
            acknowledged = 0

            async def process(scope: tuple[str, str, str], ids: list[int]) -> None:
                nonlocal acknowledged
                async with sem:
                    agent = System2Agent(factory=self.factory)
                    stats = await agent.run(
                        app_id=scope[0], user_id=scope[1], agent_id=scope[2]
                    )
                    for key, value in stats.items():
                        totals[key] = totals.get(key, 0.0) + float(value)
                    self.factory.cache.mark_s2_tasks_done(ids)
                    acknowledged += 1

            # Preserve successful acknowledgements when a different scope fails.
            results = await asyncio.gather(
                *(process(scope, ids) for scope, ids in scopes.items()),
                return_exceptions=True,
            )
            self.last_digest_stats = {
                "s2_agent_sec": time.perf_counter() - started,
                "s2_agent_runs": float(len(scopes)),
                **{f"s2_{key}": value for key, value in totals.items()},
            }
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if self.factory.settings.purge_done_queues:
                self.factory.cache.purge_done_queues()
            return acknowledged

__all__ = ["System2Writer"]

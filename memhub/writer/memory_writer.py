"""Uniform writer: retain raw evidence; all semantic decisions use Jev."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field, replace

from memhub.agent.mem_agent import MemAgent
from memhub.errors import IdempotencyConflictError
from memhub.registry import ComponentFactory
from memhub.sdk_models import ChatMessage, CommitResult, MemoryDecision, write_status
from memhub.types import Layer, MemoryNode


@dataclass
class WriterOutcome:
    memory_id: str
    extra_node_ids: list[str] = field(default_factory=list)
    commit_passed: bool = True
    is_ephemeral: bool = False
    deduplicated: bool = False
    decisions: list[MemoryDecision] = field(default_factory=list)
    status: str = ""

    def __post_init__(self) -> None:
        self.decisions = [
            MemoryDecision(**item) if isinstance(item, dict) else item
            for item in self.decisions
        ]
        if not self.status:
            self.status = write_status(self.decisions)


def _outcome(raw_id: str, commit: CommitResult, ephemeral: bool, ids: list[str]) -> WriterOutcome:
    return WriterOutcome(
        raw_id,
        ids,
        commit.passed,
        ephemeral,
        decisions=commit.decisions,
    )


class MemoryWriter:
    def __init__(self, *, factory: ComponentFactory) -> None:
        self.factory = factory
        self.agent = MemAgent(factory=factory)
        self._requests = factory.commit_locks

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
    ) -> WriterOutcome:
        scope = (app_id, user_id, agent_id)
        key = "request:" + json.dumps([scope, request_id])
        fingerprint = hashlib.sha256(
            json.dumps([content, session_id, memory_at], ensure_ascii=False).encode()
        ).hexdigest()
        async with self._requests.get(key):
            saved = self.factory.cache.get_request_outcome(key)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise IdempotencyConflictError("request_id reused with different input")
                return WriterOutcome(**saved["outcome"])
            raw_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":raw"))
            raw = self.factory.vector.get(raw_id)
            if raw and (
                raw.content != content
                or (raw.app_id, raw.user_id, raw.agent_id) != scope
                or raw.session_id != session_id
                or raw.memory_at != memory_at
            ):
                raise IdempotencyConflictError("request_id reused with different input")
            if raw is None:
                raw = await self._raw(
                    content=content,
                    app_id=app_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    memory_at=memory_at,
                    node_id=raw_id,
                    workspace_id=workspace_id,
                    workspace_remote=workspace_remote,
                )
            # Failed model work leaves original evidence available for retry; a
            # committed plan is recovered from the plan journal by run_system1.
            ids, commit, ephemeral = await self.agent.run_system1(
                content=content,
                raw_node=raw,
                messages=messages,
                request_id=request_id,
            )
            result = _outcome(raw.node_id, commit, ephemeral, ids)
            self.factory.cache.set_request_outcome(
                key, {"fingerprint": fingerprint, "outcome": asdict(result)}
            )
            return result

    async def _raw(
        self,
        *,
        content: str,
        app_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        memory_at: int | None,
        node_id: str | None = None,
        workspace_id: str | None = None,
        workspace_remote: str | None = None,
    ) -> MemoryNode:
        raw = MemoryNode(
            content=content,
            layer=Layer.L0_RAW,
            kind="raw",
            app_id=app_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            memory_at=memory_at,
            # The project the dialogue happened in travels with the evidence, so
            # every device that receives it can scope recall to that project.
            # ``workspace_remote`` (the origin ``org/repo``) tells two checkouts
            # apart on a device where this path does not exist.
            custom={
                k: v
                for k, v in (
                    ("workspace_id", workspace_id),
                    ("workspace_remote", workspace_remote),
                )
                if v
            }
            or None,
        )
        if node_id:
            raw.node_id = node_id
        raw.embedding = await self.factory.embed.embed(content)
        await asyncio.to_thread(self.factory.vector.upsert, [raw])
        self.factory.history.append(
            event="ADD",
            node_id=raw.node_id,
            user_id=user_id,
            old=None,
            new=raw.to_metadata(),
        )
        return raw

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
    ) -> WriterOutcome:
        """Store L0 evidence under ``request_id`` without extraction.

        The receipt is kept apart from ``write``'s, while the L0 node id is the same:
        a later ``write`` with this ``request_id`` reuses the evidence and extracts
        from it (raw-first cold start, extraction later).
        """
        scope = (app_id, user_id, agent_id)
        key = "request:" + json.dumps([scope, request_id])
        receipt = "raw-" + key
        fingerprint = hashlib.sha256(
            json.dumps([content, session_id, memory_at], ensure_ascii=False).encode()
        ).hexdigest()
        async with self._requests.get(key):
            saved = self.factory.cache.get_request_outcome(receipt)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise IdempotencyConflictError(
                        "request_id reused with different input"
                    )
                return WriterOutcome(**saved["outcome"])
            raw_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":raw"))
            raw = self.factory.vector.get(raw_id)
            if raw is None:
                raw = await self._raw(
                    content=content,
                    app_id=app_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    memory_at=memory_at,
                    node_id=raw_id,
                    workspace_id=workspace_id,
                    workspace_remote=workspace_remote,
                )
            elif (
                raw.content != content
                or (raw.app_id, raw.user_id, raw.agent_id) != scope
                or raw.session_id != session_id
                or raw.memory_at != memory_at
            ):
                raise IdempotencyConflictError(
                    "request_id reused with different input"
                )
            result = WriterOutcome(
                raw.node_id,
                commit_passed=False,
                is_ephemeral=True,
                status="raw_only",
            )
            self.factory.cache.set_request_outcome(
                receipt,
                {"fingerprint": fingerprint, "outcome": asdict(result)},
            )
            return result

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
        if not source_node_ids:
            raise ValueError("distill requires at least one raw source")
        sources = self.factory.vector.get_by_ids(source_node_ids)
        if set(sources) != set(source_node_ids):
            raise ValueError("distill requires existing raw sources")
        for node in sources.values():
            if (node.app_id, node.user_id, node.agent_id) != (
                app_id,
                user_id,
                agent_id,
            ):
                raise ValueError("distill sources must belong to the requested scope")
            if node.layer != Layer.L0_RAW:
                raise ValueError("distill sources must be raw evidence")
        raw = next(iter(sources.values()))
        if memory_at is not None:
            # Only the in-memory context gets the override, not immutable stored evidence.
            raw = replace(raw, memory_at=memory_at)
        ids, commit, ephemeral = await self.agent.run_system1(
            content=content,
            raw_node=raw,
            messages=messages,
            request_id=request_id,
            source_node_ids=source_node_ids,
        )
        return _outcome(raw.node_id, commit, ephemeral, ids)

# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Async reader backed by the single hybrid retrieval pipeline.
"""
import asyncio
import time

from memhub.coding.types import procedure_detail
from memhub.isolation import build_filter
from memhub.registry import ComponentFactory
from memhub.retrieval.hybrid_engine import search_hybrid
from memhub.sdk_models import MemoryItem, ReadResult, SearchMemories
from memhub.types import Layer, MemoryNode, MemoryStatus


class Reader:
    """Embed a query, run hybrid recall, and return grouped memory items."""

    def __init__(self, *, factory: ComponentFactory):
        self.factory = factory

    async def search(
        self,
        *,
        query: str,
        app_ids: list[str],
        user_id: str,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.4,
        profile_limit: int = -1,
        profile_min_score: float = 0.3,
        intention_limit: int = 0,
        include_derived: bool = True,
        include_derived_fusion: bool = True,
        created_after: int | None = None,
        request_id: str | None = None,
        collect_trace: bool = False,
        as_of: int | None = None,
        raw_limit: int = 0,
        include_relations: bool = True,
        procedure_limit: int = 0,
        procedure_min_score: float = 0.3,
    ) -> tuple[SearchMemories, ReadResult | None]:
        """Recall memories using explicit filters; no query-intent guessing is performed."""
        start = time.perf_counter()
        rid = request_id or "search"
        # Search has one query vector, so the write-side batching window only
        # adds latency here.
        query_embedding = await self.factory.embed.embed(query)
        memories = await search_hybrid(
            factory=self.factory,
            query=query,
            query_embedding=query_embedding,
            app_ids=app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            session_ids=session_ids,
            limit=limit,
            min_score=min_score,
            profile_limit=profile_limit,
            profile_min_score=profile_min_score,
            intention_limit=intention_limit,
            include_derived=include_derived,
            include_derived_fusion=include_derived_fusion,
            created_after=created_after,
            as_of=as_of,
            raw_limit=raw_limit,
            include_relations=include_relations,
            procedure_limit=procedure_limit,
            procedure_min_score=procedure_min_score,
        )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        trace = None
        if collect_trace:
            trace = ReadResult(
                memories=memories,
                final_count=(
                    len(memories.profile)
                    + len(memories.proactive)
                    + len(memories.normal)
                    + len(memories.raw)
                    + len(memories.schema)
                    + len(memories.catalog)
                    + len(memories.procedures)
                ),
                elapsed_ms=elapsed_ms,
            )
        self.factory.cache.log_pipeline(
            request_id=rid,
            stage="READ_HYBRID",
            payload={
                "profile": len(memories.profile),
                "normal": len(memories.normal),
                "proactive": len(memories.proactive),
                "raw": len(memories.raw),
                "schema": len(memories.schema),
                "catalog": len(memories.catalog),
                "procedures": len(memories.procedures),
                "include_derived": include_derived,
            },
        )
        return memories, trace

    async def search_conversation(
        self,
        *,
        query: str,
        app_ids: list[str],
        user_id: str,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        created_after: int | None = None,
    ) -> SearchMemories:
        """Recall L0_RAW (retained source history) as evidence. Never mixed into default search."""
        query_embedding = await self.factory.embed.embed(query)
        when = build_filter(
            app_ids=app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            session_ids=session_ids,
            layers=[Layer.L0_RAW],
            statuses=[MemoryStatus.ACTIVE],
            created_after=created_after,
            latest_only=True,
        )
        top_k = max(limit, 10)
        nodes = await asyncio.to_thread(
            self.factory.vector.query,
            embedding=query_embedding,
            where=when,
            top_k=top_k,
        )
        items: list[MemoryItem] = []
        for node in nodes:
            if min_score > 0 and node.score < min_score:
                continue
            items.append(self.memory_node_to_item(node))
            if len(items) >= limit:
                break
        return SearchMemories(normal=items)

    @staticmethod
    def memory_node_to_item(node: MemoryNode) -> MemoryItem:
        """Convert a stored node used by get/list into the public memory model."""
        raw_ts = (node.custom or {}).get("merged_timestamps")
        merged_timestamps = (
            [int(x) for x in raw_ts if isinstance(x, (int, float))]
            if isinstance(raw_ts, list) and raw_ts
            else None
        )
        update_type = (node.custom or {}).get("update_type") or None
        if update_type is not None:
            update_type = str(update_type)
        source_node_id = (node.custom or {}).get("source_node_id") or None
        if source_node_id is not None:
            source_node_id = str(source_node_id)
        return MemoryItem(
            memory_id=node.node_id,
            layer=node.layer.value,
            kind=node.kind or node.category.value,
            owner=node.owner,
            is_latest=node.is_latest,
            revision=node.revision,
            supersedes=list(node.supersedes),
            source_ids=list((node.custom or {}).get("source_ids", [])),
            conflicts_with=list((node.custom or {}).get("conflicts_with", [])),
            valid_from=node.valid_from,
            valid_until=node.valid_until,
            content=node.content,
            category=node.category.value,
            score=node.score,
            tags=list(node.tags),
            session_id=node.session_id,
            memory_at=node.memory_at,
            gmt_created=node.gmt_created,
            gmt_modified=node.gmt_modified,
            merged_timestamps=merged_timestamps,
            update_type=update_type,
            source_node_id=source_node_id,
            procedure=procedure_detail(node),
        )

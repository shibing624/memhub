# -*- coding: utf-8 -*-
"""
Hybrid read engine (Hy-Memory parity, latency-optimized).

Semantic and lexical recall + calibrated BM25 rerank + graph evidence fusion.
Lexical recall admits exact-term matches outside the semantic candidate pool;
both routes use the same semantic/BM25 scoring scale. profile = L2 (VDB) + L3
(graph forward/reverse RRF); L2 identity
routes to normal; intentions to proactive when intention_limit > 0.
"""
import asyncio
import logging
import math
import re
from dataclasses import replace

from memhub.coding.types import PROCEDURE_KIND, procedure_detail
from memhub.isolation import build_filter
from memhub.registry import ComponentFactory
from memhub.retrieval.bm25 import (
    compute_bm25_confidences,
    compute_bm25_scores,
    tokenize,
)
from memhub.retrieval.evolution import expand_evolution_chains
from memhub.retrieval.hybrid_scoring import score_vdb_node
from memhub.retrieval.intention_recall import recall_intentions
from memhub.retrieval.schema_selection import select_schemas
from memhub.retrieval import rrf
from memhub.sdk_models import EvolutionItem, MemoryItem, SearchMemories
from memhub.types import Layer, MemoryNode, MemoryStatus

logger = logging.getLogger("memhub.retrieval.hybrid")

# Hybrid layer routing
_PROFILE_LAYERS = [Layer.L2_PROFILE]
_VDB_PROFILE_LAYERS = [Layer.L2_PROFILE]
_VDB_RECALL_LAYERS = [Layer.L1_FACT, Layer.L2_PROFILE]
# Note: L0_RAW excluded from VDB semantic recall (align hy_memory: raw fragments
#   are noisy and dilute top-k ranking; only extracted/curated layers are recalled).
_PROFILE_RECALL_LIMIT = 10
_PROFILE_LAYER_VALS = {layer.value for layer in _PROFILE_LAYERS}
_NORMAL_SOURCES = {"vdb"}
# RRF over the raw route only ranks raw hits among themselves; this scale keeps
# raw scores below curated memories of equal rank when routes are flattened.
_RAW_SCORE_SCALE = 0.5


def _layer_of(item: dict) -> str:
    node = item.get("node")
    if node is not None and node.layer is not None:
        return node.layer.value
    return item.get("layer") or ""


def _derived_to_item(graph_hit: dict) -> dict:
    """Wrap a derived graph hit as an item dict with a synthetic MemoryNode."""
    node = MemoryNode(
        content=graph_hit.get("content", ""),
        layer=Layer.L3_DERIVED,
        app_id="",
        user_id="",
        node_id=graph_hit["node_id"],
    )
    node.score = graph_hit.get("score", 0.0)
    return {**graph_hit, "node": node, "score": node.score}


def _valid_at(node: MemoryNode | None, as_of: int | None) -> bool:
    """Whether an explicit validity window contains ``as_of`` (no window = valid)."""
    if node is None or as_of is None:
        return True
    if node.valid_until is not None and node.valid_until < as_of:
        return False
    if node.valid_from is not None and node.valid_from > as_of:
        return False
    return True


def _brief(node: MemoryNode) -> EvolutionItem:
    return EvolutionItem(
        node_id=node.node_id,
        content=node.content,
        layer=node.layer.value,
        memory_at=node.memory_at,
        gmt_created=node.gmt_created,
    )


def attach_relations(vector, items: list[MemoryItem]) -> None:
    """Fill one-hop ``previous`` (direct supersedes) and current ``conflicts``."""
    wanted = {
        nid for item in items for nid in [*item.supersedes, *item.conflicts_with]
    }
    if not wanted:
        return
    found = vector.get_by_ids(list(wanted))
    for item in items:
        previous = [found[n] for n in item.supersedes if n in found]
        if previous:
            item.previous = [_brief(n) for n in previous]
        conflicts = [
            found[n] for n in item.conflicts_with if n in found and found[n].is_latest
        ]
        if conflicts:
            item.conflicts = [_brief(n) for n in conflicts]


def _lexical_candidates(
    *,
    vector,
    where: dict,
    terms: list[str],
    excluded: set[str],
    limit: int,
    as_of: int | None,
) -> list[MemoryNode]:
    """Best BM25 matches over every current memory of the scope not already recalled.

    Memory scopes are per user (a few hundred to a few thousand nodes), so scoring them
    in one pass is cheap and needs no index. Only nodes sharing a query term are
    returned, best first.
    """
    nodes = [n for n in vector.get_many(where, limit=20_000) if _valid_at(n, as_of)]
    if not nodes:
        return []
    scores = compute_bm25_scores(
        terms, [f"{n.content} {' '.join(n.tags)}".strip() for n in nodes]
    )
    ranked = sorted(zip(nodes, scores), key=lambda pair: pair[1], reverse=True)
    return [
        node for node, score in ranked
        if score > 0 and node.node_id not in excluded
    ][:limit]


def _cosine_score(query: list[float], embedding: list[float] | None) -> float:
    if not embedding or len(query) != len(embedding):
        return 0.0
    norm = math.sqrt(sum(x * x for x in query) * sum(x * x for x in embedding))
    return sum(a * b for a, b in zip(query, embedding)) / norm if norm else 0.0


def _current_versions(
    *,
    vector,
    hits: list[dict],
    where: dict,
    as_of: int | None,
    limit: int,
) -> list[dict]:
    """For superseded memories found in recall, add the versions that replaced them.

    Memory replacement is recorded structurally (``superseded_by`` / ``is_latest``),
    so a query that describes a topic very differently from the user's current
    wording (asking about "literary discussions" when the stored change reads
    "stopped listening to book podcasts") can still reach the *current* statement:
    recall the superseded version on its own wording, then follow the recorded
    replacement edge to the node that is current now.  No lexical rule is involved:
    the edge is what the write path decided, in any language.
    """
    # Relevance a replacement inherits: the best score among the versions it replaced.
    inherited: dict[str, float] = {}
    for hit in hits:
        node = hit.get("node")
        if node is None or node.is_latest or not node.superseded_by:
            continue
        for nid in node.superseded_by:
            inherited[nid] = max(inherited.get(nid, 0.0), float(hit["score"]))
    if not inherited:
        return []
    nodes = vector.get_by_ids(sorted(inherited))
    out: list[dict] = []
    for nid in sorted(inherited, key=lambda n: -inherited[n]):
        node = nodes.get(nid)
        if (
            node is None
            or not node.is_latest
            or node.app_id not in where.get("app_id", {}).get("$in", [])
            or node.user_id != where.get("user_id")
            or not _valid_at(node, as_of)
        ):
            continue
        out.append({"node_id": node.node_id, "node": node, "inherited": inherited[nid]})
        if len(out) >= limit:
            break
    return out


def source_excerpt(content: str, query: str, anchors: list[str], max_chars: int = 3200) -> str:
    """Select verbatim, role-labelled passages instead of head-truncating a source."""
    if len(content) <= max_chars:
        return content
    turns = re.split(r"(?m)(?=^\[(?:user|assistant|tool|system)\]:)", content)
    passages: list[str] = []
    roles: list[str] = []
    for turn in turns:
        if not turn.strip():
            continue
        label = re.match(r"\[(\w+)\]:", turn)
        role = label.group(1) if label else ""
        start = 0
        while start < len(turn):
            end = min(start + 900, len(turn))
            if end < len(turn):
                boundary = turn.rfind(" ", start + 450, end)
                if boundary > start:
                    end = boundary
            passage = turn[start:end].strip()
            if start and role:
                passage = f"[{role}]: […] {passage}"
            passages.append(passage)
            roles.append(role)
            start = end
    if not passages:
        return content[:max_chars]
    query_scores = compute_bm25_scores(tokenize(query), passages)
    anchor_scores = compute_bm25_scores(tokenize(" ".join(anchors[:4])), passages)
    query_max = max(query_scores, default=0) or 1
    anchor_max = max(anchor_scores, default=0) or 1
    ranked = sorted(range(len(passages)), key=lambda i: -(
        query_scores[i] / query_max + anchor_scores[i] / anchor_max
    ))
    selected: set[int] = set()
    size = 0
    for index in ranked:
        # A quoted answer needs the preceding user turn to resolve references.
        neighbours = [index]
        if roles[index] == "assistant" and index > 0 and roles[index - 1] == "user":
            neighbours.append(index - 1)
        for i in neighbours:
            if i not in selected and size + len(passages[i]) + 8 <= max_chars:
                selected.add(i)
                size += len(passages[i]) + 8
        if len(selected) >= 3:
            break
    return "\n[…]\n".join(passages[i] for i in sorted(selected))


def _compact_span(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _cut_phrase(span: str, phrase: str) -> str:
    """Delete one whitespace-insensitive occurrence of ``phrase`` from ``span``."""
    chars = [ch for ch in phrase if not ch.isspace()]
    if not chars:
        return span
    pattern = r"\s*".join(re.escape(ch) for ch in chars)
    return re.sub(pattern, "", span, count=1, flags=re.IGNORECASE)


def _strip_retired_clauses(content: str, retired_texts: list[str]) -> tuple[str, bool]:
    """Drop clauses that are a retired claim, and report whether every claim was found.

    A shared token is not evidence: "生产数据库每晚备份" is a different fact from
    the retracted "测试使用生产数据库". A clause is removed only when it contains
    the retired wording, or it is itself a substantial fragment of that wording.
    Claims that cannot be located stay in the text; the caller marks the version
    relation instead of deleting the surrounding fact.
    """
    pairs = [
        (text, _compact_span(text))
        for text in retired_texts
        if text and len(_compact_span(text)) >= 4
    ]
    if not pairs:
        return content, True
    found = [False] * len(pairs)
    kept_turns: list[str] = []
    for turn in re.split(r"(?m)(?=^\[(?:user|assistant|tool|system)\]:)", content):
        if not turn.strip():
            continue
        m = re.match(r"^(\[\w+\]:\s*)([\s\S]*)$", turn.strip())
        prefix, body = (m.group(1), m.group(2)) if m else ("", turn.strip())
        kept_sentences: list[str] = []
        for sent in re.split(r"(?<=[。！？.!?\n；;])\s*", body):
            if not sent.strip():
                continue
            kept_clauses: list[str] = []
            for clause in re.split(r"[，,]", sent):
                piece = clause.strip()
                if not piece:
                    continue
                norm = _compact_span(piece)
                hit = False
                for i, (raw, retired) in enumerate(pairs):
                    if len(norm) >= 4 and retired in norm:
                        found[i] = True
                        hit = True
                        piece = _cut_phrase(piece, raw).strip(" ，,；;。")
                        norm = _compact_span(piece)
                    elif len(norm) >= 4 and norm in retired and len(norm) / len(retired) >= 0.6:
                        found[i] = True
                        hit = True
                        piece = ""
                        break
                cleaned = piece.strip(" ，,；;。")
                if cleaned and (not hit or len(_compact_span(cleaned)) >= 4):
                    kept_clauses.append(cleaned)
            if kept_clauses:
                kept_sentences.append("，".join(kept_clauses))
        if kept_sentences:
            kept_turns.append(f"{prefix}{' '.join(kept_sentences)}".strip())
    return "\n".join(kept_turns), all(found)


_RETIRED_NOTE = "（此段有已废止说法，以当前记忆为准）"


def _with_retired_note(text: str, located: bool, max_chars: int) -> str:
    if not text or located:
        return text
    if len(text) + 1 + len(_RETIRED_NOTE) > max_chars:
        return text
    return f"{text} {_RETIRED_NOTE}"


def novel_source_excerpt(
    content: str,
    query: str,
    covered_texts: list[str],
    max_chars: int = 480,
    *,
    retired_texts: list[str] | None = None,
) -> str:
    """Compress a same-source L0 dialogue to passages NOT already covered by recalled items.

    A single L0 batch can hold several turns, dates, conditions or failure steps while
    structured recall matched only one fact or procedure from it. Passages whose terms
    are already covered verbatim are skipped. A clause is removed only when its wording
    is the retired claim; an unrelated sentence that merely shares a subject is kept,
    with a version note when the retired wording cannot be located.
    """
    if max_chars <= 0 or not content.strip():
        return ""
    retired = [t for t in (retired_texts or []) if t and t.strip()]
    located = True
    if retired:
        content, located = _strip_retired_clauses(content, retired)
        if not content.strip():
            return ""
    all_covered = [*covered_texts, *retired]
    if not all_covered:
        return _with_retired_note(source_excerpt(content, query, [], max_chars), located, max_chars)

    covered_terms = set(tokenize(" ".join(all_covered)))
    if not covered_terms:
        return _with_retired_note(source_excerpt(content, query, [], max_chars), located, max_chars)

    turns = re.split(r"(?m)(?=^\[(?:user|assistant|tool|system)\]:)", content)
    passages: list[str] = []
    roles: list[str] = []
    for turn in turns:
        if not turn.strip():
            continue
        label = re.match(r"\[(\w+)\]:", turn)
        role = label.group(1) if label else ""
        start = 0
        while start < len(turn):
            end = min(start + 420, len(turn))
            if end < len(turn):
                boundary = turn.rfind(" ", start + 200, end)
                if boundary > start:
                    end = boundary
            chunk = turn[start:end].strip()
            if start and role:
                chunk = f"[{role}]: […] {chunk}"
            if chunk:
                passages.append(chunk)
                roles.append(role)
            start = end
    if not passages:
        return ""

    novelties: list[float] = []
    for p in passages:
        body = re.sub(r"^\[(?:\w+)\]:\s*(?:\[…\]\s*)?", "", p)
        ptoks = tokenize(body)
        if not ptoks:
            novelties.append(0.0)
            continue
        overlap = sum(1 for t in ptoks if t in covered_terms) / len(ptoks)
        novelties.append(max(0.0, 1.0 - overlap))

    # Every passage is already stated almost word-for-word in the recalled memories.
    if max(novelties, default=0.0) < 0.20:
        return ""

    q_terms = tokenize(query)
    q_scores = compute_bm25_scores(q_terms, passages) if q_terms else [0.0] * len(passages)
    q_max = max(q_scores, default=0.0) or 1.0

    candidates = [i for i, nov in enumerate(novelties) if nov >= 0.20]
    ranked = sorted(
        candidates,
        key=lambda i: -((q_scores[i] / q_max) * (0.4 + 0.6 * novelties[i]) + 0.25 * novelties[i]),
    )
    selected: set[int] = set()
    size = 0
    for index in ranked:
        neighbours = [index]
        if (
            roles[index] == "assistant"
            and index > 0
            and roles[index - 1] == "user"
            and novelties[index - 1] >= 0.15
        ):
            neighbours.append(index - 1)
        for i in neighbours:
            if i in selected:
                continue
            piece = passages[i]
            if not selected and len(piece) > max_chars:
                piece = piece[: max_chars - 1] + "…"
                passages[i] = piece
            if size + len(piece) + (8 if selected else 0) <= max_chars:
                selected.add(i)
                size += len(piece) + (8 if len(selected) > 1 else 0)
        if len(selected) >= 2:
            break
    if not selected:
        return ""
    return _with_retired_note(
        "\n[…]\n".join(passages[i] for i in sorted(selected)), located, max_chars
    )


def _item_to_memory(item: dict) -> MemoryItem:
    if item.get("node") is None:
        item = _derived_to_item(item)
    node = item["node"]
    chain = item.get("evolution_chain")
    evolution_chain = (
        [
            EvolutionItem(
                node_id=entry["node_id"],
                content=entry["content"],
                layer=entry["layer"],
                memory_at=entry.get("memory_at"),
                gmt_created=entry.get("gmt_created"),
                speculate=entry.get("speculate"),
            )
            for entry in chain
        ]
        if chain
        else None
    )
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
        supporting_ids=list((node.custom or {}).get("supporting_result_ids", [])),
        conflicts_with=list((node.custom or {}).get("conflicts_with", [])),
        valid_from=node.valid_from,
        valid_until=node.valid_until,
        content=node.content,
        category=node.category.value,
        score=item["score"],
        procedure=procedure_detail(node),
        tags=list(node.tags),
        session_id=node.session_id,
        memory_at=node.memory_at,
        gmt_created=node.gmt_created,
        gmt_modified=node.gmt_modified,
        evolution_chain=evolution_chain,
        merged_timestamps=merged_timestamps,
        update_type=update_type,
        source_node_id=source_node_id,
    )


def _persona_catalog(vector, *, app_ids, user_id, agent_ids, as_of) -> list[dict]:
    """The current question-blind persona catalog of the scope (at most one)."""
    where = build_filter(
        app_ids=app_ids, user_id=user_id, agent_ids=agent_ids or None,
        layers=[Layer.L3_DERIVED], statuses=[MemoryStatus.ACTIVE], latest_only=True,
    )
    nodes = [n for n in vector.get_many(where, limit=50) if n.kind == "persona_catalog" and _valid_at(n, as_of)]
    nodes.sort(key=lambda n: n.gmt_created, reverse=True)
    return [{"node_id": n.node_id, "node": n, "score": 1.0, "source": "catalog"} for n in nodes[:1]]


async def _procedure_route(
    *,
    vector,
    query: str,
    query_embedding: list[float],
    where: dict,
    limit: int,
    min_score: float,
    weights: tuple[float, float],
    version_bridge_limit: int,
    as_of: int | None,
) -> list[dict]:
    """Current procedures near the query, scored like normal recall within the group.

    A procedure is embedded from its task and alternative phrasings, so semantic
    recall matches the question side; BM25 over the readable text and file anchors
    catches exact error strings, commands and paths. A superseded procedure that
    matches bridges to its current replacement.
    """
    if limit <= 0:
        return []
    current_where = where | {"is_latest": True}
    pool_size = max(limit * 4, 20)
    nodes = await asyncio.to_thread(
        vector.query, embedding=query_embedding, where=current_where, top_k=pool_size
    )
    pool = {n.node_id: n for n in nodes if _valid_at(n, as_of)}
    if version_bridge_limit > 0:
        old = await asyncio.to_thread(
            vector.query,
            embedding=query_embedding,
            where=where | {"is_latest": False},
            top_k=version_bridge_limit,
        )
        bridged = await asyncio.to_thread(
            _current_versions,
            vector=vector,
            hits=[{"node_id": n.node_id, "score": n.score, "node": n} for n in old],
            where=current_where,
            as_of=as_of,
            limit=version_bridge_limit,
        )
        heads = await asyncio.to_thread(
            vector.get_by_ids, [b["node_id"] for b in bridged if b["node_id"] not in pool]
        )
        for hit in bridged:
            node = pool.get(hit["node_id"]) or heads.get(hit["node_id"], hit["node"])
            node.score = max(
                pool[node.node_id].score if node.node_id in pool
                else _cosine_score(query_embedding, node.embedding),
                hit["inherited"],
            )
            pool[node.node_id] = node
    terms = tokenize(query)
    w_sem, w_bm25 = weights
    bm25: dict[str, float] = {}
    if terms and w_bm25 > 0:
        lexical = await asyncio.to_thread(
            _lexical_candidates,
            vector=vector,
            where=current_where,
            terms=terms,
            excluded=set(pool),
            limit=limit,
            as_of=as_of,
        )
        embedded = await asyncio.to_thread(vector.get_by_ids, [n.node_id for n in lexical])
        for candidate in lexical:
            node = embedded.get(candidate.node_id, candidate)
            node.score = _cosine_score(query_embedding, node.embedding)
            pool[node.node_id] = node
        rows = list(pool.values())
        contents = [f"{n.content} {' '.join(n.tags)}".strip() for n in rows]
        calibrated = compute_bm25_confidences(
            terms, contents, compute_bm25_scores(terms, contents)
        )
        bm25 = {n.node_id: s for n, s in zip(rows, calibrated, strict=True) if s > 0}
    scored = [
        {
            "node_id": n.node_id,
            "node": n,
            "score": (
                score_vdb_node(n.score, bm25.get(n.node_id, 0.0), w_sem, w_bm25)
                if bm25
                else n.score
            ),
            "source": "procedure",
        }
        for n in pool.values()
    ]
    scored = [row for row in scored if row["score"] >= min_score]
    scored.sort(key=lambda row: row["score"], reverse=True)
    return scored[:limit]


async def search_hybrid(
    *,
    factory: ComponentFactory,
    query: str,
    query_embedding: list[float],
    app_ids: list[str],
    user_id: str,
    agent_ids: list[str] | None = None,
    session_ids: list[str] | None = None,
    limit: int = 10,
    min_score: float = 0.4,
    profile_limit: int = -1,
    profile_min_score: float = 0.3,
    intention_limit: int = 0,
    created_after: int | None = None,
    include_derived: bool = True,
    include_derived_fusion: bool = True,
    as_of: int | None = None,
    raw_limit: int = 0,
    include_relations: bool = True,
    procedure_limit: int = 0,
    procedure_min_score: float = 0.3,
) -> SearchMemories:
    """Run hybrid recall + fusion; return profile/proactive/normal/raw/procedure groups.

    ``as_of`` drops memories whose explicit ``valid_from``/``valid_until`` window
    excludes that time (a past TEMPORAL exception). ``raw_limit`` adds an L0_RAW
    route as fallback evidence for details extraction did not keep.
    """
    vector = factory.vector
    graph = factory.graph
    settings = factory.settings

    w_sem = settings.hybrid_w_sem
    w_bm25 = settings.hybrid_w_bm25
    # min_score=0 means no gate (benchmark default); only enforce a floor when > 0.
    min_score_gate = min_score if min_score > 0 else 0.0

    final_limit = limit if limit > 0 else 10
    candidate_multiplier = max(1.0, float(settings.normal_candidate_multiplier))
    # v10: enlarge semantic recall to ~hy's combined sem+kw pool (45+60=105),
    # compensating for ChromaDB's lack of native keyword index. L0_RAW excluded.
    vdb_sem_limit = max(int(math.ceil(final_limit * candidate_multiplier * 2.5)), 60)
    # Extra candidates admitted from a lexical pass over the scope (pure keyword recall).
    lexical_limit = max(final_limit // 2, 10)
    graph_limit = max(final_limit * 2, 20)
    # How many superseded versions are examined as bridges to their current replacement.
    version_bridge_limit = settings.version_bridge_limit

    use_derived = include_derived
    # Current facts/profiles serve normal recall; derived evidence uses profile fusion.
    recall_statuses = [MemoryStatus.ACTIVE]

    normal_where = build_filter(
        app_ids=app_ids,
        user_id=user_id,
        agent_ids=agent_ids,
        session_ids=session_ids,
        layers=_VDB_RECALL_LAYERS,
        statuses=recall_statuses,
        latest_only=True,
        created_after=created_after,
        exclude_kinds=[PROCEDURE_KIND],
    )
    profile_where = build_filter(
        app_ids=app_ids,
        user_id=user_id,
        agent_ids=agent_ids,
        session_ids=session_ids,
        layers=_VDB_PROFILE_LAYERS,
        statuses=[MemoryStatus.ACTIVE],
        latest_only=True,
        created_after=created_after,
    )
    # Superseded memories are recall bridges: matching one reveals what replaced it.
    superseded_where = build_filter(
        app_ids=app_ids,
        user_id=user_id,
        agent_ids=agent_ids,
        session_ids=session_ids,
        layers=_VDB_RECALL_LAYERS,
        statuses=recall_statuses,
        created_after=created_after,
        exclude_kinds=[PROCEDURE_KIND],
    ) | {"is_latest": False}

    async def _vdb_semantic() -> list[dict]:
        nodes = await asyncio.to_thread(
            vector.query,
            embedding=query_embedding,
            where=normal_where,
            top_k=vdb_sem_limit,
        )
        return [
            {"node_id": n.node_id, "score": n.score, "node": n}
            for n in nodes
            if _valid_at(n, as_of)
        ]

    async def _superseded_semantic() -> list[dict]:
        """Superseded versions near the query: bridges to their current replacements."""
        if version_bridge_limit <= 0:
            return []
        nodes = await asyncio.to_thread(
            vector.query,
            embedding=query_embedding,
            where=superseded_where,
            top_k=version_bridge_limit,
        )
        return [{"node_id": n.node_id, "score": n.score, "node": n} for n in nodes]

    async def _profile_vdb() -> list[dict]:
        if profile_limit == 0:
            return []
        nodes = await asyncio.to_thread(
            vector.query,
            embedding=query_embedding,
            where=profile_where,
            top_k=_PROFILE_RECALL_LIMIT,
        )
        return [
            {"node_id": n.node_id, "score": n.score, "node": n}
            for n in nodes
            if n.score >= profile_min_score and _valid_at(n, as_of)
        ]

    async def _raw_route() -> list[dict]:
        if raw_limit <= 0:
            return []
        where = build_filter(
            app_ids=app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            session_ids=session_ids,
            layers=[Layer.L0_RAW],
            statuses=[MemoryStatus.ACTIVE],
            created_after=created_after,
        )
        nodes = await asyncio.to_thread(
            vector.query, embedding=query_embedding, where=where, top_k=raw_limit * 3
        )
        lexical_nodes = await asyncio.to_thread(
            _lexical_candidates, vector=vector, where=where, terms=tokenize(query),
            excluded={n.node_id for n in nodes}, limit=raw_limit * 2, as_of=as_of,
        )
        if lexical_nodes:
            embedded = await asyncio.to_thread(vector.get_by_ids, [n.node_id for n in lexical_nodes])
            for candidate in lexical_nodes:
                node = embedded.get(candidate.node_id, candidate)
                node.score = _cosine_score(query_embedding, node.embedding)
                nodes.append(node)
        if not nodes:
            return []
        semantic = sorted(nodes, key=lambda n: n.score, reverse=True)
        lexical = sorted(
            zip(nodes, compute_bm25_scores(tokenize(query), [n.content for n in nodes])),
            key=lambda pair: pair[1],
            reverse=True,
        )
        fused = rrf.rrf_fuse(
            {
                "semantic": [{"node_id": n.node_id} for n in semantic],
                "bm25": [{"node_id": n.node_id} for n, s in lexical if s > 0],
            }
        )
        by_id = {n.node_id: n for n in nodes}
        top = max((f["rrf_score"] for f in fused), default=0.0) or 1.0
        return [
            {
                "node_id": f["node_id"],
                "node": by_id[f["node_id"]],
                "score": _RAW_SCORE_SCALE * float(f["rrf_score"]) / top,
                "source": "raw",
            }
            for f in fused[:raw_limit]
        ]

    async def _graph_schema() -> list[dict]:
        if graph is None or not include_derived or not include_derived_fusion or settings.derived_context_limit <= 0:
            return []
        hits = await asyncio.to_thread(
            graph.query_by_embedding,
            layer=Layer.L3_DERIVED.value,
            user_id=user_id,
            app_ids=app_ids,
            embedding=query_embedding,
            top_k=graph_limit,
        )
        return [
            {
                "node_id": g.node_id,
                "score": g.score,
                "content": g.content,
                "layer": g.layer,
                "confidence": g.score,
                "node": head,
            }
            for g in hits
            if g.score >= profile_min_score
            and (head := vector.get(g.node_id)) is not None
            and head.is_latest
            and head.app_id in app_ids
            and head.user_id == user_id
            and head.agent_id in (agent_ids or [head.agent_id])
            and (not session_ids or head.session_id in session_ids)
            and (created_after is None or head.gmt_created >= created_after)
            and _valid_at(head, as_of)
        ]

    results = await asyncio.gather(
        _vdb_semantic(),
        _profile_vdb(),
        _graph_schema(),
        _raw_route(),
        _superseded_semantic(),
        _procedure_route(
            vector=vector,
            query=query,
            query_embedding=query_embedding,
            where=build_filter(
                app_ids=app_ids,
                user_id=user_id,
                agent_ids=agent_ids,
                session_ids=session_ids,
                layers=[Layer.L1_FACT],
                statuses=[MemoryStatus.ACTIVE],
                created_after=created_after,
                kinds=[PROCEDURE_KIND],
            ),
            limit=procedure_limit,
            min_score=procedure_min_score,
            weights=(w_sem, w_bm25),
            version_bridge_limit=version_bridge_limit,
            as_of=as_of,
        ),
        return_exceptions=True,
    )

    vdb_semantic_hits = results[0] if not isinstance(results[0], Exception) else []
    profile_hits = results[1] if not isinstance(results[1], Exception) else []
    graph_schema_hits = results[2] if not isinstance(results[2], Exception) else []
    raw_hits: list[dict] = results[3] if isinstance(results[3], list) else []
    superseded_hits: list[dict] = results[4] if isinstance(results[4], list) else []

    if isinstance(results[0], Exception):
        logger.warning("[hybrid] vdb semantic failed: %s", results[0])
    if isinstance(results[1], Exception):
        logger.warning("[hybrid] profile vdb failed: %s", results[1])
    if isinstance(results[2], Exception):
        logger.warning("[hybrid] graph schema failed: %s", results[2])
    if isinstance(results[3], Exception):
        logger.warning("[hybrid] raw route failed: %s", results[3])
    if isinstance(results[4], Exception):
        logger.warning("[hybrid] superseded route failed: %s", results[4])
    # The procedure group is its own product: a failure must be visible, not empty.
    if isinstance(results[5], Exception):
        raise results[5]
    procedure_hits: list[dict] = results[5]

    intention_hits: list[dict] = []
    if use_derived and intention_limit > 0:
        intention_hits = await recall_intentions(
            vector,
            query_embedding,
            app_ids=app_ids,
            user_id=user_id,
            agent_ids=agent_ids,
            limit=intention_limit,
        )
        if graph is not None:
            graph_intentions = await asyncio.to_thread(
                graph.query_by_embedding,
                layer=Layer.L3_DERIVED.value,
                user_id=user_id,
                app_ids=app_ids,
                embedding=query_embedding,
                top_k=intention_limit,
            )
            seen = {h["node_id"] for h in intention_hits}
            for g in graph_intentions:
                head = vector.get(g.node_id)
                if head is None or not head.is_latest or head.kind != "intention":
                    continue
                if agent_ids and head.agent_id not in agent_ids:
                    continue
                if g.node_id in seen:
                    continue
                seen.add(g.node_id)
                node = MemoryNode(
                    content=g.content,
                    layer=head.layer,
                    kind=head.kind,
                    app_id=g.app_id,
                    user_id=g.user_id,
                    agent_id=g.agent_id,
                    tags=g.tags,
                    node_id=g.node_id,
                    gmt_created=g.gmt_created,
                )
                node.score = g.score
                intention_hits.append(
                    {
                        "node_id": g.node_id,
                        "score": g.score,
                        "node": node,
                        "layer": head.layer.value,
                        "source": "graph_intention",
                    }
                )
            intention_hits.sort(key=lambda x: x["score"], reverse=True)
            intention_hits = intention_hits[:intention_limit]
        intention_hits = [h for h in intention_hits if _valid_at(h["node"], as_of)]

    # Every route uses the same candidate calibration. A second profile lookup must
    # not overwrite a fused fact/profile score with its larger raw cosine score.
    seen_candidates = {h["node_id"] for h in vdb_semantic_hits}
    for hit in profile_hits:
        if hit["node_id"] not in seen_candidates:
            vdb_semantic_hits.append(hit)
            seen_candidates.add(hit["node_id"])

    # A replaced memory matched the query: its current version joins the candidate pool.
    # It is as relevant as the topic it continues, so it inherits the best semantic
    # score of the versions it replaced when that exceeds its own.
    bridged = await asyncio.to_thread(
        _current_versions,
        vector=vector,
        hits=superseded_hits,
        where=normal_where,
        as_of=as_of,
        limit=version_bridge_limit,
    )
    if bridged:
        pooled = {hit["node_id"]: hit for hit in vdb_semantic_hits}
        new_ids = [b["node_id"] for b in bridged if b["node_id"] not in pooled]
        embedded = await asyncio.to_thread(vector.get_by_ids, new_ids) if new_ids else {}
        for hit in bridged:
            if hit["node_id"] in pooled:
                row = pooled[hit["node_id"]]
                row["score"] = max(row["score"], hit["inherited"])
                continue
            node = embedded.get(hit["node_id"], hit["node"])
            vdb_semantic_hits.append({
                "node_id": node.node_id,
                "score": max(_cosine_score(query_embedding, node.embedding), hit["inherited"]),
                "node": node,
            })
            seen_candidates.add(node.node_id)

    # Admit lexical matches before calibration so both routes have identical BM25
    # statistics. Raw BM25 scores cannot be fused with cosine similarities.
    terms = tokenize(query)
    bm25_by_nid: dict[str, float] = {}
    if terms and w_bm25 > 0:
        lexical = await asyncio.to_thread(
            _lexical_candidates,
            vector=vector,
            where=normal_where,
            terms=terms,
            excluded={h["node_id"] for h in vdb_semantic_hits},
            limit=lexical_limit,
            as_of=as_of,
        )
        lexical_nodes = await asyncio.to_thread(
            vector.get_by_ids, [n.node_id for n in lexical]
        ) if lexical else {}
        for candidate in lexical:
            node = lexical_nodes.get(candidate.node_id, candidate)
            vdb_semantic_hits.append(
                {
                    "node_id": node.node_id,
                    "score": _cosine_score(query_embedding, node.embedding),
                    "node": node,
                }
            )
        contents = [
            f"{h['node'].content} {' '.join(h['node'].tags)}".strip()
            for h in vdb_semantic_hits
        ]
        calibrated = compute_bm25_confidences(
            terms, contents, compute_bm25_scores(terms, contents)
        )
        bm25_by_nid = {
            hit["node_id"]: score
            for hit, score in zip(vdb_semantic_hits, calibrated, strict=True)
            if score > 0
        }
    has_bm25 = bool(bm25_by_nid)

    vdb_scored: list[dict] = []
    for row in vdb_semantic_hits:
        nid = row["node_id"]
        sem_score = row["score"]
        bm25_score = bm25_by_nid.get(nid, 0.0)
        final = (
            score_vdb_node(sem_score, bm25_score, w_sem, w_bm25)
            if has_bm25
            else sem_score
        )
        vdb_scored.append(
            {
                "node_id": nid,
                "node": row["node"],
                "score": final,
                "source": "vdb",
                "_semantic": sem_score,
                "_bm25": bm25_score,
            }
        )
    vdb_scored.sort(key=lambda x: x["score"], reverse=True)
    scored_by_id = {row["node_id"]: row for row in vdb_scored}

    if raw_limit > 0:
        # Extracted memories are also retrieval keys for their source conversations.
        # This recovers surrounding details without a second extraction/model call.
        anchors: dict[str, list[str]] = {}
        source_scores: dict[str, float] = {}
        for row in vdb_scored[:max(final_limit, raw_limit * 4)]:
            node = row["node"]
            source_ids = ((node.custom or {}).get("source_ids") or []) if node.layer == Layer.L3_DERIVED else []
            if not source_ids:
                source_ids = [(node.custom or {}).get("source_node_id")]
            for source in source_ids:
                if not source:
                    continue
                anchors.setdefault(source, []).append(node.content)
                source_scores[source] = max(source_scores.get(source, 0), row["score"])
        sources = await asyncio.to_thread(vector.get_by_ids, list(anchors))
        # L3 sources point to facts; fact sources point to L0. Follow that one hop,
        # while checking scope before reading any linked text into the response.
        for source_id, node in list(sources.items()):
            if (node.app_id not in app_ids or node.user_id != user_id
                    or (agent_ids and node.agent_id not in agent_ids)):
                continue
            raw_id = (node.custom or {}).get("source_node_id") if node.layer != Layer.L0_RAW else None
            if raw_id:
                anchors.setdefault(raw_id, []).extend(anchors[source_id])
                source_scores[raw_id] = max(source_scores.get(raw_id, 0), source_scores[source_id])
        extra_ids = [nid for nid in anchors if nid not in sources]
        if extra_ids:
            sources.update(await asyncio.to_thread(vector.get_by_ids, extra_ids))
        sources = {
            nid: node for nid, node in sources.items()
            if node.layer == Layer.L0_RAW and node.app_id in app_ids and node.user_id == user_id
            and (not agent_ids or node.agent_id in agent_ids)
            and (not session_ids or node.session_id in session_ids)
            and (created_after is None or node.gmt_created >= created_after)
            and _valid_at(node, as_of)
        }
        linked = sorted(sources, key=lambda nid: -source_scores[nid])
        merged = rrf.rrf_fuse({
            "direct": [{"node_id": row["node_id"]} for row in raw_hits],
            "linked": [{"node_id": nid} for nid in linked],
        })
        raw_nodes = {row["node_id"]: row["node"] for row in raw_hits} | sources
        top = max((row["rrf_score"] for row in merged), default=0) or 1
        raw_hits = [
            {"node_id": row["node_id"],
             "node": replace(raw_nodes[row["node_id"]], content=source_excerpt(
                 raw_nodes[row["node_id"]].content, query, anchors.get(row["node_id"], [])
             )),
             "score": _RAW_SCORE_SCALE * row["rrf_score"] / top, "source": "raw"}
            for row in merged[:raw_limit]
        ]

    expandable = vdb_scored
    meta_by_nid: dict[str, dict] = {
        it["node_id"]: it for it in expandable if it.get("node_id")
    }
    # NOTE(dual_vs_hy): evolution-chain 展开只作用于 profile/L3 桶 —— normal 路保持
    # ACTIVE 纯事实（S1 原始记忆保真），S2 深层证据（chain/schema）单独进 profile 桶。
    # 实测 62-74% 检索结果被 chain 挤占导致事实丢失，分段后事实路与 s1 一致。
    if use_derived and expandable:
        for item in expandable:
            if item.get("node") is not None:
                item["node"].score = item.get("score", 0.0)
        expanded_raw = expand_evolution_chains(
            vector=vector,
            hits=[it["node"] for it in expandable if it.get("node")],
        )
        expanded: list[dict] = []
        for exp in expanded_raw:
            nid = exp["node"].node_id
            orig = meta_by_nid.get(nid, {})
            merged = {
                "node": exp["node"],
                "score": max(exp.get("score", 0.0), orig.get("score", 0.0)),
                "source": orig.get("source", "vdb"),
            }
            if exp.get("evolution_chain"):
                merged["evolution_chain"] = exp["evolution_chain"]
                merged["is_evolved"] = True
            else:
                merged["is_evolved"] = False
            expanded.append(merged)
    else:
        expanded = expandable

    normal_results = [
        it
        for it in expanded
        if _layer_of(it) not in _PROFILE_LAYER_VALS
        or (profile_limit == 0 and _layer_of(it) == Layer.L2_PROFILE.value)
    ]
    if min_score_gate > 0:
        normal_results = [
            it
            for it in normal_results
            if it.get("source") not in _NORMAL_SOURCES
            or it.get("score", 0.0) >= min_score_gate
        ]
    normal_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    normal_results = normal_results[:final_limit]

    profile_results = [it for it in expanded if _layer_of(it) in _PROFILE_LAYER_VALS]
    profile_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    if profile_limit == 0:
        profile_results = []
    elif profile_limit > 0:
        profile_results = profile_results[:profile_limit]

    schema_results: list[dict] = []
    if use_derived and include_derived_fusion and settings.derived_context_limit > 0:
        schema_results = await asyncio.to_thread(
            select_schemas,
            vector=vector,
            graph=graph,
            evidence=[it["node"] for it in [*normal_results, *profile_results] if it.get("node")],
            schema_hits=graph_schema_hits,
            scope_filter=lambda n: (
                n.is_latest and n.status == MemoryStatus.ACTIVE
                and n.app_id in app_ids and n.user_id == user_id
                and (not agent_ids or n.agent_id in agent_ids)
                and _valid_at(n, as_of)
            ),
            limit=settings.derived_context_limit,
            forward_min_score=settings.schema_forward_min_score,
            min_support=settings.schema_min_support,
        )

    catalog_results: list[dict] = []
    if use_derived and include_derived_fusion and settings.catalog_context_enabled:
        catalog_results = await asyncio.to_thread(
            _persona_catalog, vector, app_ids=app_ids, user_id=user_id,
            agent_ids=agent_ids, as_of=as_of,
        )

    logger.info(
        "read_hybrid user=%s vdb_sem=%d bm25_hits=%d profile_l0=%d fwd_l6=%d schemas=%d "
        "normal=%d profile=%d intention=%d",
        user_id,
        len(vdb_semantic_hits),
        len(bm25_by_nid),
        len(profile_hits),
        len(graph_schema_hits),
        len(schema_results),
        len(normal_results),
        len(profile_results),
        len(intention_hits),
    )

    memories = SearchMemories(
        profile=[_item_to_memory(it) for it in profile_results],
        proactive=[_item_to_memory(it) for it in intention_hits],
        normal=[_item_to_memory(it) for it in normal_results],
        raw=[_item_to_memory(it) for it in raw_hits],
        schema=[_item_to_memory(it) for it in schema_results],
        catalog=[_item_to_memory(it) for it in catalog_results],
        procedures=[_item_to_memory(it) for it in procedure_hits],
    )
    if include_relations:
        attach_relations(
            vector, [*memories.profile, *memories.proactive, *memories.normal]
        )
    return memories

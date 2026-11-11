# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Formats three-route search results into an LLM context string (profile ->
proactive -> normal). Evolution chains expose only the current (is_latest head) fact by
default; superseded versions are optional. Memory timestamps are labeled as conversation
dates so QA models do not confuse them with "today".
"""
from __future__ import annotations

from datetime import datetime, timezone

_GROUP_TITLES = [
    ("profile", "【画像 Profile】"),
    ("proactive", "【主动 Proactive】"),
    ("normal", "【常规记忆 Normal】"),
    ("raw", "【原文 Raw】"),
    ("schema", "【模式 Schema】"),
    ("catalog", "【画像目录 Catalog】"),
    ("procedures", "【工程经验 Procedures】"),
]

_DATE_PREAMBLE = (
    "Note: dates on memories are when the user said each fact in past conversations, "
    "not today's date.\n\n"
)


def format_memory_timestamp(ts: int | None) -> str:
    """Format a unix timestamp as YYYY-MM-DD (UTC) for prompt context."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _item_source_id(item: dict) -> str:
    direct = item.get("source_node_id")
    if direct:
        return str(direct)
    for sid in item.get("source_ids") or []:
        if sid:
            return str(sid)
    return ""


def _procedure_boundary_tag(item: dict) -> str:
    proc = item.get("procedure") if isinstance(item.get("procedure"), dict) else {}
    parts: list[str] = []
    scope = str(proc.get("boundary_scope") or "").strip()
    if scope:
        parts.append(f"scope={scope}")
    branch = str(proc.get("branch") or "").strip()
    if branch:
        parts.append(f"branch={branch}")
    envs = str(proc.get("boundary_envs") or "").strip()
    if envs:
        parts.append(f"env: {envs[:80]}")
    files = [str(f) for f in (proc.get("files") or item.get("tags") or []) if f]
    if files:
        shown = ", ".join(files[:3])
        if len(files) > 3:
            shown += f" +{len(files) - 3}"
        parts.append(f"files: {shown}")
    return f" [{' | '.join(parts)}]" if parts else ""


def _local_drift_line(item: dict) -> str:
    drift = item.get("_local_drift")
    if not isinstance(drift, dict):
        return ""
    missing = drift.get("missing_files") or []
    if not missing:
        return ""
    branch = drift.get("branch")
    branch_note = f", 当前分支 {branch}" if branch else ""
    shown = ", ".join(str(p) for p in missing[:3])
    if len(missing) > 3:
        shown += f" 等 {len(missing)} 个文件"
    return f"本机核验提示: 关联文件 {shown} 在当前工作区未找到{branch_note}，引用路径或命令前请核实当前代码"


def _format_procedure_index(item: dict) -> str:
    """Compact 1-line directory entry carrying stable memory ID, task, boundary, and L0 ref."""
    proc = item.get("procedure") if isinstance(item.get("procedure"), dict) else {}
    task = str(proc.get("task") or "").strip()
    if not task:
        first_line = str(item.get("content") or "").splitlines()
        task = first_line[0].strip() if first_line else ""
    mid = str(item.get("memory_id") or item.get("id") or "")
    id_part = f" id={mid[:8]}" if mid else ""
    boundary_part = _procedure_boundary_tag(item)
    mem_date = format_memory_timestamp(item.get("memory_at") or item.get("gmt_created"))
    date_part = f" (conversation date: {mem_date})" if mem_date else ""
    source_id = _item_source_id(item)
    source_part = f" (来源: L0 {source_id[:8]})" if source_id else ""
    drift = _local_drift_line(item)
    drift_part = f" ({drift})" if drift else ""
    return f"- [procedure-index{id_part}] {task}{boundary_part}{date_part}{source_part}{drift_part}"


def _format_item(
    item: dict,
    raw_truncate: int,
    *,
    include_superseded: bool = False,
    compact_procedure: bool = False,
) -> str:
    """Format one memory item; evolution chains default to current head only."""
    if item.get("kind") == "procedure" and compact_procedure:
        return _format_procedure_index(item)

    content = item["content"]
    if item["category"] == "raw" and len(content) > raw_truncate:
        content = content[:raw_truncate] + "…"

    mem_date = format_memory_timestamp(item.get("memory_at") or item.get("gmt_created"))
    date_suffix = f" (conversation date: {mem_date})" if mem_date else ""
    label = "procedure" if item.get("kind") == "procedure" else item["category"]
    mid = str(item.get("memory_id") or item.get("id") or "")
    id_tag = f" id={mid[:8]}" if (label == "procedure" and mid and compact_procedure) else ""
    lines = [f"- [{label}{id_tag}] {_source_tag(item)}{content}{date_suffix}"]
    if item.get("valid_from") is not None or item.get("valid_until") is not None:
        start = format_memory_timestamp(item.get("valid_from")) or "?"
        end = format_memory_timestamp(item.get("valid_until")) or "?"
        lines.append(f"  (valid: {start} → {end})")
    lines.extend(relation_lines(item))
    merged_ts = item.get("merged_timestamps") or []
    if isinstance(merged_ts, list) and len(merged_ts) > 1:
        start = format_memory_timestamp(min(int(x) for x in merged_ts))
        end = format_memory_timestamp(max(int(x) for x in merged_ts))
        if start and end:
            lines.append(f"  (持续: {start} → {end})")
    drift = _local_drift_line(item)
    if drift:
        lines.append(f"  ({drift})")
    source_id = _item_source_id(item)
    if source_id:
        lines.append(f"  (来源: L0 {source_id[:8]})")

    chain = item.get("evolution_chain")
    is_merge = item.get("update_type") == "MERGE" or (
        isinstance(merged_ts, list) and len(merged_ts) > 1
    )
    if chain and len(chain) > 1 and include_superseded:
        label = (
            "  (folded, still valid):"
            if is_merge
            else "  (superseded versions — not current):"
        )
        lines.append(label)
        for ver in chain[1:]:
            ver_date = format_memory_timestamp(ver.get("memory_at") or ver.get("gmt_created"))
            date_part = f", {ver_date}" if ver_date else ""
            lines.append(f"    ·{date_part}: {ver['content']}")
    return "\n".join(lines)


def relation_lines(item: dict, *, prefix: str = "  ") -> list[str]:
    """One-hop previous value and live conflicts of a memory item dict."""
    lines: list[str] = []
    label = {"NEGATE": "negated", "MERGE": "merged from"}.get(
        str(item.get("update_type") or ""), "previously"
    )
    for prev in item.get("previous") or []:
        date = format_memory_timestamp(prev.get("memory_at") or prev.get("gmt_created"))
        lines.append(f"{prefix}({label}{', ' + date if date else ''}: {prev.get('content', '')})")
    for other in item.get("conflicts") or []:
        date = format_memory_timestamp(other.get("memory_at") or other.get("gmt_created"))
        lines.append(
            f"{prefix}(conflicts, both unretracted{', ' + date if date else ''}: "
            f"{other.get('content', '')})"
        )
    return lines


def covered_texts_by_source(
    result: dict,
    *,
    procedure_full_score: float = 0.0,
    max_full_procedures: int = -1,
) -> dict[str, list[str]]:
    """Map each L0_RAW node ID to the structured texts already expanded from it.

    A procedure collapsed to a 1-line ``[procedure-index]`` only contributed its
    ``task`` title, not its full ``solution``, so only its ``task`` counts as covered.
    """
    covered: dict[str, list[str]] = {}
    for group in ("profile", "proactive", "normal"):
        for item in result.get(group) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            sids = [item.get("source_node_id"), *(item.get("source_ids") or [])]
            for sid in dict.fromkeys(str(s) for s in sids if s):
                covered.setdefault(sid, []).append(text)
    full_procs = 0
    for item in result.get("procedures") or []:
        if not isinstance(item, dict):
            continue
        score = float(item.get("score") or 0.0)
        compact = False
        if max_full_procedures == 0:
            compact = True
        elif procedure_full_score > 0.0:
            over_cap = max_full_procedures > 0 and full_procs >= max_full_procedures
            if score < procedure_full_score or over_cap:
                compact = True
            else:
                full_procs += 1
        proc = item.get("procedure") if isinstance(item.get("procedure"), dict) else {}
        text = (
            str(proc.get("task") or "").strip()
            if compact
            else str(item.get("content") or "").strip()
        )
        if not text:
            continue
        sids = [item.get("source_node_id"), *(item.get("source_ids") or [])]
        for sid in dict.fromkeys(str(s) for s in sids if s):
            covered.setdefault(sid, []).append(text)
    return covered


def format_project_view(
    project_view: dict | None,
    *,
    exclude_ids: set[str] | None = None,
    max_chars: int = 1200,
) -> tuple[str, set[str]]:
    """Render query-independent project conventions and procedure directory within budget."""
    if not isinstance(project_view, dict) or max_chars <= 0:
        return "", set()
    excluded = set(exclude_ids or ())
    rendered_ids: set[str] = set()
    lines: list[str] = ["【项目约定与目录 Project View】"]
    used = len(lines[0])
    has_proc_index = False

    for item in project_view.get("conventions") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("memory_id") or item.get("id") or "")
        if mid and mid in excluded:
            continue
        text = " ".join(str(item.get("content") or "").split())
        if not text:
            continue
        source_id = _item_source_id(item)
        src_part = f" (来源: L0 {source_id[:8]})" if source_id else ""
        id_part = f" id={mid[:8]}" if mid else ""
        line = f"- [rule{id_part}] {text}{src_part}"
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        if mid:
            rendered_ids.add(mid)
            excluded.add(mid)

    for item in project_view.get("procedures") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("memory_id") or item.get("id") or "")
        if mid and mid in excluded:
            continue
        line = _format_procedure_index(item)
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        has_proc_index = True
        if mid:
            rendered_ids.add(mid)
            excluded.add(mid)

    if len(lines) == 1:
        return "", set()
    if has_proc_index:
        hint = "  (按需展开: 看到相关 [procedure-index id=...] 可调 memory_get(memory_id=id) 读取完整方案)"
        if used + 1 + len(hint) <= max_chars + 120:
            lines.append(hint)
    return "\n".join(lines), rendered_ids


def format_memories(
    result: dict,
    raw_truncate: int = 800,
    *,
    include_superseded: bool = False,
    date_preamble: bool = True,
    procedure_full_score: float = 0.0,
    max_full_procedures: int = -1,
) -> str:
    """Join non-empty profile/proactive/normal groups into a single context string.

    ``max_full_procedures`` < 0 means unlimited; ``0`` renders every procedure as a
    1-line ``[procedure-index id=...]`` entry; ``> 0`` caps full procedure expansions.
    """
    blocks: list[str] = []
    has_dates = False
    seen_ids: set[str] = set()

    for key, title in _GROUP_TITLES:
        items = result.get(key) or []
        if not items:
            continue
        body_lines: list[str] = []
        full_procs = 0
        has_compact_proc = False
        for it in items:
            mid = it.get("memory_id") or it.get("id")
            if mid and str(mid) in seen_ids:
                continue
            if mid:
                seen_ids.add(str(mid))
            if it.get("memory_at") or it.get("gmt_created"):
                has_dates = True
            compact_proc = False
            if key == "procedures" and (procedure_full_score > 0.0 or max_full_procedures >= 0):
                score = float(it.get("score") or 0.0)
                if max_full_procedures == 0:
                    compact_proc = True
                elif max_full_procedures > 0 and full_procs >= max_full_procedures:
                    compact_proc = True
                elif procedure_full_score > 0.0 and score < procedure_full_score:
                    compact_proc = True
                if compact_proc:
                    has_compact_proc = True
                else:
                    full_procs += 1
            body_lines.append(
                _format_item(
                    it,
                    raw_truncate,
                    include_superseded=include_superseded,
                    compact_procedure=compact_proc,
                )
            )
        if not body_lines:
            continue
        if has_compact_proc:
            body_lines.append(
                "  (按需展开: 看到相关 [procedure-index id=...] 可调 "
                "memory_get(memory_id=id) 读取完整方案，或按 L0 短 ID 调 conversation_search 核对原话)"
            )
        blocks.append(f"{title}\n" + "\n".join(body_lines))

    if not blocks:
        return ""

    text = "\n\n".join(blocks)
    if date_preamble and has_dates:
        return _DATE_PREAMBLE + text
    return text


def _hit_text(hit: dict) -> str:
    return str(hit.get("content") or hit.get("memory") or "")


def _source_tag(hit: dict) -> str:
    if hit.get("layer") == "L3_DERIVED":
        return "[derived] "
    return {"user": "[user] ", "agent": "[assistant] ", "assistant": "[assistant] "}.get(hit.get("owner"), "")


def _hit_timestamp(hit: dict) -> int | None:
    for key in ("memory_at", "gmt_created"):
        val = hit.get(key)
        if isinstance(val, int) and val > 0:
            return val
    created_at = hit.get("created_at")
    if isinstance(created_at, str) and created_at:
        try:
            from datetime import datetime

            if created_at.endswith("Z"):
                created_at = created_at[:-1] + "+00:00"
            return int(datetime.fromisoformat(created_at).timestamp())
        except ValueError:
            return None
    return None


def format_search_hit_line(
    hit: dict,
    *,
    prefix: str = "- ",
    include_date: bool = True,
    include_evolution: bool = False,
    include_relations: bool = True,
) -> str:
    """Format one flat search hit (``memory`` or ``content`` key) for QA prompts."""
    category = hit.get("category", "")
    text = _hit_text(hit)
    cat_part = f"[{category}] " if category else ""
    date_suffix = ""
    if include_date:
        mem_date = format_memory_timestamp(_hit_timestamp(hit))
        if mem_date:
            date_suffix = f" (conversation date: {mem_date})"
    line = f"{prefix}{cat_part}{_source_tag(hit)}{text}{date_suffix}"
    if include_relations:
        extra = relation_lines(hit, prefix=f"{prefix}  ")
        if extra:
            line = "\n".join([line, *extra])

    chain = hit.get("evolution_chain")
    if not include_evolution or not chain or len(chain) <= 1:
        return line

    older = list(reversed(chain[1:]))
    tail = "\n".join(
        f"{prefix}  · {format_memory_timestamp(_hit_timestamp(ver)) or '?'}: {ver.get('content', '')}"
        for ver in older
    )
    current = f"{prefix}  → current: {chain[0].get('content', text)}"
    return f"{line}\n{tail}\n{current}"


def collect_evolution_chains(hits: list[dict]) -> list[dict]:
    """Return unique hits that carry a multi-step ``evolution_chain`` (newest-first)."""
    seen_heads: set[str] = set()
    chains: list[dict] = []
    for hit in hits:
        chain = hit.get("evolution_chain")
        if not chain or len(chain) <= 1:
            continue
        head_id = str(chain[0].get("node_id") or hit.get("memory_id") or hit.get("id") or "")
        if head_id and head_id in seen_heads:
            continue
        if head_id:
            seen_heads.add(head_id)
        chains.append(hit)
    chains.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    return chains


def format_evolution_timeline(
    hits: list[dict],
    *,
    header: str = "Preference evolution (oldest → newest):",
    max_chains: int = 5,
    max_versions: int = 4,
) -> str:
    """Render evolution chains as a compact oldest→newest timeline (generic, no benchmark ids)."""
    chains = collect_evolution_chains(hits)[:max_chains]
    if not chains:
        return ""

    blocks: list[str] = [header]
    for hit in chains:
        chain = hit["evolution_chain"][:max_versions]
        ordered = list(reversed(chain))
        topic = _hit_text(hit)[:80].strip() or "topic"
        lines = [f"  · {topic}"]
        for idx, ver in enumerate(ordered):
            date = format_memory_timestamp(
                _hit_timestamp(ver) or ver.get("memory_at") or ver.get("gmt_created")
            )
            label = "CURRENT" if idx == len(ordered) - 1 else (date or "?")
            prefix = "  →" if idx == len(ordered) - 1 else "    "
            lines.append(f"{prefix} {label}: {ver.get('content', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def chain_node_ids(hit: dict) -> set[str]:
    """All node ids appearing in a hit's evolution chain."""
    chain = hit.get("evolution_chain") or []
    ids: set[str] = set()
    for ver in chain:
        nid = ver.get("node_id")
        if nid:
            ids.add(str(nid))
    mid = hit.get("memory_id") or hit.get("id")
    if mid:
        ids.add(str(mid))
    return ids


def is_schema_hit(hit: dict) -> bool:
    """A System 2 derived hit (schema, core schema or persona catalog)."""
    return hit.get("layer") == "L3_DERIVED"


def split_schema_hits(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate System 1 evidence from System 2 derived hits, keeping order."""
    evidence = [h for h in hits if not is_schema_hit(h)]
    derived = [h for h in hits if is_schema_hit(h)]
    return evidence, derived


def _derived_kind(hit: dict) -> str:
    return str(hit.get("kind") or hit.get("category") or "")


def format_schema_block(derived: list[dict], *, prefix: str = "- ") -> str:
    """Render System 2 output after the System 1 evidence, as hy-memory does.

    Schemas (basic and core) are plain memory lines with no System 2 label, no
    confidence and no attached evidence: the reader weighs them like any other
    memory. The persona catalog (exact current facts, question-blind) is rendered
    as the user's profile.
    """
    if not derived:
        return ""
    catalog = [h for h in derived if _derived_kind(h) == "persona_catalog"]
    schemas = [h for h in derived if _derived_kind(h) != "persona_catalog"]
    parts: list[str] = []
    if catalog:
        parts.append("User profile (current recorded facts by area):\n" + _hit_text(catalog[0]))
    if schemas:
        parts.append("\n".join(f"{prefix}{_hit_text(h)}" for h in schemas))
    return "\n\n".join(parts)

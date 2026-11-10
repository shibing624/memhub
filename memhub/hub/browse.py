# -*- coding: utf-8 -*-
"""Rows for the MemHub memory browser (``/ui``) and its JSON API.

A memory belongs to a project when the write that produced it carried a
workspace: the raw dialogue keeps it as ``custom["workspace_id"]`` and a
procedure as its ``workspace_id``; every other memory extracted from that
dialogue shares it through ``source_ids``. Memories of plain chats have no
project. Both live in the replicated store, so every device sees the same
projects.
"""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path
from typing import Any

from memhub.coding.types import procedure_detail
from memhub.types import MemoryNode

_WORKTREE_GITDIR_RE = re.compile(r"^(.+)[/\\]\.git[/\\]worktrees[/\\]([^/\\]+)$", re.DOTALL)
_SUBMODULE_GITDIR_RE = re.compile(r"^(.+)[/\\]\.git[/\\]modules[/\\](.+)$", re.DOTALL)
_BARE_WORKTREE_GITDIR_RE = re.compile(r"^(.+)[/\\]worktrees[/\\]([^/\\]+)$", re.DOTALL)
_URL_FORM_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+/(.+)$", re.IGNORECASE)
_SCP_FORM_RE = re.compile(r"^(?:[^/@\s]+@)?[^:/\s]+:(?!//)(.+)$")


def parse_origin_slug(url: str | None) -> str | None:
    """Turn a git remote URL into an ``org/repo`` slug, or ``None`` for local paths."""
    trimmed = (url or "").strip()
    if not trimmed:
        return None
    if (
        trimmed.lower().startswith("file:")
        or trimmed.startswith(("/", ".", "~", "\\\\"))
        or re.match(r"^[a-zA-Z]:[\\/]", trimmed)
    ):
        return None
    cleaned = re.sub(r"\.git$", "", trimmed.rstrip("/"), flags=re.IGNORECASE)
    url_match = _URL_FORM_RE.match(cleaned)
    scp_match = None if url_match else _SCP_FORM_RE.match(cleaned)
    path_part = url_match.group(1) if url_match else (scp_match.group(1) if scp_match else None)
    if not path_part:
        return None
    # Azure DevOps puts ``_git`` between the project and the repository.
    segments = [seg for seg in path_part.split("/") if seg and seg != "_git"]
    if len(segments) >= 2:
        return f"{segments[-2]}/{segments[-1]}"
    if len(segments) == 1:
        return segments[0]
    return None


def _find_git_info(workspace: str | Path) -> tuple[Path, Path | None] | None:
    """Locate the main git repository root and its config file for ``workspace``.

    Resolves subdirectories, linked ``git worktree`` checkouts, submodules, and
    bare-repository worktrees back to the parent repository root without spawning
    a ``git`` subprocess.
    """
    try:
        current = Path(workspace).expanduser().absolute()
    except OSError:
        return None

    for _ in range(64):
        git_entry = current / ".git"
        try:
            is_dir = git_entry.is_dir()
            is_file = False if is_dir else git_entry.is_file()
        except OSError:
            is_dir = is_file = False

        if is_dir:
            return current, git_entry / "config"

        if is_file:
            try:
                content = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return current, None
            if content.startswith("gitdir:"):
                raw_gitdir = content[len("gitdir:") :].strip()
                gitdir = Path(os.path.normpath(str(current / raw_gitdir)))
                wt_match = _WORKTREE_GITDIR_RE.match(str(gitdir))
                if wt_match:
                    parent = Path(wt_match.group(1))
                    return parent, parent / ".git" / "config"
                sub_match = _SUBMODULE_GITDIR_RE.match(str(gitdir))
                if sub_match:
                    parent = Path(sub_match.group(1))
                    return parent, parent / ".git" / "config"
                bare_match = _BARE_WORKTREE_GITDIR_RE.match(str(gitdir))
                if bare_match and (gitdir / "commondir").is_file():
                    bare_root = Path(bare_match.group(1))
                    repo_root = (
                        bare_root.parent
                        if bare_root.name.startswith(".")
                        else bare_root
                    )
                    return repo_root, bare_root / "config"
                return current, gitdir / "config"
            return current, None

        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def resolve_workspace_root(workspace: str | Path) -> str:
    """Resolve ``workspace`` (subdirectory, worktree, submodule) to its main repo root.

    Deliberately not cached: a worktree's ``.git`` file appears the moment ``git
    worktree add`` returns, so a memoised answer would keep reporting the folder's
    own name for the rest of the process. Walking up to ``.git`` costs microseconds.
    """
    raw = str(workspace).strip()
    if not raw:
        return ""
    info = _find_git_info(raw)
    if info is not None:
        return str(info[0])
    return str(Path(raw).expanduser())


def git_remote_slug(workspace: str | Path) -> str | None:
    """Extract the ``org/repo`` origin slug from ``workspace``'s git config, if any."""
    info = _find_git_info(workspace)
    if info is None or info[1] is None or not info[1].is_file():
        return None
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(info[1], encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    for section in ('remote "origin"', *[s for s in parser.sections() if s.startswith("remote ")]):
        if parser.has_section(section):
            url = parser.get(section, "url", fallback="").strip()
            slug = parse_origin_slug(url)
            if slug:
                return slug
    return None


def canonical_workspace(workspace: str | None) -> str:
    """Canonical project identifier for a workspace directory."""
    if not workspace or not str(workspace).strip():
        return ""
    return resolve_workspace_root(str(workspace).strip())


def workspace_keys(workspace: str) -> set[str]:
    """All comparable identity keys for ``workspace`` (canonical root + git remote slug)."""
    raw = workspace.strip()
    if not raw:
        return set()
    keys: set[str] = set()
    root = resolve_workspace_root(raw)
    keys.add(root)
    try:
        keys.add(str(Path(root).resolve(strict=False)))
        keys.add(str(Path(raw).expanduser().resolve(strict=False)))
    except OSError:
        pass
    slug = git_remote_slug(raw)
    if slug:
        keys.add(f"remote:{slug.lower()}")
    return keys


def _workspaces_match(
    found: str,
    found_remote: str,
    target: str,
    keys_cache: dict[str, set[str]],
    git_cache: dict[str, tuple[bool, bool, str]],
) -> bool:
    if found not in keys_cache:
        keys_cache[found] = workspace_keys(found)
    if target not in keys_cache:
        keys_cache[target] = workspace_keys(target)
    found_keys = keys_cache[found] | (
        {f"remote:{found_remote.lower()}"} if found_remote else set()
    )
    if found_keys & keys_cache[target]:
        return True

    # Cross-machine fallback: a session synced from another machine stores a
    # checkout path that does not exist here, so its canonical root can never be
    # compared, and its remote is known only when the write recorded it
    # (``workspace_remote``). Without one, match a local git repo of the same
    # folder name. With remotes on both sides that differ, the projects differ: a
    # fork named like the upstream repo must not receive the upstream's memory.
    found_named = any(k.startswith("remote:") for k in found_keys)
    target_named = any(k.startswith("remote:") for k in keys_cache[target])
    if found_named and target_named:
        return False
    for w in (found, target):
        if w not in git_cache:
            expanded = Path(w).expanduser()
            exists = expanded.exists()
            info = _find_git_info(w) if exists else None
            leaf = (info[0].name if info is not None else expanded.name).lower()
            git_cache[w] = (exists, info is not None, leaf)
    f_exists, f_is_git, f_leaf = git_cache[found]
    t_exists, t_is_git, t_leaf = git_cache[target]
    return bool(f_leaf) and f_leaf == t_leaf and (
        (t_is_git and not f_exists) or (f_is_git and not t_exists)
    )


def node_workspace(node: MemoryNode) -> str:
    """The workspace a raw dialogue or a procedure was written from, else ``""``."""
    detail = procedure_detail(node)
    if detail is not None:
        return detail.workspace_id or ""
    return str((node.custom or {}).get("workspace_id") or "")


def workspaces_by_source(nodes: list[MemoryNode]) -> dict[str, str]:
    """Raw dialogue id -> its workspace (its own, else its procedures')."""
    out: dict[str, str] = {}
    for node in nodes:
        workspace = node_workspace(node)
        if not workspace:
            continue
        if procedure_detail(node) is None:
            out[node.node_id] = workspace
            continue
        for source_id in (node.custom or {}).get("source_ids", []):
            out.setdefault(source_id, workspace)
    return out


def workspace_sessions(nodes: list[MemoryNode], workspace: str) -> list[str]:
    """Session ids of the dialogues written from ``workspace`` (worktrees, subdirs,
    and synced git remotes resolved).

    ``nodes`` are raw dialogues and procedures; every memory extracted from a
    dialogue carries its session id, so these ids scope all of them.
    """
    keys_cache: dict[str, set[str]] = {}
    git_cache: dict[str, tuple[bool, bool, str]] = {}
    sessions: set[str] = set()
    for node in nodes:
        found = node_workspace(node)
        if not found or not node.session_id:
            continue
        remote = str((node.custom or {}).get("workspace_remote") or "")
        if _workspaces_match(found, remote, workspace, keys_cache, git_cache):
            sessions.add(node.session_id)
    return sorted(sessions)


def browse_rows(nodes: list[MemoryNode]) -> list[dict[str, Any]]:
    """Every version, newest first, with its project and source host."""
    by_source = workspaces_by_source(nodes)
    canon_cache: dict[str, str] = {}
    rows = []
    for node in nodes:
        detail = procedure_detail(node)
        source_ids = list((node.custom or {}).get("source_ids", []))
        raw_project = (
            node_workspace(node)
            or by_source.get(node.node_id)
            or next((by_source[s] for s in source_ids if s in by_source), "")
        )
        if raw_project not in canon_cache:
            canon_cache[raw_project] = canonical_workspace(raw_project) if raw_project else ""
        project = canon_cache[raw_project]
        rows.append(
            {
                "id": node.node_id,
                "layer": node.layer.value,
                "kind": node.kind,
                "content": node.content,
                "project": project,
                "source": node.session_id.partition(":")[0],
                "session_id": node.session_id,
                "at": node.memory_at or node.gmt_created,
                "is_latest": node.is_latest,
                # Retired by a delete, as opposed to replaced by a newer version.
                "deleted": bool((node.custom or {}).get("archived")),
                "supersedes": list(node.supersedes),
                "superseded_by": list(node.superseded_by),
                "source_ids": source_ids,
                "procedure": detail.to_dict() if detail is not None else None,
            }
        )
    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows


def current_rows(
    rows: list[dict[str, Any]], *, query: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """Editable current versions only; ``query`` is a content substring filter."""
    current = [
        row for row in rows if row.get("is_latest") and not row.get("deleted")
    ]
    needle = query.strip().casefold()
    if needle:
        current = [
            row for row in current if needle in str(row.get("content", "")).casefold()
        ]
    return current[:limit]


__all__ = [
    "browse_rows",
    "canonical_workspace",
    "current_rows",
    "git_remote_slug",
    "node_workspace",
    "parse_origin_slug",
    "resolve_workspace_root",
    "workspace_keys",
    "workspace_sessions",
    "workspaces_by_source",
]

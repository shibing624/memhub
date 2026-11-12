# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Vector store abstraction and a Chroma-backed implementation for
upserting, querying and managing MemoryNode embeddings with metadata filters.
"""
from abc import ABC, abstractmethod
import base64
import threading
import json
import sqlite3
import hashlib
import math
import fcntl
import struct
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable
from typing import Any, Iterator

import chromadb
from chromadb.config import Settings

from memhub.storage.history_store import HistoryStore
from memhub.types import _LIST_SEP, Layer, MemoryNode, MemoryStatus

_COLLECTION = "memories"
# Log entries replicated by one baseline row when a store predates the log.
_BASELINE_CHUNK = 200
# Version links and retirement only grow; every other field takes the newest value.
_UNION_FIELDS = ("supersedes", "superseded_by")


def pack_vector(vector: list[float]) -> str:
    """A vector as base64 little-endian float16: what the replication log carries.

    Decimal floats were three quarters of the log; float16 keeps cosine
    similarity unchanged to four decimals at a fifth of the size.
    """
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode("ascii")


def unpack_vector(text: str) -> list[float]:
    raw = base64.b64decode(text)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def _packed(kind: str, data: Any) -> Any:
    """Log entry data as it travels: vectors packed."""
    if kind == "nodes":
        return [{**row, "embedding": pack_vector(row["embedding"])} for row in data]
    if kind == "plan":
        payload = data["payload"]
        nodes = [{**n, "embedding": pack_vector(n["embedding"])} for n in payload["nodes"]]
        return {**data, "payload": {**payload, "nodes": nodes}}
    return data


def _unpacked(kind: str, data: Any) -> Any:
    if kind == "nodes":
        return [{**row, "embedding": unpack_vector(row["embedding"])} for row in data]
    if kind == "plan":
        payload = data["payload"]
        nodes = [{**n, "embedding": unpack_vector(n["embedding"])} for n in payload["nodes"]]
        return {**data, "payload": {**payload, "nodes": nodes}}
    return data


def _without_vectors(payload: dict) -> dict:
    """A committed plan as the journal keeps it: Chroma holds the vectors."""
    nodes = [{k: v for k, v in n.items() if k != "embedding"} for n in payload["nodes"]]
    return {**payload, "nodes": nodes}


def _union(current: str, incoming: str) -> str:
    # Sorted, so replicas that merged the same links in another order agree.
    items = set(current.split(_LIST_SEP) if current else [])
    items.update(incoming.split(_LIST_SEP) if incoming else [])
    return _LIST_SEP.join(sorted(items))


def merge_metadata(current: dict, incoming: dict) -> dict:
    """Merge a replicated metadata patch into the stored metadata.

    Order-independent for the version structure: retirement is final (``is_latest``
    is AND-ed), ``supersedes`` / ``superseded_by`` are set unions and ``custom``
    keys are merged. Other fields take the incoming value, which is the newest one
    because each origin's entries are applied in order. ``revision`` is a local
    concurrency token and moves forward whenever anything changes.
    """
    merged = {**current, **incoming}
    if "is_latest" in incoming:
        merged["is_latest"] = bool(current["is_latest"]) and bool(incoming["is_latest"])
    for key in _UNION_FIELDS:
        if key in incoming:
            merged[key] = _union(current[key], incoming[key])
    if "custom" in incoming:
        merged["custom"] = json.dumps(
            {**json.loads(current["custom"]), **json.loads(incoming["custom"])},
            ensure_ascii=False,
        )
    merged["revision"] = int(current["revision"])
    if merged != current:
        merged["revision"] = max(int(current["revision"]) + 1, int(incoming.get("revision", 0)))
    return merged


class _CountingCollection:
    """The Chroma collection, with every mutating call reported to ``on_write``.

    All writes of the store go through ``self.collection``; counting here covers
    each of them (local writes, replication, snapshots, plan recovery) at one place.
    """

    _WRITES = frozenset({"add", "upsert", "update", "delete"})

    def __init__(self, collection: Any, on_write: Callable[[], None]) -> None:
        self._collection = collection
        self._on_write = on_write

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._collection, name)
        if name not in self._WRITES:
            return attr

        def write(*args: Any, **kwargs: Any) -> Any:
            out = attr(*args, **kwargs)
            self._on_write()
            return out

        return write


def _normalize_where(where: dict | None) -> dict | None:
    """Wrap a multi-key where-filter in Chroma's $and form; pass single keys through."""
    if not where:
        return None
    if len(where) == 1:
        return where
    return {"$and": [{key: value} for key, value in where.items()]}


def _plan_semantic(nodes: list[MemoryNode]) -> list[dict]:
    """Replay identity of a version plan: everything except embeddings/revisions."""
    return [
        {
            "id": n.node_id,
            "content": n.content,
            "layer": n.layer.value,
            "kind": n.kind,
            "supersedes": n.supersedes,
            "superseded_by": n.superseded_by,
            "is_latest": n.is_latest,
            "source_ids": (n.custom or {}).get("source_ids", []),
            "valid_from": n.valid_from,
            "valid_until": n.valid_until,
            "update_type": (n.custom or {}).get("update_type"),
            "conflicts_with": (n.custom or {}).get("conflicts_with", []),
            "owner": n.owner,
            "session_id": n.session_id,
            "memory_at": n.memory_at,
            "tags": n.tags,
            "temporal_scope": (n.custom or {}).get("temporal_scope"),
            "negation": (n.custom or {}).get("negation", False),
        }
        for n in nodes
    ]


class VectorStore(ABC):
    """Abstract interface for embedding-backed memory storage."""

    @property
    @abstractmethod
    def generation(self) -> int:
        """Changes whenever any node of this store is written, patched or removed
        (local or replicated). A cache keyed on it never serves a stale read."""

    @abstractmethod
    def upsert(self, nodes: list[MemoryNode]) -> None: ...

    @abstractmethod
    def query(
        self, *, embedding: list[float], where: dict, top_k: int = 10
    ) -> list[MemoryNode]: ...

    @abstractmethod
    def get(self, node_id: str) -> MemoryNode | None: ...

    @abstractmethod
    def get_by_ids(self, node_ids: list[str]) -> dict[str, MemoryNode]: ...

    @abstractmethod
    def get_many(self, where: dict, limit: int | None = 1000) -> list[MemoryNode]: ...

    @abstractmethod
    def get_ids(self, where: dict) -> list[str]: ...

    @abstractmethod
    def update_payload(self, node_id: str, patch: dict) -> None: ...

    @abstractmethod
    def update_payload_many(self, patches: dict[str, dict]) -> None: ...

    @abstractmethod
    def delete(self, node_ids: list[str]) -> None: ...


class ChromaVectorStore(VectorStore):
    """Persistent Chroma-backed vector store using cosine distance."""

    def __init__(self, storage_dir: str, *, history: HistoryStore | None = None):
        self._history = history
        Path(storage_dir).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._lock_path = Path(storage_dir) / "versions.lock"
        self._journal = sqlite3.connect(
            str(Path(storage_dir) / "versions.db"),
            check_same_thread=False,
        )
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS epochs (scope TEXT PRIMARY KEY, epoch INTEGER NOT NULL)"
        )
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, scope TEXT NOT NULL, epoch INTEGER NOT NULL, payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"
        )
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS projections (plan_id TEXT PRIMARY KEY, done INTEGER NOT NULL DEFAULT 0)"
        )
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # Replication log: every change of this store, and every change received
        # from another store, by (origin, seq). ``body`` holds a reference (plan
        # id, node ids) once applied, the received data while it is not.
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS log (origin TEXT NOT NULL, seq INTEGER NOT NULL, "
            "kind TEXT NOT NULL, body TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY (origin, seq))"
        )
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS tombstones (node_id TEXT PRIMARY KEY)"
        )
        indexed = self._journal.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'"
        ).fetchone()
        # request_key -> plan, written with the plan row; looked up on every write.
        self._journal.execute(
            "CREATE TABLE IF NOT EXISTS requests (request_key TEXT PRIMARY KEY, plan_id TEXT NOT NULL)"
        )
        if not indexed:
            for plan_id, payload in self._journal.execute(
                "SELECT id, payload FROM plans"
            ).fetchall():
                key = json.loads(payload).get("request_key")
                if key:
                    self._journal.execute(
                        "INSERT OR IGNORE INTO requests (request_key, plan_id) VALUES (?, ?)",
                        (key, plan_id),
                    )
        self._journal.commit()
        self.client = chromadb.PersistentClient(
            path=f"{storage_dir}/chroma",
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = _CountingCollection(
            self.client.get_or_create_collection(
                name=_COLLECTION,
                metadata={"hnsw:space": "cosine", "hnsw:num_threads": 4},
            ),
            self._bump_generation,
        )

        try:
            with self._guard(recover=False):
                self._validate_schema()
                self._recover_pending()
                self.origin = self._ensure_origin()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _guard(self, *, recover: bool = True) -> Iterator[None]:
        """Serialize cooperating processes and recover before exposing any read."""
        with self._lock:
            if getattr(self._local, "inside", False):
                yield
                return
            with self._lock_path.open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._local.inside = True
                try:
                    if recover:
                        self._recover_pending()
                    yield
                finally:
                    self._local.inside = False
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _check_metadata_schema(meta: dict) -> None:
        if (
            meta.get("layer_schema_version") != 4
            or meta.get("layer") not in Layer._value2member_map_
            or meta.get("status") != MemoryStatus.ACTIVE.value
        ):
            raise ValueError("unsupported memory schema; use a new storage directory")

    def _recover_pending(self) -> None:
        """Validate every pending payload before recovering any of them."""
        pending = [
            (plan_id, scope, epoch, json.loads(payload))
            for plan_id, scope, epoch, payload in self._journal.execute(
                "SELECT id, scope, epoch, payload FROM plans WHERE done=0 ORDER BY rowid"
            ).fetchall()
        ]
        for _, _, _, payload in pending:
            for row in payload["nodes"]:
                self._check_metadata_schema(row["meta"])
            for meta in payload["patches"].values():
                self._check_metadata_schema(meta)
        for plan_id, scope, epoch, payload in pending:
            self._finish_plan(plan_id, scope, epoch, payload)

    # ---- replication log -----------------------------------------------------

    def _ensure_origin(self) -> str:
        """This store's replication identity, created once.

        A store that already holds memories when the log starts gets one baseline
        entry per chunk of node ids, so a peer receives everything written before.
        """
        row = self._journal.execute("SELECT value FROM meta WHERE key='origin'").fetchone()
        if row:
            return row[0]
        origin = str(uuid.uuid4())
        ids: list[str] = []
        while True:
            page = self.collection.get(limit=1000, offset=len(ids), include=[])["ids"]
            if not page:
                break
            ids.extend(page)
        with self._journal:
            self._journal.execute("INSERT INTO meta(key,value) VALUES ('origin',?)", (origin,))
            self.origin = origin
            for offset in range(0, len(ids), _BASELINE_CHUNK):
                self._append_log("nodes", json.dumps(ids[offset : offset + _BASELINE_CHUNK]))
        return origin

    def _append_log(self, kind: str, body: str) -> None:
        """Append a local change; call inside a journal transaction."""
        seq = self._journal.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM log WHERE origin=?", (self.origin,)
        ).fetchone()[0]
        self._journal.execute(
            "INSERT INTO log(origin,seq,kind,body,applied) VALUES (?,?,?,?,1)",
            (self.origin, seq, kind, body),
        )

    def log_heads(self) -> dict[str, int]:
        """Per origin, the last sequence such that every entry up to it was received.

        A pull asks for entries after these heads, so an entry missing from the
        middle (a file a cloud folder has not downloaded yet) is asked for again.
        After a compaction the log starts at ``log_base``: everything before it
        lives in the snapshot, so a head never goes below its base.
        """
        with self._guard():
            rows = self._journal.execute(
                "SELECT origin, seq FROM log ORDER BY origin, seq"
            ).fetchall()
        base = self.log_base()
        heads: dict[str, int] = dict(base)
        for origin, seq in rows:
            if seq <= base.get(origin, 0):
                continue
            if seq == heads.get(origin, 0) + 1:
                heads[origin] = seq
        return heads

    def log_base(self) -> dict[str, int]:
        """Per origin, the last sequence folded into the published snapshot."""
        with self._guard():
            row = self._journal.execute(
                "SELECT value FROM meta WHERE key='log_base'"
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def snapshot_epoch(self) -> int:
        """The highest snapshot this store has adopted (0 before any compaction)."""
        with self._guard():
            row = self._journal.execute(
                "SELECT value FROM meta WHERE key='snapshot_epoch'"
            ).fetchone()
        return int(row[0]) if row else 0

    def applied_heads(self) -> dict[str, int]:
        """Per origin, the last sequence such that every entry up to it is applied.

        This is what a snapshot of this store covers: a received entry still
        waiting for a node is not in the state yet.
        """
        with self._guard():
            base = self.log_base()
            rows = self._journal.execute(
                "SELECT origin, seq FROM log WHERE applied=1 ORDER BY origin, seq"
            ).fetchall()
        heads: dict[str, int] = dict(base)
        for origin, seq in rows:
            if seq == heads.get(origin, 0) + 1:
                heads[origin] = seq
        return heads

    def _all_rows(self) -> list[dict]:
        """Every node row, vectors included; page by page, so nothing is truncated."""
        rows: list[dict] = []
        offset = 0
        while True:
            page = self.collection.get(
                limit=1000, offset=offset,
                include=["metadatas", "documents", "embeddings"],
            )
            rows.extend(
                {
                    "id": nid,
                    "content": doc,
                    "embedding": [float(v) for v in emb],
                    "meta": meta,
                }
                for nid, doc, emb, meta in zip(
                    page["ids"], page["documents"], page["embeddings"], page["metadatas"]
                )
            )
            offset += len(page["ids"])
            if len(page["ids"]) < 1000:
                return rows

    def snapshot_state(self, epoch: int) -> dict:
        """This store's whole state, standing in for every log entry it has applied.

        It replaces the log prefix up to ``base`` (``applied_heads``) on the sync
        target. Tombstones travel with it, so a node deleted before the snapshot
        never comes back through an older entry; evidence edges travel with it,
        so a device built from it projects the same graph.
        """
        with self._guard():
            base = self.applied_heads()
            rows = self._all_rows()
            tombstones = sorted(
                r[0] for r in self._journal.execute("SELECT node_id FROM tombstones")
            )
            present = {row["id"] for row in rows}
            edges = sorted(
                {
                    (a, b)
                    for (payload,) in self._journal.execute("SELECT payload FROM plans")
                    for a, b in json.loads(payload).get("edges", [])
                    if a in present and b in present
                }
            )
        return {
            "epoch": epoch,
            "base": base,
            "nodes": _packed("nodes", rows),
            "tombstones": tombstones,
            "edges": [list(edge) for edge in edges],
        }

    def mark_snapshot(self, epoch: int, base: dict[str, int]) -> None:
        """Record that the sync target's log now starts after ``base``."""
        with self._guard(), self._journal:
            self._set_meta("log_base", json.dumps(base))
            self._set_meta("snapshot_epoch", str(epoch))

    def adopt_snapshot(self, state: dict) -> list[tuple[str, str, str]]:
        """Bring this store to the snapshot's state; the scopes that changed.

        The snapshot carries what entries up to its ``base`` did. A node this
        store holds that the snapshot lacks was deleted before it, unless an
        applied log entry past the base created it: this device's own writes
        not covered yet, and entries received from beyond the snapshot. A node
        such an entry also changed keeps its local field values: they are newer
        than the snapshot's, and the entry that made them still travels.
        Retirement and version links merge the way replication merges them, so
        a store that already has everything ends up unchanged.
        """
        base = {name: int(seq) for name, seq in state["base"].items()}
        rows = _unpacked("nodes", state["nodes"])
        scopes: set[tuple[str, str, str]] = set()
        with self._guard():
            created, changed = self._ids_beyond(base)
            keep = {row["id"] for row in rows} | created
            gone = sorted(
                (set(self._all_ids()) - keep) | (set(state["tombstones"]) - created)
            )
            old = self.get_by_ids(gone)
            if old:
                self.collection.delete(ids=list(old))
            scopes.update((n.app_id, n.user_id, n.agent_id) for n in old.values())
            with self._journal:
                self._journal.executemany(
                    "INSERT OR IGNORE INTO tombstones(node_id) VALUES (?)",
                    [(nid,) for nid in gone],
                )
            touched = [row["id"] for row in rows if row["id"] in changed]
            local = (
                self.collection.get(ids=touched, include=["metadatas"])
                if touched
                else {"ids": [], "metadatas": []}
            )
            newer = dict(zip(local["ids"], local["metadatas"]))
            self._merge_nodes([row for row in rows if row["id"] not in newer])
            self._keep_newer(
                {row["id"]: row["meta"] for row in rows if row["id"] in newer}, newer
            )
            scopes.update(
                (r["meta"]["app_id"], r["meta"]["user_id"], r["meta"]["agent_id"])
                for r in rows
                if r["meta"]["layer"] != Layer.L0_RAW.value
            )
            for scope in scopes:
                self._bump_epoch(scope)
            schemas = [
                row["id"] for row in rows if row["meta"]["layer"] == Layer.L3_DERIVED.value
            ]
            present = set(self.collection.get(ids=schemas, include=[])["ids"]) if schemas else set()
            with self._journal:
                # A received entry the snapshot covers would wait forever for the
                # entries before it, which the sync target no longer has.
                for name, seq in base.items():
                    if name != self.origin:
                        self._journal.execute(
                            "DELETE FROM log WHERE origin=? AND seq<=? AND applied=0",
                            (name, seq),
                        )
                self._record_projection(
                    rows, state["edges"], present, int(state["epoch"])
                )
                self._set_meta("log_base", json.dumps(base))
                self._set_meta("snapshot_epoch", str(int(state["epoch"])))
            scopes.update(self._apply_received_scopes())
        return sorted(scopes)

    def _keep_newer(self, snapshot: dict[str, dict], local: dict[str, dict]) -> None:
        """Merge the snapshot's metadata under local values a later entry made."""
        ids: list[str] = []
        metas: list[dict] = []
        for nid, theirs in snapshot.items():
            ours = local[nid]
            merged = merge_metadata(theirs, ours)
            same = {k: v for k, v in merged.items() if k != "revision"} == {
                k: v for k, v in ours.items() if k != "revision"
            }
            merged["revision"] = int(ours["revision"]) + (0 if same else 1)
            if not same:
                ids.append(nid)
                metas.append(merged)
        if ids:
            self.collection.update(ids=ids, metadatas=metas)

    def _apply_received_scopes(self) -> set[tuple[str, str, str]]:
        scopes: set[tuple[str, str, str]] = set()
        self._apply_received(scopes)
        return scopes

    def _record_projection(
        self, rows: list[dict], edges: list, present: set[str], epoch: int
    ) -> None:
        """Queue the snapshot's schemas and evidence edges for graph projection,
        one committed plan per scope, the way a received plan is queued."""
        by_scope: dict[str, dict] = {}
        meta_of = {row["id"]: row["meta"] for row in rows if row["id"] in present}
        for nid, meta in meta_of.items():
            scope = json.dumps([meta["app_id"], meta["user_id"], meta["agent_id"]])
            by_scope.setdefault(scope, {"nodes": [], "patches": {}, "edges": []})
            by_scope[scope]["nodes"].append({"id": nid, "meta": {"layer": meta["layer"]}})
        for schema_id, fact_id in edges:
            meta = meta_of.get(schema_id)
            if meta is None:
                continue
            scope = json.dumps([meta["app_id"], meta["user_id"], meta["agent_id"]])
            by_scope.setdefault(scope, {"nodes": [], "patches": {}, "edges": []})
            by_scope[scope]["edges"].append([schema_id, fact_id])
        for scope, payload in by_scope.items():
            plan_id = f"snapshot:{epoch}:{scope}"
            self._journal.execute(
                "INSERT OR IGNORE INTO plans(id,scope,epoch,payload,done) VALUES (?,?,?,?,1)",
                (plan_id, scope, 0, json.dumps(payload)),
            )
            self._journal.execute(
                "INSERT OR IGNORE INTO projections(plan_id) VALUES (?)", (plan_id,)
            )

    def _all_ids(self) -> list[str]:
        ids: list[str] = []
        while True:
            page = self.collection.get(limit=1000, offset=len(ids), include=[])["ids"]
            if not page:
                return ids
            ids.extend(page)

    def _ids_beyond(self, base: dict[str, int]) -> tuple[set[str], set[str]]:
        """Node ids applied log entries past ``base`` created, and ones they changed."""
        created: set[str] = set()
        changed: set[str] = set()
        for name, seq, kind, body in self._journal.execute(
            "SELECT origin, seq, kind, body FROM log WHERE applied=1"
        ).fetchall():
            if seq <= base.get(name, 0):
                continue
            if kind == "nodes":
                created.update(json.loads(body))
            elif kind == "patch":
                changed.update(json.loads(body))
            elif kind == "plan":
                (payload,) = self._journal.execute(
                    "SELECT payload FROM plans WHERE id=?", (json.loads(body),)
                ).fetchone()
                plan = json.loads(payload)
                created.update(n["id"] for n in plan["nodes"])
                changed.update(plan["patches"])
        return created, changed | created

    def _set_meta(self, key: str, value: str) -> None:
        self._journal.execute(
            "INSERT INTO meta(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _node_rows(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        got = self.collection.get(
            ids=ids, include=["metadatas", "documents", "embeddings"]
        )
        return [
            {
                "id": nid,
                "content": doc,
                "embedding": [float(v) for v in emb],
                "meta": meta,
            }
            for nid, doc, emb, meta in zip(
                got["ids"], got["documents"], got["embeddings"], got["metadatas"]
            )
        ]

    def export_log(
        self,
        after: dict[str, int] | None = None,
        *,
        origin: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Log entries newer than ``after`` (per origin), oldest first per origin.

        ``origin`` restricts the export to one origin (a store publishing its own
        changes). Entries carry their data: plan payloads and node rows with
        embeddings, so a receiver needs no model call.
        """
        after = after or {}
        out: list[dict] = []
        with self._guard():
            origins = (
                [origin]
                if origin is not None
                else [r[0] for r in self._journal.execute(
                    "SELECT DISTINCT origin FROM log ORDER BY origin"
                ).fetchall()]
            )
            for name in origins:
                rows = self._journal.execute(
                    "SELECT seq, kind, body, applied FROM log WHERE origin=? AND seq>? "
                    "ORDER BY seq LIMIT ?",
                    (name, after.get(name, 0), limit - len(out)),
                ).fetchall()
                for seq, kind, body, applied in rows:
                    out.append(
                        {
                            "origin": name,
                            "seq": seq,
                            "kind": kind,
                            "data": _packed(
                                kind, self._entry_data(kind, json.loads(body), applied)
                            ),
                        }
                    )
                if len(out) >= limit:
                    break
        return out

    def _entry_data(self, kind: str, body, applied: int):
        if not applied:
            return body
        if kind == "plan":
            plan_id, scope, epoch, payload = self._journal.execute(
                "SELECT id, scope, epoch, payload FROM plans WHERE id=?", (body,)
            ).fetchone()
            payload = json.loads(payload)
            # Vectors come from Chroma; a node deleted since then no longer travels.
            vectors = {row["id"]: row["embedding"] for row in self._node_rows(
                [n["id"] for n in payload["nodes"]]
            )}
            payload["nodes"] = [
                {**n, "embedding": vectors[n["id"]]}
                for n in payload["nodes"]
                if n["id"] in vectors
            ]
            payload["patches"] = self._live(payload["patches"])
            return {"id": plan_id, "scope": scope, "epoch": epoch, "payload": payload}
        if kind == "nodes":
            return self._node_rows(body)
        if kind == "patch":
            return self._live(body)
        return body

    def _live(self, patches: dict[str, dict]) -> dict[str, dict]:
        """Drop patches of deleted nodes: a receiver may never get those nodes, and
        a patch waiting for a node that cannot arrive would stall its origin."""
        dead = self._tombstoned(list(patches))
        return {nid: meta for nid, meta in patches.items() if nid not in dead}

    def import_log(self, entries: list[dict]) -> dict[str, int]:
        """Store entries from other origins and apply every one whose inputs exist.

        Each origin's entries apply in sequence order. An entry that patches a
        node this store has not received yet waits (with every later entry of its
        origin) until that node arrives from another origin. Re-receiving an entry
        is a no-op, so pulls may overlap.
        """
        received = 0
        with self._guard():
            for entry in entries:
                if entry["origin"] == self.origin:
                    continue
                try:
                    data = _unpacked(entry["kind"], entry["data"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"log entry {entry['origin']}/{entry['seq']} is not in the "
                        "current sync format (vectors must be packed): that device "
                        "runs an older memhub. Upgrade it, delete its log/"
                        f"{entry['origin']}/ from the target, and it pushes again"
                    ) from exc
                with self._journal:
                    cursor = self._journal.execute(
                        "INSERT OR IGNORE INTO log(origin,seq,kind,body,applied) "
                        "VALUES (?,?,?,?,0)",
                        (
                            entry["origin"],
                            entry["seq"],
                            entry["kind"],
                            json.dumps(data, ensure_ascii=False),
                        ),
                    )
                received += cursor.rowcount
            scopes: set[tuple[str, str, str]] = set()
            applied = self._apply_received(scopes)
            pending = self._journal.execute(
                "SELECT COUNT(*) FROM log WHERE applied=0"
            ).fetchone()[0]
        return {
            "received": received,
            "applied": applied,
            "pending": pending,
            "scopes": sorted(scopes),
        }

    def _apply_received(self, scopes: set[tuple[str, str, str]]) -> int:
        """Apply in order; ``scopes`` collects scopes whose facts or versions changed."""
        applied = 0
        progress = True
        base = self.log_base()
        while progress:
            progress = False
            for (origin,) in self._journal.execute(
                "SELECT DISTINCT origin FROM log WHERE applied=0"
            ).fetchall():
                done = max(
                    base.get(origin, 0),
                    self._journal.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM log WHERE origin=? AND applied=1",
                        (origin,),
                    ).fetchone()[0],
                )
                for seq, kind, body in self._journal.execute(
                    "SELECT seq, kind, body FROM log WHERE origin=? AND applied=0 ORDER BY seq",
                    (origin,),
                ).fetchall():
                    # An origin's entries apply strictly in order, without gaps.
                    if seq != done + 1:
                        break
                    reference = self._apply_entry(kind, json.loads(body), scopes)
                    if reference is None:
                        break
                    with self._journal:
                        self._journal.execute(
                            "UPDATE log SET applied=1, body=? WHERE origin=? AND seq=?",
                            (json.dumps(reference), origin, seq),
                        )
                    applied += 1
                    done = seq
                    progress = True
        return applied

    def _tombstoned(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {
            r[0]
            for r in self._journal.execute(
                f"SELECT node_id FROM tombstones WHERE node_id IN ({marks})", ids
            ).fetchall()
        }

    def _forget_tombstones(self, ids: list[str]) -> None:
        self._journal.executemany(
            "DELETE FROM tombstones WHERE node_id=?", [(nid,) for nid in ids]
        )

    def _revive(self, ids: list[str]) -> None:
        """A local write reusing a deleted id ends that deletion, on every replica.

        Ids are derived from content, so a retried write can reuse one. Replicas
        get a ``revive`` entry ahead of the write: a tombstone alone cannot tell
        that write from a stale copy, which must stay out. Call inside a journal
        transaction.
        """
        dead = sorted(self._tombstoned(ids))
        if dead:
            self._forget_tombstones(dead)
            self._append_log("revive", json.dumps(dead))

    def _missing(self, ids: list[str], arriving: set[str]) -> bool:
        wanted = [i for i in ids if i not in arriving]
        if not wanted:
            return False
        present = set(self.collection.get(ids=wanted, include=[])["ids"])
        return bool(set(wanted) - present - self._tombstoned(wanted))

    def _apply_entry(self, kind: str, data, scopes: set[tuple[str, str, str]]):
        """Apply one received entry; the stored reference, or None while it must wait."""
        if kind == "nodes":
            self._merge_nodes(data)
            scopes.update(
                (r["meta"]["app_id"], r["meta"]["user_id"], r["meta"]["agent_id"])
                for r in data
                if r["meta"]["layer"] != Layer.L0_RAW.value
            )
            return [row["id"] for row in data]
        if kind == "patch":
            if self._missing(list(data), set()):
                return None
            self._merge_patches(data)
            return data
        if kind == "revive":
            with self._journal:
                self._forget_tombstones(data)
            return data
        if kind == "delete":
            with self._journal:
                self._journal.executemany(
                    "INSERT OR IGNORE INTO tombstones(node_id) VALUES (?)",
                    [(nid,) for nid in data],
                )
            old = self.get_by_ids(data)
            if old:
                self.collection.delete(ids=list(old))
            for scope in {(n.app_id, n.user_id, n.agent_id) for n in old.values()}:
                self._bump_epoch(scope)
            return data
        payload = data["payload"]
        arriving = {row["id"] for row in payload["nodes"]}
        if self._missing(list(payload["patches"]), arriving):
            return None
        self._merge_nodes(payload["nodes"])
        self._merge_patches(payload["patches"])
        plan_id = data["id"]
        if self._history:
            for i, event in enumerate(payload.get("audit", [])):
                self._history.append(**event, event_key=f"{plan_id}:{i}")
        with self._journal:
            self._journal.execute(
                "INSERT OR IGNORE INTO plans(id,scope,epoch,payload,done) VALUES (?,?,?,?,1)",
                (plan_id, data["scope"], data["epoch"], json.dumps(_without_vectors(payload))),
            )
            if payload.get("request_key"):
                self._journal.execute(
                    "INSERT OR IGNORE INTO requests (request_key, plan_id) VALUES (?, ?)",
                    (payload["request_key"], plan_id),
                )
            if payload.get("edges") or any(
                n["meta"]["layer"] == Layer.L3_DERIVED.value for n in payload["nodes"]
            ):
                self._journal.execute(
                    "INSERT OR IGNORE INTO projections(plan_id) VALUES (?)", (plan_id,)
                )
        scope = tuple(json.loads(data["scope"]))
        self._bump_epoch(scope)
        scopes.add(scope)
        return plan_id

    def _merge_nodes(self, rows: list[dict]) -> None:
        if not rows:
            return
        for row in rows:
            self._check_metadata_schema(row["meta"])
        ids = [row["id"] for row in rows]
        dead = self._tombstoned(ids)
        current = self.collection.get(ids=ids, include=["metadatas"])
        stored = dict(zip(current["ids"], current["metadatas"]))
        fresh = [r for r in rows if r["id"] not in stored and r["id"] not in dead]
        if fresh:
            self.collection.upsert(
                ids=[r["id"] for r in fresh],
                documents=[r["content"] for r in fresh],
                embeddings=[r["embedding"] for r in fresh],
                metadatas=[r["meta"] for r in fresh],
            )
        self._merge_patches({r["id"]: r["meta"] for r in rows if r["id"] in stored})
        for scope in {
            (r["meta"]["app_id"], r["meta"]["user_id"], r["meta"]["agent_id"])
            for r in rows
            if r["meta"]["layer"] != Layer.L0_RAW.value
        }:
            self._bump_epoch(scope)

    def _merge_patches(self, patches: dict[str, dict]) -> None:
        if not patches:
            return
        current = self.collection.get(ids=list(patches), include=["metadatas"])
        ids: list[str] = []
        metas: list[dict] = []
        for nid, meta in zip(current["ids"], current["metadatas"]):
            merged = merge_metadata(meta, patches[nid])
            if merged != meta:
                ids.append(nid)
                metas.append(merged)
                self._bump_epoch((meta["app_id"], meta["user_id"], meta["agent_id"]))
        if ids:
            self.collection.update(ids=ids, metadatas=metas)

    def _finish_plan(
        self, plan_id: str, scope_key: str, epoch: int, payload: dict
    ) -> None:
        rows = payload["nodes"]
        cap = self.client.get_max_batch_size()
        for offset in range(0, len(rows), cap):
            chunk = rows[offset : offset + cap]
            self.collection.upsert(
                ids=[n["id"] for n in chunk],
                documents=[n["content"] for n in chunk],
                embeddings=[n["embedding"] for n in chunk],
                metadatas=[n["meta"] for n in chunk],
            )
        patches = payload["patches"]
        patch_ids = list(patches)
        for offset in range(0, len(patch_ids), cap):
            chunk_ids = patch_ids[offset : offset + cap]
            self.collection.update(
                ids=chunk_ids, metadatas=[patches[nid] for nid in chunk_ids]
            )
        if self._history:
            for i, event in enumerate(payload.get("audit", [])):
                self._history.append(**event, event_key=f"{plan_id}:{i}")
        with self._journal:
            if payload.get("edges") or any(
                n["meta"]["layer"] == Layer.L3_DERIVED.value for n in rows
            ):
                self._journal.execute(
                    "INSERT OR IGNORE INTO projections(plan_id) VALUES (?)", (plan_id,)
                )
            self._journal.execute(
                "INSERT INTO epochs(scope,epoch) VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET epoch=excluded.epoch",
                (scope_key, epoch + 1),
            )
            # Until now the journal held the vectors for recovery; Chroma has them.
            self._journal.execute(
                "UPDATE plans SET done=1, payload=? WHERE id=?",
                (json.dumps(_without_vectors(payload)), plan_id),
            )

    @property
    def generation(self) -> int:
        # Kept in the shared journal, not in this process: another process writing
        # the same store (CLI next to the Core) must invalidate this one's caches.
        with self._lock:
            row = self._journal.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()
        return int(row[0]) if row else 0

    def _bump_generation(self) -> None:
        with self._lock, self._journal:
            self._journal.execute(
                "INSERT INTO meta(key,value) VALUES ('generation','1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
            )

    def scope_epoch(self, scope: tuple[str, str, str]) -> int:
        with self._guard():
            row = self._journal.execute(
                "SELECT epoch FROM epochs WHERE scope=?", (json.dumps(scope),)
            ).fetchone()
            return row[0] if row else 0

    def _bump_epoch(self, scope: tuple[str, str, str]) -> None:
        with self._journal:
            self._journal.execute(
                "INSERT INTO epochs(scope,epoch) VALUES (?,1) ON CONFLICT(scope) DO UPDATE SET epoch=epoch+1",
                (json.dumps(scope),),
            )

    def _validate_schema(self) -> None:
        """Reject unsupported stored metadata without mutating historical rows."""
        offset = 0
        while True:
            rows = self.collection.get(limit=500, offset=offset, include=["metadatas"])
            if not rows["ids"]:
                return
            for meta in rows["metadatas"]:
                self._check_metadata_schema(meta)
            offset += len(rows["ids"])

    def upsert(self, nodes: list[MemoryNode]) -> None:
        """Insert or update the given nodes (embeddings, content and metadata)."""
        if not nodes:
            return
        with self._guard():
            old_nodes = self.get_by_ids([n.node_id for n in nodes])
            for node in nodes:
                old = old_nodes.get(node.node_id)
                if old:
                    if (old.app_id, old.user_id, old.agent_id) != (
                        node.app_id,
                        node.user_id,
                        node.agent_id,
                    ):
                        raise ValueError("cannot change node tenant")
                    if old.content != node.content:
                        raise ValueError(
                            "stored documents are immutable; append a version"
                        )
                    # An import/raw retry cannot replace a newer version's metadata.
                    # Documents are immutable; patches and version plans own mutations.
            nodes = [n for n in nodes if n.node_id not in old_nodes]
            if not nodes:
                return
            self.collection.upsert(
                ids=[n.node_id for n in nodes],
                embeddings=[n.embedding for n in nodes],
                documents=[n.content for n in nodes],
                metadatas=[n.to_metadata() for n in nodes],
            )
            with self._journal:
                self._revive([n.node_id for n in nodes])
                self._append_log("nodes", json.dumps([n.node_id for n in nodes]))
            for scope in {
                (n.app_id, n.user_id, n.agent_id)
                for n in nodes
                if n.layer != Layer.L0_RAW
            }:
                self._bump_epoch(scope)

    def query(
        self, *, embedding: list[float], where: dict, top_k: int = 10
    ) -> list[MemoryNode]:
        """Nearest-neighbor search under a filter; node.score is cosine similarity (1 - distance)."""
        with self._guard():
            result = self.collection.query(
                query_embeddings=[embedding],
                where=_normalize_where(where),
                n_results=top_k,
                include=["metadatas", "documents", "distances"],
            )
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]
        nodes: list[MemoryNode] = []
        for content, meta, dist in zip(documents, metadatas, distances):
            node = MemoryNode.from_storage(content, meta)
            node.score = 1.0 - dist
            nodes.append(node)
        return nodes

    def get(self, node_id: str) -> MemoryNode | None:
        """Fetch a single node (with embedding) by id, or None if absent."""
        with self._guard():
            result = self.collection.get(
                ids=[node_id], include=["metadatas", "documents", "embeddings"]
            )
        if not result["ids"]:
            return None
        embeddings = result["embeddings"]
        embedding = (
            list(embeddings[0]) if embeddings is not None and len(embeddings) else None
        )
        return MemoryNode.from_storage(
            result["documents"][0], result["metadatas"][0], embedding
        )

    def get_by_ids(self, node_ids: list[str]) -> dict[str, MemoryNode]:
        """Batch-fetch nodes by id (with embeddings when stored)."""
        if not node_ids:
            return {}
        unique_ids = list(dict.fromkeys(node_ids))
        with self._guard():
            result = self.collection.get(
                ids=unique_ids,
                include=["metadatas", "documents", "embeddings"],
            )
        out: dict[str, MemoryNode] = {}
        embeddings = result.get("embeddings")
        for i, nid in enumerate(result["ids"]):
            emb = None
            if (
                embeddings is not None
                and i < len(embeddings)
                and embeddings[i] is not None
            ):
                emb = list(embeddings[i])
            node = MemoryNode.from_storage(
                result["documents"][i], result["metadatas"][i], emb
            )
            out[nid] = node
        return out

    def get_many(self, where: dict, limit: int | None = 1000) -> list[MemoryNode]:
        """Fetch nodes matching a metadata filter (no embeddings); ``limit=None`` = all."""
        with self._guard():
            result = self.collection.get(
                where=_normalize_where(where),
                include=["metadatas", "documents"],
                limit=limit,
            )
        return [
            MemoryNode.from_storage(content, meta)
            for content, meta in zip(result["documents"], result["metadatas"])
        ]

    def get_ids(self, where: dict) -> list[str]:
        """Ids of the nodes matching a metadata filter; no documents or metadata read."""
        with self._guard():
            return list(
                self.collection.get(where=_normalize_where(where), include=[])["ids"]
            )

    def update_payload(self, node_id: str, patch: dict) -> None:
        """Merge a metadata patch into an existing node; no-op if it does not exist."""
        with self._guard():
            current = self.collection.get(ids=[node_id], include=["metadatas"])
            if not current["ids"]:
                return
            meta = dict(current["metadatas"][0])
            meta.update(patch)
            if any(
                key in patch
                for key in (
                    "content",
                    "layer",
                    "status",
                    "is_latest",
                    "supersedes",
                    "superseded_by",
                    "custom",
                    "valid_from",
                    "valid_until",
                )
            ):
                meta["revision"] = int(current["metadatas"][0].get("revision", 0)) + 1
                self._bump_epoch((meta["app_id"], meta["user_id"], meta["agent_id"]))
            self.collection.update(ids=[node_id], metadatas=[meta])
            with self._journal:
                self._append_log("patch", json.dumps({node_id: meta}, ensure_ascii=False))

    def update_payload_many(self, patches: dict[str, dict]) -> None:
        """Merge metadata patches into many existing nodes in two batched round-trips.

        Reads all target ids in one ``get`` (metadata only, no embeddings) and writes
        all merged metadatas in one ``update``. Embeddings are never rewritten. Ids that
        do not exist are skipped. This replaces per-node get/upsert loops which otherwise
        trigger one HNSW/SQLite write per node.
        """
        if not patches:
            return
        ids = list(patches.keys())
        with self._guard():
            current = self.collection.get(ids=ids, include=["metadatas"])
            existing_ids = current["ids"]
            if not existing_ids:
                return
            metas = current["metadatas"]
            new_ids: list[str] = []
            new_metas: list[dict] = []
            for nid, meta in zip(existing_ids, metas):
                merged = dict(meta)
                merged.update(patches[nid])
                if any(
                    key in patches[nid]
                    for key in (
                        "layer",
                        "status",
                        "is_latest",
                        "supersedes",
                        "superseded_by",
                        "custom",
                        "valid_from",
                        "valid_until",
                    )
                ):
                    merged["revision"] = int(meta.get("revision", 0)) + 1
                    self._bump_epoch(
                        (meta["app_id"], meta["user_id"], meta["agent_id"])
                    )
                new_ids.append(nid)
                new_metas.append(merged)
            self.collection.update(ids=new_ids, metadatas=new_metas)
            with self._journal:
                self._append_log(
                    "patch", json.dumps(dict(zip(new_ids, new_metas)), ensure_ascii=False)
                )

    def delete(self, node_ids: list[str]) -> None:
        """Physically remove the given node ids from the collection."""
        if not node_ids:
            return
        with self._guard():
            old = self.get_by_ids(node_ids)
            self.collection.delete(ids=node_ids)
            for scope in {(n.app_id, n.user_id, n.agent_id) for n in old.values()}:
                self._bump_epoch(scope)
            with self._journal:
                # A tombstone keeps a replica from bringing the node back.
                self._journal.executemany(
                    "INSERT OR IGNORE INTO tombstones(node_id) VALUES (?)",
                    [(nid,) for nid in node_ids],
                )
                self._append_log("delete", json.dumps(list(node_ids)))

    def close(self) -> None:
        with self._lock:
            self._journal.close()
            # Client.close() only exists in newer chromadb; pyproject allows >=1.0.
            close = getattr(self.client, "close", None)
            if close is not None:
                close()

    def completed_request(self, request_key: str) -> dict | None:
        """The committed result of ``request_key``, or None while it has not committed."""
        with self._guard():
            row = self._journal.execute(
                "SELECT plans.payload FROM requests JOIN plans ON plans.id = requests.plan_id "
                "WHERE requests.request_key = ? AND plans.done = 1",
                (request_key,),
            ).fetchone()
        if row is None:
            return None
        plan = json.loads(row[0])
        return {
            "extra_node_ids": [n["id"] for n in plan["nodes"]],
            "commit_passed": bool(plan["nodes"] or plan["patches"]),
            "decisions": plan.get("decisions", []),
        }

    def projection_scopes(self) -> list[tuple[str, str, str]]:
        """Scopes with committed plans not yet projected into the graph."""
        with self._guard():
            rows = self._journal.execute(
                "SELECT DISTINCT plans.scope FROM plans JOIN projections ON plans.id=projections.plan_id WHERE plans.done=1 AND projections.done=0"
            ).fetchall()
        return [tuple(json.loads(r[0])) for r in rows]

    def pending_projections(
        self, scope: tuple[str, str, str]
    ) -> list[tuple[str, list[str], list[tuple[str, str]]]]:
        """Committed derived versions and evidence edges awaiting graph projection."""
        with self._guard():
            rows = self._journal.execute(
                "SELECT plans.id, plans.payload FROM plans JOIN projections ON plans.id=projections.plan_id WHERE plans.done=1 AND projections.done=0 AND plans.scope=? ORDER BY plans.rowid",
                (json.dumps(list(scope)),),
            ).fetchall()
            plans = [(pid, json.loads(payload)) for pid, payload in rows]
            return [
                (
                    pid,
                    [
                        n["id"]
                        for n in plan["nodes"]
                        if n["meta"]["layer"] == Layer.L3_DERIVED.value
                    ],
                    [(a, b) for a, b in plan.get("edges", [])],
                )
                for pid, plan in plans
            ]

    def acknowledge_projection(self, plan_id: str) -> None:
        with self._guard(), self._journal:
            self._journal.execute(
                "UPDATE projections SET done=1 WHERE plan_id=?", (plan_id,)
            )

    def apply_version_plan(
        self,
        *,
        nodes: list[MemoryNode],
        expected: dict[str, int],
        archives: list[str],
        scope: tuple[str, str, str],
        expected_epoch: int | None = None,
        request_key: str | None = None,
        processed_evidence: list[str] | None = None,
        decisions: list[dict] | None = None,
        edges: list[tuple[str, str]] | None = None,
    ) -> bool:
        """Recoverable append/link with revision and scope CAS, never erase documents.

        A durable journal holds the entire validated operation before Chroma is
        changed. Every store read recovers unfinished work under a process lock.
        Replaying a committed stable operation is a no-op; reusing an ID with a
        different document is rejected. Direct upsert is reserved for raw/import.
        """
        with self._guard():
            scope_key = json.dumps(scope)
            epoch = self.scope_epoch(scope)
            plan_key = json.dumps(
                [
                    scope,
                    [n.node_id for n in nodes],
                    sorted(archives),
                    sorted(expected.items()),
                    sorted(processed_evidence or []),
                ]
            )
            plan_id = hashlib.sha256(plan_key.encode()).hexdigest()
            replay = self._journal.execute(
                "SELECT payload FROM plans WHERE id=? AND done=1", (plan_id,)
            ).fetchone()
            if replay:
                previous = json.loads(replay[0])
                if previous.get("semantic") != _plan_semantic(nodes):
                    raise ValueError(
                        "stable operation ID reused with different semantics"
                    )
                stored = self.get_by_ids([n.node_id for n in nodes])
                if any(
                    n.node_id not in stored or stored[n.node_id].content != n.content
                    for n in nodes
                ):
                    raise ValueError(
                        "stable operation ID reused with different content"
                    )
                return True
            if expected_epoch is not None and epoch != expected_epoch:
                return False
            current = self.get_by_ids(list(expected))
            if any(
                nid not in current
                or (current[nid].app_id, current[nid].user_id, current[nid].agent_id)
                != scope
                or current[nid].revision != rev
                or not current[nid].is_latest
                or current[nid].status != MemoryStatus.ACTIVE
                for nid, rev in expected.items()
            ):
                return False
            new_ids = {n.node_id for n in nodes}
            if len(new_ids) != len(nodes) or new_ids.intersection(current):
                raise ValueError(
                    "new version IDs must be unique and distinct from targets"
                )
            if self.get_by_ids(list(new_ids)):
                raise ValueError("new version IDs already exist")
            # Chroma retains dimension even after deleting its last document.
            # Fetch the current model: the original Collection's model is cached.
            expected_dim = (
                self.client.get_collection(name=_COLLECTION).get_model().dimension
            )
            if nodes:
                expected_dim = expected_dim or len(nodes[0].embedding or [])
            for node in nodes:
                if (
                    not node.embedding
                    or len(node.embedding) != expected_dim
                    or any(not math.isfinite(v) for v in node.embedding)
                ):
                    raise ValueError("invalid embedding dimension or nonfinite value")
            seen: set[str] = set()
            consumed: set[str] = set()
            linked: set[str] = set()
            patches: dict[str, dict] = {}
            new_by_id = {n.node_id: n for n in nodes}

            def patch_of(nid: str) -> dict:
                if nid not in patches:
                    old = current.get(nid) or new_by_id[nid]
                    patches[nid] = old.to_metadata()
                    patches[nid]["revision"] = old.revision + 1
                return patches[nid]

            for node in nodes:
                if (node.app_id, node.user_id, node.agent_id) != scope:
                    raise ValueError("new node crosses tenant scope")
                if not node.content.strip() or node.embedding is None:
                    raise ValueError("new version requires content and embedding")
                for old_id in node.supersedes:
                    if old_id not in current and old_id not in seen:
                        raise ValueError(
                            "missing, forward or unvalidated version target"
                        )
                    if old_id in consumed or old_id in linked:
                        raise ValueError(
                            "two operations consume the same current target"
                        )
                    old = current.get(old_id) or new_by_id[old_id]
                    patch = patch_of(old_id)
                    patch.update(
                        is_latest=False,
                        superseded_by=_LIST_SEP.join(
                            [*old.superseded_by, node.node_id]
                        ),
                    )
                    consumed.add(old_id)
                # An older report is inserted behind a newer head that stays current.
                for newer_id in node.superseded_by:
                    if node.is_latest or newer_id not in current:
                        raise ValueError(
                            "historical version requires a validated newer head"
                        )
                    if newer_id in consumed:
                        raise ValueError(
                            "historical version cannot attach to a retired target"
                        )
                    newer = current[newer_id]
                    patch = patch_of(newer_id)
                    existing = patch["supersedes"].split(_LIST_SEP) if patch["supersedes"] else []
                    patch["supersedes"] = _LIST_SEP.join([*existing, node.node_id])
                    linked.add(newer_id)
                seen.add(node.node_id)
            for nid in archives:
                if nid not in current or nid in consumed or nid in linked:
                    raise ValueError("archive requires one validated unconsumed target")
                old = current[nid]
                patch = patch_of(nid)
                patch.update(
                    is_latest=False,
                    custom=json.dumps({**(old.custom or {}), "archived": True}),
                )
                consumed.add(nid)
            # A derived claim supported by a retired fact must stop being current.
            # Remaining evidence becomes fresh for the next explicit digest.
            if consumed:
                for derived in self.get_many(
                    {
                        "app_id": scope[0],
                        "user_id": scope[1],
                        "agent_id": scope[2],
                        "layer": Layer.L3_DERIVED.value,
                        "is_latest": True,
                    },
                    limit=100_000,
                ):
                    evidence = set((derived.custom or {}).get("source_ids", []))
                    stale = evidence.intersection(consumed)
                    if stale and derived.node_id not in consumed:
                        patch = derived.to_metadata()
                        patch.update(
                            is_latest=False,
                            revision=derived.revision + 1,
                            custom=json.dumps(
                                {
                                    **(derived.custom or {}),
                                    "stale_evidence": sorted(stale),
                                }
                            ),
                        )
                        patches[derived.node_id] = patch
                        for fid in evidence - consumed:
                            fact = self.get(fid)
                            if fact and fact.is_latest:
                                patches.setdefault(fid, fact.to_metadata())[
                                    "s2_evidence_count"
                                ] = 0
            for fid in processed_evidence or []:
                fact = current.get(fid)
                if (
                    fact is None
                    or not fact.is_latest
                    or fact.layer not in (Layer.L1_FACT, Layer.L2_PROFILE)
                ):
                    raise ValueError(
                        "processed evidence must be a validated current fact"
                    )
                patches.setdefault(fid, fact.to_metadata())["s2_evidence_count"] = 1
            if not nodes and not archives and not processed_evidence:
                if request_key:
                    # Nothing changes, but the request has completed: record it so a
                    # retry replays it and deferred reviews know it has finished.
                    empty_id = hashlib.sha256(
                        f"request:{request_key}".encode()
                    ).hexdigest()
                    with self._journal:
                        self._journal.execute(
                            "INSERT OR IGNORE INTO plans(id,scope,epoch,payload,done) VALUES (?,?,?,?,1)",
                            (
                                empty_id,
                                scope_key,
                                epoch,
                                json.dumps(
                                    {
                                        "decisions": decisions or [],
                                        "request_key": request_key,
                                        "nodes": [],
                                        "patches": {},
                                    }
                                ),
                            ),
                        )
                        self._journal.execute(
                            "INSERT OR IGNORE INTO requests (request_key, plan_id) VALUES (?, ?)",
                            (request_key, empty_id),
                        )
                return True
            semantic = _plan_semantic(nodes)
            audit = [
                {
                    "event": (
                        "HISTORICAL"
                        if n.superseded_by
                        else ("SUPERSEDE" if n.supersedes else "ADD")
                    ),
                    "node_id": n.node_id,
                    "user_id": n.user_id,
                    "old": None,
                    "new": patches.get(n.node_id, n.to_metadata()),
                }
                for n in nodes
            ]
            audit.extend(
                {
                    "event": "ARCHIVE" if nid in archives else "LINK",
                    "node_id": nid,
                    "user_id": scope[1],
                    "old": current[nid].to_metadata() if nid in current else None,
                    "new": patch,
                }
                for nid, patch in patches.items()
                if nid not in new_ids
            )
            payload = {
                "decisions": decisions or [],
                "request_key": request_key,
                "semantic": semantic,
                "audit": audit,
                "nodes": [
                    {
                        "id": n.node_id,
                        "content": n.content,
                        "embedding": n.embedding,
                        "meta": n.to_metadata(),
                    }
                    for n in nodes
                ],
                "patches": patches,
                # Evidence added to existing schemas: graph edges, projected
                # with the plan so every replica builds the same graph.
                "edges": [list(edge) for edge in edges or []],
            }
            with self._journal:
                self._journal.execute(
                    "INSERT INTO plans(id,scope,epoch,payload) VALUES (?,?,?,?)",
                    (plan_id, scope_key, epoch, json.dumps(payload)),
                )
                self._revive([n.node_id for n in nodes])
                self._append_log("plan", json.dumps(plan_id))
                if request_key:
                    self._journal.execute(
                        "INSERT OR IGNORE INTO requests (request_key, plan_id) VALUES (?, ?)",
                        (request_key, plan_id),
                    )
            self._finish_plan(plan_id, scope_key, epoch, payload)
            return True

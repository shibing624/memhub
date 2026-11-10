# -*- coding: utf-8 -*-
"""Durable evidence intake and the MemHub Core's single writer.

Every submission is stored in ``hub_writes`` before any model work, so the row is
the receipt: clients move on once it exists. One worker processes rows in event
time order (``memory_at``, else arrival), so a cold-start import from several
CLIs reaches the reconciler oldest-first. Model failures are retried with
backoff; a row only ends ``failed`` after ``max_attempts``, and ``retry_failed``
or a resubmission of the same request puts it back in the queue.

``raw_only`` is a mode, not part of the evidence: the same request can be stored
raw first and extracted later (``extract_raw``), reusing its L0 node.

``on_idle`` runs once the queue has been empty for ``idle_seconds`` after it
processed work (and once after start): the Core's System 2 digest goes there, so
nobody has to remember to call ``digest()``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from memhub.errors import IdempotencyConflictError

WriteProcessor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
IdleHook = Callable[[], Awaitable[Any]]

_JOB_COLUMNS = "request_id,status,raw_only,attempts,result,error,created_at,updated_at"


def evidence_fingerprint(payload: dict[str, Any]) -> str:
    """Identity of a write's evidence: what was said, in which session.

    ``memory_at`` and the workspace boundary describe the evidence rather than
    being it: a transcript reread later can stamp another time (a file mtime) or
    resolve the workspace differently (``git init`` since), and the same request
    must still be recognised as the one already stored.
    """
    encoded = json.dumps(
        [payload.get("content", ""), payload.get("messages"), payload.get("session_id", "")],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class HubWriteQueue:
    """Persist source evidence before one worker serially calls MemoryClient."""

    def __init__(
        self,
        path: Path,
        process: WriteProcessor,
        *,
        max_attempts: int = 5,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 1800.0,
        on_idle: IdleHook | None = None,
        idle_seconds: float = 300.0,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS hub_writes ("
            "request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
            "payload TEXT NOT NULL, raw_only INTEGER NOT NULL, status TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, "
            "event_at INTEGER NOT NULL, result TEXT, error TEXT, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS hub_writes_ready "
            "ON hub_writes(status, event_at)"
        )
        self._conn.execute(
            "UPDATE hub_writes SET status='pending' WHERE status='running'"
        )
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Rows stored before the fingerprint covered only the evidence.
            rows = self._conn.execute(
                "SELECT request_id,payload FROM hub_writes"
            ).fetchall()
            self._conn.executemany(
                "UPDATE hub_writes SET fingerprint=? WHERE request_id=?",
                [
                    (evidence_fingerprint(json.loads(r["payload"])), r["request_id"])
                    for r in rows
                ],
            )
            self._conn.execute("PRAGMA user_version=1")
        self._conn.commit()
        self._process = process
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._on_idle = on_idle
        self._idle_seconds = idle_seconds
        self.last_idle_error: str | None = None
        self._wake = asyncio.Event()
        self._closed = False
        # Work left by a previous process (an undigested scope) is handled once.
        self._dirty = on_idle is not None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="memhub-hub-writer")
        self._wake.set()

    def wake(self) -> None:
        """Re-run the idle hook: work arrived outside the queue (replicated facts)."""
        if self._on_idle is not None:
            self._dirty = True
            self._wake.set()

    def submit(
        self, payload: dict[str, Any], *, fingerprint: str | None = None
    ) -> dict[str, Any]:
        """Store a write once; resubmitting requeues a failed row or upgrades raw to full.

        ``fingerprint`` defaults to ``evidence_fingerprint(payload)``; the Core passes
        the one of the evidence as submitted, before sanitizing, so changing
        ``redact_secrets`` does not turn a resend into a conflict. A resend keeps the
        stored payload.
        """
        request_id = str(payload["request_id"])
        raw_only = bool(payload["raw_only"])
        evidence = {k: v for k, v in payload.items() if k != "raw_only"}
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        fingerprint = fingerprint or evidence_fingerprint(payload)
        now = int(time.time())
        row = self._conn.execute(
            "SELECT fingerprint,status,raw_only FROM hub_writes WHERE request_id=?",
            (request_id,),
        ).fetchone()
        with self._conn:
            if row is None:
                self._conn.execute(
                    "INSERT INTO hub_writes(request_id,fingerprint,payload,raw_only,"
                    "status,event_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        request_id,
                        fingerprint,
                        encoded,
                        int(raw_only),
                        "pending",
                        payload.get("memory_at") or now,
                        now,
                        now,
                    ),
                )
            elif row["fingerprint"] != fingerprint:
                raise IdempotencyConflictError(
                    "request_id reused with different Hub evidence"
                )
            elif row["status"] == "failed":
                self._requeue("request_id=?", (request_id,), raw_only=raw_only)
            elif row["raw_only"] and not raw_only:
                self._requeue(
                    "request_id=? AND status='done'", (request_id,), raw_only=False
                )
                self._conn.execute(
                    "UPDATE hub_writes SET raw_only=0 WHERE request_id=?",
                    (request_id,),
                )
        self._wake.set()
        return self.get(request_id)

    def _requeue(self, where: str, params: tuple, *, raw_only: bool) -> int:
        cursor = self._conn.execute(
            "UPDATE hub_writes SET status='pending',attempts=0,next_at=0,error=NULL,"
            f"raw_only=?,updated_at=? WHERE {where}",
            (int(raw_only), int(time.time()), *params),
        )
        return cursor.rowcount

    def extract_raw(self) -> int:
        """Queue extraction for every write that was stored raw-only."""
        with self._conn:
            done = self._requeue("raw_only=1 AND status='done'", (), raw_only=False)
            # Not processed yet: switching the mode is enough. A running raw job
            # sees the change when it finishes (see ``_finish``).
            self._conn.execute("UPDATE hub_writes SET raw_only=0 WHERE raw_only=1")
        self._wake.set()
        return done

    def retry_failed(self) -> int:
        with self._conn:
            count = self._conn.execute(
                "UPDATE hub_writes SET status='pending',attempts=0,next_at=0,"
                "error=NULL,updated_at=? WHERE status='failed'",
                (int(time.time()),),
            ).rowcount
        self._wake.set()
        return count

    def get(self, request_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM hub_writes WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return {
            "request_id": row["request_id"],
            "status": row["status"],
            "raw_only": bool(row["raw_only"]),
            "attempts": row["attempts"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status,COUNT(*) FROM hub_writes GROUP BY status"
        ).fetchall()
        out = {str(status): int(count) for status, count in rows}
        out["raw_only_done"] = self._conn.execute(
            "SELECT COUNT(*) FROM hub_writes WHERE raw_only=1 AND status='done'"
        ).fetchone()[0]
        return out

    def last_failed_error(self) -> str | None:
        row = self._conn.execute(
            "SELECT error FROM hub_writes WHERE status='failed' AND error IS NOT NULL "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return str(row["error"]) if row and row["error"] else None

    def failure_summary(self) -> tuple[int, str | None]:
        """(failed count, last error) from the ``(status, event_at)`` index.

        The hot recall path asks this on every prompt, so it must not aggregate the
        whole table the way ``counts()`` does: both queries are index range scans over
        the (usually empty) ``failed`` rows.
        """
        row = self._conn.execute(
            "SELECT COUNT(*), (SELECT error FROM hub_writes WHERE status='failed' "
            "AND error IS NOT NULL ORDER BY updated_at DESC, rowid DESC LIMIT 1) "
            "FROM hub_writes WHERE status='failed'"
        ).fetchone()
        count = int(row[0]) if row else 0
        error = str(row[1]) if row and row[1] else None
        return count, error

    async def wait(self, request_id: str) -> dict[str, Any]:
        while True:
            job = self.get(request_id)
            if job["status"] in ("done", "failed"):
                return job
            await asyncio.sleep(0.02)

    def _take(self) -> tuple[str, dict[str, Any]] | float | None:
        """The next ready job, else the time the earliest delayed one is due."""
        now = time.time()
        row = self._conn.execute(
            "SELECT request_id,payload,raw_only,event_at FROM hub_writes "
            "WHERE status='pending' AND next_at<=? ORDER BY event_at, rowid LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            due = self._conn.execute(
                "SELECT MIN(next_at) FROM hub_writes WHERE status='pending'"
            ).fetchone()[0]
            return due
        with self._conn:
            self._conn.execute(
                "UPDATE hub_writes SET status='running',updated_at=? WHERE request_id=?",
                (int(now), row["request_id"]),
            )
        payload = json.loads(row["payload"])
        payload["raw_only"] = bool(row["raw_only"])
        # A write that names no time happened when it arrived (MCP / `remember`).
        payload["memory_at"] = payload.get("memory_at") or row["event_at"]
        return str(row["request_id"]), payload

    def _finish_ok(self, request_id: str, *, raw_only: bool, result: dict) -> None:
        with self._conn:
            # Raw evidence stored, but extraction was requested meanwhile: run again.
            self._conn.execute(
                "UPDATE hub_writes SET status=CASE WHEN ?=1 AND raw_only=0 "
                "THEN 'pending' ELSE 'done' END,result=?,error=NULL,updated_at=? "
                "WHERE request_id=?",
                (
                    int(raw_only),
                    json.dumps(result, ensure_ascii=False),
                    int(time.time()),
                    request_id,
                ),
            )

    def _finish_error(self, request_id: str, exc: Exception) -> None:
        attempts = self._conn.execute(
            "SELECT attempts FROM hub_writes WHERE request_id=?", (request_id,)
        ).fetchone()[0] + 1
        permanent = isinstance(exc, IdempotencyConflictError)
        failed = permanent or attempts >= self._max_attempts
        delay = min(self._retry_base * 2 ** (attempts - 1), self._retry_max)
        with self._conn:
            self._conn.execute(
                "UPDATE hub_writes SET status=?,attempts=?,next_at=?,error=?,"
                "updated_at=? WHERE request_id=?",
                (
                    "failed" if failed else "pending",
                    attempts,
                    0 if failed else time.time() + delay,
                    f"{type(exc).__name__}: {exc}",
                    int(time.time()),
                    request_id,
                ),
            )

    async def _idle(self) -> None:
        try:
            await self._on_idle()
        except Exception as exc:
            # The hook's work stays queued in its own store and is retried after
            # the next write; it must never stop the intake worker.
            self.last_idle_error = f"{type(exc).__name__}: {exc}"
        else:
            self.last_idle_error = None

    async def _run(self) -> None:
        while not self._closed:
            item = self._take()
            if not isinstance(item, tuple):
                self._wake.clear()
                idle_due = item is None and self._dirty
                if item is None:
                    timeout = self._idle_seconds if idle_due else None
                else:
                    timeout = max(item - time.time(), 0.01)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except TimeoutError:
                    if idle_due and not self._closed:
                        self._dirty = False
                        await self._idle()
                continue
            request_id, payload = item
            try:
                result = await self._process(payload)
            except Exception as exc:
                self._finish_error(request_id, exc)
            else:
                self._finish_ok(request_id, raw_only=payload["raw_only"], result=result)
            self._dirty = self._on_idle is not None

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is not None:
            await self._task
        self._conn.close()

"""SQLite persistence.

One connection, guarded by a lock, used synchronously from the event loop. Queries here touch a few
hundred rows of text and timestamps and complete in well under a millisecond, so the complexity of
an async driver would buy nothing and add a dependency. If that ever stops being true the fix is a
thread executor, not a rewrite.

History is append-only. Runs are never deleted and never rewritten to match the present — a test
that has since been renamed still has the history it had, and the interface says so rather than
quietly dropping it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL,          -- pytest | check_drift | build_map
    target            TEXT    NOT NULL,          -- nodeid, path, marker expression, or '' for jobs
    label             TEXT    NOT NULL,          -- what to show a human
    status            TEXT    NOT NULL,          -- queued|running|passed|failed|error|cancelled|timeout|crashed
    error_reason      TEXT,                      -- set by testboard's own checks, never by parsing output
    requested_by      TEXT,
    requested_at      TEXT    NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    exit_code         INTEGER,
    pid               INTEGER,
    pid_created_at    REAL,                      -- guards against PID reuse on reconciliation
    timeout_seconds   INTEGER,
    destructive       INTEGER NOT NULL DEFAULT 0,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    artifacts_pruned  INTEGER NOT NULL DEFAULT 0,
    totals_json       TEXT,                      -- parsed JUnit counts
    message           TEXT,
    -- What the working tree was when this ran. A green run against an unknown commit tells you
    -- almost nothing three weeks later, and `dirty` matters most of all: a pass from a tree with
    -- uncommitted edits is not a pass anybody else can reproduce.
    git_sha           TEXT,
    git_branch        TEXT,
    git_subject       TEXT,
    git_author        TEXT,
    git_dirty         INTEGER
);
CREATE INDEX IF NOT EXISTS runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS runs_target   ON runs(kind, target, id DESC);
CREATE INDEX IF NOT EXISTS runs_finished ON runs(finished_at);

-- Every test testboard knows about, and whether a human has accepted it.
--
-- A test arrives one of two ways: an agent generated it here, or somebody merged a pull request
-- and it turned up in the next collection. Both are treated identically — newly seen means
-- **pending**, and pending stays out of "run all" until a person approves it and their name is
-- recorded against that decision. That is the whole promotion mechanism, and keeping it keyed on
-- the nodeid rather than on a marker or a directory is what lets it work for a test that arrived
-- through a merge, which nothing here was involved in creating.
CREATE TABLE IF NOT EXISTS inventory (
    nodeid       TEXT PRIMARY KEY,
    file         TEXT NOT NULL,
    name         TEXT NOT NULL,
    markers      TEXT NOT NULL DEFAULT '[]',
    present      INTEGER NOT NULL DEFAULT 1,     -- 0 once a collect no longer returns it
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',-- pending | approved | rejected
    decided_by   TEXT,
    decided_at   TEXT,
    origin       TEXT,                           -- baseline | generated | merged
    source_id    INTEGER                         -- the recording it came from, when generated
);

-- Per-test outcomes, keyed by nodeid and reported by the plugin at run time. This is what lets a
-- single run of the whole suite update every individual test's last known result, without
-- reconstructing nodeids from JUnit's classname/name pair.
CREATE TABLE IF NOT EXISTS test_results (
    nodeid   TEXT    NOT NULL,
    run_id   INTEGER NOT NULL,
    outcome  TEXT    NOT NULL,
    duration REAL,
    message  TEXT,
    at       TEXT    NOT NULL,
    PRIMARY KEY (nodeid, run_id)
);
CREATE INDEX IF NOT EXISTS test_results_nodeid ON test_results(nodeid, run_id DESC);

-- A recording session: the video a tester made, the transcript of what they said, or both.
--
-- The transcript text lives in this table. It is small, it is what the agent actually reads, and
-- having it here means a generation job needs nothing from the filesystem. The video does NOT:
-- it is tens or hundreds of megabytes, it would bloat every backup and VACUUM of this database,
-- and nothing ever queries its bytes. It goes on disk under .testboard/media/ with its digest
-- recorded here, which is also what makes the artifact-retention story apply to it unchanged.
CREATE TABLE IF NOT EXISTS sources (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    created_by        TEXT,
    video_name        TEXT,
    video_path        TEXT,                -- relative to the state directory
    video_bytes       INTEGER,
    video_sha256      TEXT,
    video_duration    REAL,
    transcript_name   TEXT,
    transcript_text   TEXT,
    transcript_cues   TEXT,                -- JSON: [{start, end, speaker, text}]
    transcript_bytes  INTEGER,
    has_timestamps    INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS sources_created ON sources(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

TERMINAL = ("passed", "failed", "error", "cancelled", "timeout", "crashed")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL so a reader never blocks the writer; busy_timeout so a brief overlap waits instead of
        # raising "database is locked" at whoever happened to click first.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a newer testboard expects to a database an older one created.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a schema
        change is invisible to every database that predates it. Adding the missing columns is the
        whole migration: nothing here is ever renamed or retyped, because a run's history is
        append-only and rewriting it would defeat the point of keeping it.
        """
        wanted = {
            "runs": {
                "git_sha": "TEXT", "git_branch": "TEXT", "git_subject": "TEXT",
                "git_author": "TEXT", "git_dirty": "INTEGER",
            },
            "inventory": {
                "state": "TEXT NOT NULL DEFAULT 'pending'", "decided_by": "TEXT",
                "decided_at": "TEXT", "origin": "TEXT", "source_id": "INTEGER",
            },
            "sources": {"last_generation_run": "INTEGER"},
        }
        added: set[str] = set()
        with self._lock:
            for table, columns in wanted.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not have:
                    continue
                for column, decl in columns.items():
                    if column not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                        added.add(f"{table}.{column}")

            # Backfill, at the one moment it is unambiguous. `state` defaults to 'pending', which
            # is right for a test that appears from now on and wrong for every test that was
            # already in the suite before approval existed as an idea. Immediately after the
            # column is added, every row present is by definition pre-existing — so they are the
            # baseline. Waiting until later would make this impossible to tell.
            if "inventory.state" in added:
                self._conn.execute(
                    "UPDATE inventory SET state = 'approved', origin = 'baseline', "
                    "decided_by = 'the suite as it stood', decided_at = ?",
                    (utcnow(),),
                )
            self._conn.commit()
        if added:
            import logging
            logging.getLogger("testboard.db").info(
                "migrated: added %s", ", ".join(sorted(added)))

    # --- plumbing --------------------------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- meta ------------------------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(row["value"]) if row and row["value"] is not None else default

    # --- runs ------------------------------------------------------------------------------
    def enqueue(self, *, kind: str, target: str, label: str, destructive: bool,
                timeout_seconds: int, requested_by: str) -> int:
        return self.execute(
            "INSERT INTO runs(kind, target, label, status, requested_at, destructive, "
            "timeout_seconds, requested_by) VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)",
            (kind, target, label, utcnow(), int(destructive), timeout_seconds, requested_by),
        )

    def next_queued(self) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM runs WHERE status = 'queued' AND cancel_requested = 0 "
            "ORDER BY id LIMIT 1"
        )

    def active(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM runs WHERE status = 'running' ORDER BY id LIMIT 1")

    def queued(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM runs WHERE status = 'queued' ORDER BY id")

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def already_pending(self, kind: str, target: str) -> sqlite3.Row | None:
        """The same thing already queued or running. Two fast clicks should not mean two runs."""
        return self.one(
            "SELECT * FROM runs WHERE kind = ? AND target = ? AND status IN ('queued', 'running') "
            "ORDER BY id LIMIT 1",
            (kind, target),
        )

    def history(self, limit: int = 100, target: str | None = None) -> list[sqlite3.Row]:
        if target:
            return self.query(
                "SELECT * FROM runs WHERE target = ? ORDER BY id DESC LIMIT ?", (target, limit)
            )
        return self.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_result_by_target(self) -> dict[str, sqlite3.Row]:
        """The most recent finished run for each pytest target, in one pass."""
        rows = self.query(
            "SELECT r.* FROM runs r JOIN (SELECT target, MAX(id) AS id FROM runs "
            "WHERE kind = 'pytest' AND status NOT IN ('queued', 'running') GROUP BY target) m "
            "ON r.id = m.id"
        )
        return {r["target"]: r for r in rows}

    # --- inventory -------------------------------------------------------------------------
    def replace_inventory(self, items: list[dict], *, origin: str = "merged",
                          source_id: int | None = None) -> list[str]:
        """Re-assert what collection just returned. Returns the nodeids that are new.

        Rows are never deleted, only marked absent, so a historical run can still say which file
        its test used to live in.

        **The very first collection is the existing suite, and is approved outright.** Anything
        appearing after that is something a person has not looked at yet — whether an agent wrote
        it here or a merge brought it in — so it lands as pending. Getting this backwards would
        either drown a new installation in approvals nobody asked for, or silently promote every
        future arrival, which is the thing approval exists to prevent.
        """
        now = utcnow()
        with self._lock:
            first_ever = not self._conn.execute(
                "SELECT 1 FROM inventory LIMIT 1").fetchone()
            known = {r["nodeid"] for r in self._conn.execute("SELECT nodeid FROM inventory")}
            new_ids = [it["nodeid"] for it in items if it["nodeid"] not in known]

            self._conn.execute("UPDATE inventory SET present = 0")
            for it in items:
                is_new = it["nodeid"] not in known
                state = "approved" if (first_ever or not is_new) else "pending"
                row_origin = "baseline" if first_ever else (origin if is_new else None)
                self._conn.execute(
                    "INSERT INTO inventory(nodeid, file, name, markers, present, first_seen, "
                    "last_seen, state, decided_by, decided_at, origin, source_id) "
                    "VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(nodeid) DO UPDATE SET file=excluded.file, name=excluded.name, "
                    "markers=excluded.markers, present=1, last_seen=excluded.last_seen",
                    (it["nodeid"], it["file"], it["name"], json.dumps(it["markers"]), now, now,
                     state, "first collection" if first_ever else None,
                     now if first_ever else None, row_origin,
                     source_id if is_new and not first_ever else None),
                )
            self._conn.commit()
        return new_ids

    def decide(self, nodeid: str, state: str, who: str) -> None:
        self.execute(
            "UPDATE inventory SET state = ?, decided_by = ?, decided_at = ? WHERE nodeid = ?",
            (state, who, utcnow(), nodeid),
        )

    def pending_tests(self) -> list[dict]:
        return [t for t in self.inventory() if t["state"] == "pending"]

    def inventory(self, present_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM inventory"
        if present_only:
            sql += " WHERE present = 1"
        sql += " ORDER BY file, name"
        return [
            {**dict(r), "markers": json.loads(r["markers"])}
            for r in self.query(sql)
        ]

    def present_nodeids(self) -> set[str]:
        return {r["nodeid"] for r in self.query("SELECT nodeid FROM inventory WHERE present = 1")}

    # --- per-test outcomes -----------------------------------------------------------------
    def record_results(self, run_id: int, records: list[dict]) -> None:
        """One row per test the run actually reported on.

        A later phase overwrites an earlier one for the same nodeid, so a teardown failure on a
        test that passed its call phase ends up recorded as the teardown failure — which is the
        fact that matters when teardown is what cleans up shared data.
        """
        now = utcnow()
        with self._lock:
            for rec in records:
                self._conn.execute(
                    "INSERT INTO test_results(nodeid, run_id, outcome, duration, message, at) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(nodeid, run_id) DO UPDATE SET outcome=excluded.outcome, "
                    "duration=excluded.duration, message=excluded.message",
                    (rec["nodeid"], run_id, rec["outcome"], rec.get("duration"),
                     (rec.get("message") or "")[:4000], now),
                )
            self._conn.commit()

    def latest_results(self) -> dict[str, sqlite3.Row]:
        rows = self.query(
            "SELECT t.* FROM test_results t JOIN (SELECT nodeid, MAX(run_id) AS run_id "
            "FROM test_results GROUP BY nodeid) m "
            "ON t.nodeid = m.nodeid AND t.run_id = m.run_id"
        )
        return {r["nodeid"]: r for r in rows}

    def results_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM test_results WHERE run_id = ? ORDER BY nodeid", (run_id,)
        )

    # --- recordings and transcripts ---------------------------------------------------------
    def create_source(self, title: str, created_by: str) -> int:
        return self.execute(
            "INSERT INTO sources(title, created_at, created_by) VALUES(?, ?, ?)",
            (title, utcnow(), created_by),
        )

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE sources SET {sets} WHERE id = ?", (*fields.values(), source_id))

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sources WHERE id = ?", (source_id,))

    def sources(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sources ORDER BY id DESC")

    def source_by_video_digest(self, digest: str) -> sqlite3.Row | None:
        """Uploading the same file twice should attach to the same source, not create a twin."""
        return self.one("SELECT * FROM sources WHERE video_sha256 = ?", (digest,))

    def delete_source(self, source_id: int) -> None:
        self.execute("DELETE FROM sources WHERE id = ?", (source_id,))

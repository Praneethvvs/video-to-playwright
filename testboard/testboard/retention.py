"""Prune run artifacts, so the disk never fills.

This is the way an unattended service actually dies. A Playwright trace is tens of megabytes and
`--tracing=retain-on-failure` writes one per failure, so a flaky suite left alone for a few weeks
fills a container volume and every subsequent run fails for a reason that has nothing to do with
the tests.

Two rules make the result predictable rather than merely smaller:

* **Directories are pruned; rows never are.** History is text and timestamps and costs nothing to
  keep forever. The interface shows "trace no longer available" instead of a broken link, which is
  honest, where a missing row would silently rewrite the past.
* **The most recent run of every test survives, whatever its age.** "What happened last time I ran
  this" must not be the thing that quietly disappears.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import Config
from .db import Database

log = logging.getLogger("testboard.retention")


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def _prune(config: Config, database: Database, run_id: int) -> float:
    path = config.runs_dir / f"run-{run_id}"
    if not path.exists():
        database.update_run(run_id, artifacts_pruned=1)
        return 0.0
    # The log itself is small and is the thing people come back for; the artifacts directory is
    # what costs megabytes. Keep the former, drop the latter.
    freed = 0.0
    artifacts = path / "artifacts"
    if artifacts.exists():
        freed = _dir_size_mb(artifacts)
        shutil.rmtree(artifacts, ignore_errors=True)
    database.update_run(run_id, artifacts_pruned=1)
    return freed


def sweep(config: Config, database: Database) -> dict:
    keep_per_test = config.artifacts.retain_runs_per_test
    cap_mb = config.artifacts.total_cap_mb

    finished = database.query(
        "SELECT id, target, status, pinned, artifacts_pruned FROM runs "
        "WHERE status NOT IN ('queued', 'running') ORDER BY id DESC"
    )

    keep: set[int] = set()
    seen_per_target: dict[str, int] = {}
    for row in finished:
        target = row["target"]
        n = seen_per_target.get(target, 0)
        # The first row seen per target is the newest, because the query is id DESC.
        if n < keep_per_test:
            keep.add(row["id"])
        seen_per_target[target] = n + 1
        if row["pinned"]:
            keep.add(row["id"])

    candidates = [r for r in finished
                  if r["id"] not in keep and not r["artifacts_pruned"]]

    freed = 0.0
    pruned = 0
    for row in reversed(candidates):        # oldest first
        freed += _prune(config, database, row["id"])
        pruned += 1

    # Then the global cap, oldest first, still never touching the newest run of a test or a pin.
    if config.runs_dir.exists():
        size = _dir_size_mb(config.runs_dir)
        if size > cap_mb:
            newest_per_target = set()
            seen: set[str] = set()
            for row in finished:
                if row["target"] not in seen:
                    seen.add(row["target"])
                    newest_per_target.add(row["id"])
            for row in reversed(finished):
                if size <= cap_mb:
                    break
                if row["id"] in newest_per_target or row["pinned"] or row["artifacts_pruned"]:
                    continue
                released = _prune(config, database, row["id"])
                freed += released
                size -= released
                pruned += 1

    try:
        usage = shutil.disk_usage(config.state_dir)
        percent = round(100 * usage.used / usage.total, 1)
    except OSError:
        percent = -1.0

    summary = {"pruned": pruned, "freed_mb": round(freed, 1), "disk_used_percent": percent}
    # Logged every pass, including the quiet ones. This line is what you read in six months when
    # the disk filled anyway.
    log.info("retention sweep: pruned %s run(s), freed %s MB, disk %s%% used",
             summary["pruned"], summary["freed_mb"], summary["disk_used_percent"])
    database.set_meta("last_retention_sweep", summary)
    return summary

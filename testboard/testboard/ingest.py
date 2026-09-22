"""Accepting the result of a run that happened somewhere else.

Tests are written on a laptop and executed in the cluster. Without this, the dashboard could only
ever report the runs it started itself, so the Results column said "never run from here" about
tests the pipeline had been running green for a month — technically true and practically useless,
and exactly the sort of half-truth this project keeps trying not to ship.

So a pipeline posts its JUnit XML here when it finishes. The run is recorded as `kind='ci'`, which
keeps it visibly distinct from a run somebody started in the interface: it has no log to stream,
no artifacts to prune and no process that was ever ours, and pretending otherwise would make the
run page lie about all three.

What is deliberately **not** done here:

* **No approval.** An imported run updates results; it never promotes a test. A pipeline is not a
  person, and the whole point of the approval record is that a person made a decision.
* **No inventory changes.** A nodeid in the XML that this repository has never collected is
  recorded against the run and ignored for the test list, because the pipeline may be on a branch
  with tests that do not exist here. Inventing rows from a remote report would put tests on the
  dashboard that nobody can run or find.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .db import Database, utcnow
from .results import Totals, parse

log = logging.getLogger("testboard.ingest")

MAX_XML_BYTES = 32 * 1024 * 1024


@dataclass
class Imported:
    run_id: int
    totals: dict
    recorded: int
    unknown: list[str]


def _nodeid_for(case: dict, known: set[str]) -> str | None:
    """Recover a nodeid from JUnit's classname/name pair.

    pytest writes `classname="e2e.tests.test_x"` and `name="test_y[chromium]"`, so the file path
    has to be rebuilt from a dotted module path — and that guess is only trustworthy when it lands
    on a nodeid this repository actually collected. Anything else is reported as unknown rather
    than recorded against a test that may not be the one that ran.
    """
    name = (case.get("name") or "").strip()
    classname = (case.get("classname") or "").strip()
    if not name:
        return None

    parts = [p for p in classname.split(".") if p]
    # A class-based test contributes a final component that is not the module.
    for split in range(len(parts), 0, -1):
        module = "/".join(parts[:split])
        rest = "::".join(parts[split:] + [name])
        candidate = f"{module}.py::{rest}"
        if candidate in known:
            return candidate

    bare = [k for k in known if k.endswith(f"::{name}")]
    return bare[0] if len(bare) == 1 else None


def junit(database: Database, *, xml: bytes, label: str, requested_by: str,
          git_sha: str | None = None, git_branch: str | None = None,
          source_url: str | None = None, state_dir: Path | None = None) -> Imported:
    """Record an externally produced JUnit report as a run."""
    if len(xml) > MAX_XML_BYTES:
        raise ValueError(f"that report is larger than the {MAX_XML_BYTES // 1024 // 1024} MB limit")

    scratch = (state_dir or Path(".")) / "ingest.xml"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(xml)
    totals: Totals | None = parse(scratch)
    scratch.unlink(missing_ok=True)

    if totals is None:
        raise ValueError("that does not parse as JUnit XML")

    run_id = database.enqueue(
        kind="ci", target="", label=label or "Pipeline run", destructive=False,
        timeout_seconds=0, requested_by=requested_by,
    )
    payload = totals.as_dict()
    payload["imported_from"] = source_url

    database.update_run(
        run_id,
        status=totals.outcome,
        error_reason=None if totals.outcome == "passed" else "test_failure",
        started_at=utcnow(), finished_at=utcnow(),
        exit_code=0 if totals.outcome == "passed" else 1,
        totals_json=__import__("json").dumps(payload),
        git_sha=git_sha, git_branch=git_branch,
        message=(f"Imported from {source_url}" if source_url
                 else "Imported from an external run"),
    )

    known = database.present_nodeids()
    records, unknown = [], []
    for case in totals.cases:
        nodeid = _nodeid_for(case, known)
        if nodeid is None:
            unknown.append(f"{case.get('classname', '')}::{case.get('name', '')}".strip(":"))
            continue
        records.append({
            "nodeid": nodeid,
            "outcome": case["status"],
            "duration": case.get("time"),
            "message": case.get("message", ""),
        })
    if records:
        database.record_results(run_id, records)

    if unknown:
        log.info("imported run %s: %d case(s) did not match a collected test", run_id, len(unknown))

    return Imported(run_id=run_id, totals=payload, recorded=len(records), unknown=unknown[:50])

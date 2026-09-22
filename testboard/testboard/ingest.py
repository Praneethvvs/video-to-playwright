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

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import Database, utcnow
from .results import Totals, parse

log = logging.getLogger("testboard.ingest")

MAX_XML_BYTES = 32 * 1024 * 1024
DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def _has_doctype(xml: bytes) -> bool:
    # Only the prolog matters, and looking at all of a 32 MB document to answer this would be
    # its own small denial of service.
    return bool(DOCTYPE.search(xml[:8192]))


@dataclass
class Imported:
    run_id: int
    totals: dict
    recorded: int
    unknown: list[str]


def _nodeid_for(case: dict, known: set[str]) -> str | None:
    """Recover a nodeid from JUnit's classname/name pair.

    pytest writes `classname="e2e.tests.test_x"` and `name="test_y[chromium]"`, so the file path
    has to be rebuilt from a dotted module path. A reconstruction is only trusted when it lands
    on a nodeid this repository actually collected — and, just as importantly, **only when the
    file it lands on is the file the report named.**

    That second condition is not fussiness. An earlier version fell back to matching on the test
    function name alone, which threw away the one field saying where the test lives. It fired
    exactly when the pipeline's layout did not match this checkout — the case the caller most
    needs told about — and recorded a remote pass against whatever local test happened to share
    the function name. `e2e/test_billing.py::test_totals` went green because
    `suite/smoke/test_checkout.py::test_totals` passed somewhere else, silently, with nothing in
    the unmatched list. A false green attributed to a test that never ran is the worst output
    this module could produce.

    A `name` carrying `::` could also address a class-nested nodeid directly, letting a
    hand-shaped report mark a chosen test as passing. The tail must now equal `name` exactly.
    """
    name = (case.get("name") or "").strip()
    classname = (case.get("classname") or "").strip()
    if not name or not classname:
        # Without a classname there is nothing to corroborate a guess against, and guessing is
        # the failure mode being avoided.
        return None

    parts = [p for p in classname.split(".") if p]
    if not parts:
        return None

    # Longest module prefix first: a class-based test contributes trailing components that are
    # class names rather than directories.
    for split in range(len(parts), 0, -1):
        module = "/".join(parts[:split])
        rest = "::".join(parts[split:] + [name])
        candidate = f"{module}.py::{rest}"
        if candidate in known:
            return candidate

    # The report's layout does not match this checkout. One narrow rescue: the same file under a
    # different root — "e2e.tests.test_x" reported where "tests/e2e/tests/test_x.py" was
    # collected. Both the file name and the whole post-file portion have to agree, and the match
    # has to be unique.
    stem = parts[-1]
    tail = name
    rescued = [
        k for k in known
        if k.split("::", 1)[0].rsplit("/", 1)[-1] == f"{stem}.py"
        and k.split("::", 1)[1:] == [tail]
    ]
    return rescued[0] if len(rescued) == 1 else None


def junit(database: Database, *, xml: bytes, label: str, requested_by: str,
          git_sha: str | None = None, git_branch: str | None = None,
          source_url: str | None = None, state_dir: Path | None = None) -> Imported:
    """Record an externally produced JUnit report as a run."""
    if len(xml) > MAX_XML_BYTES:
        raise ValueError(f"that report is larger than the {MAX_XML_BYTES // 1024 // 1024} MB limit")

    if _has_doctype(xml):
        # xml.etree expands entities, so a few hundred bytes of nested definitions become
        # gigabytes in memory and take the whole dashboard down with them. A JUnit report has no
        # legitimate reason to carry a DOCTYPE, so refusing is both safe and sufficient — and
        # cheaper than adding a dependency to parse it defensively.
        raise ValueError("that report declares a DOCTYPE, which a JUnit report has no need of")

    scratch = (state_dir or Path(".")) / f"ingest-{uuid.uuid4().hex}.xml"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        scratch.write_bytes(xml)
        totals: Totals | None = parse(scratch)
    finally:
        # A fixed filename shared by two concurrent posts is one report parsing the other's
        # bytes; and without the finally it survived every exception path.
        scratch.unlink(missing_ok=True)

    if totals is None:
        raise ValueError("that does not parse as JUnit XML")
    if not totals.cases and not totals.tests:
        # Well-formed XML that is not a test report at all. Recording it as a run would put a
        # meaningless red entry in the history and describe a parse problem as a test failure.
        raise ValueError(
            "that parses as XML but contains no test cases, so it is not a JUnit report"
        )

    payload = totals.as_dict()
    payload["imported_from"] = source_url

    run_id = database.record_finished_run(
        kind="ci", label=label or "Pipeline run", requested_by=requested_by,
        status=totals.outcome,
        error_reason=None if totals.outcome == "passed" else "test_failure",
        started_at=utcnow(), finished_at=utcnow(),
        exit_code=0 if totals.outcome == "passed" else 1,
        totals_json=json.dumps(payload),
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

"""A pytest plugin that reports collection and per-test outcomes as JSON.

Loaded with `-p` from the repo's state directory. It does two jobs:

* **Collection** — every nodeid with its exact markers, in one run. The alternative, running
  `--collect-only -m <marker>` once per marker and diffing, is several extra interpreter starts and
  gets the answer wrong for any marker applied dynamically.

* **Outcomes, keyed by nodeid.** JUnit XML identifies a case by `classname` and `name`, so mapping
  a result back to a nodeid means reconstructing `e2e.tests.test_x` into `e2e/tests/test_x.py` and
  hoping no package layout breaks the guess. The nodeid is right here at report time, so it is
  recorded directly. This is what lets a run of the whole suite update every individual test's last
  known result.

Executed by the *repository's* interpreter, not testboard's, so it stays within the standard
library and assumes nothing about which pytest the repo pins.
"""

import json
import os


def _relative(config, item):
    rootdir = str(getattr(config, "rootpath", "") or "")
    path = str(getattr(item, "path", "") or getattr(item, "fspath", "") or "")
    if rootdir and path.startswith(rootdir):
        path = path[len(rootdir):].lstrip("\\/")
    return path.replace("\\", "/")


def pytest_collection_modifyitems(session, config, items):
    out = os.environ.get("TESTBOARD_COLLECT_OUT")
    if not out:
        return
    data = [
        {
            "nodeid": item.nodeid,
            "file": _relative(config, item),
            "name": item.name,
            "markers": sorted({m.name for m in item.iter_markers()}),
        }
        for item in items
    ]
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        # Collection succeeding matters more than reporting it. A missing file is treated by the
        # caller as a failed collect, never as an empty suite.
        pass


def pytest_runtest_logreport(report):
    out = os.environ.get("TESTBOARD_REPORT_OUT")
    if not out:
        return

    # One line per phase that decided something: a setup error and a call failure are different
    # facts, and collapsing them loses which one happened.
    if report.when == "call":
        outcome = report.outcome            # passed | failed | skipped
    elif report.when == "setup" and report.outcome in ("failed", "skipped"):
        outcome = "error" if report.outcome == "failed" else "skipped"
    elif report.when == "teardown" and report.outcome == "failed":
        outcome = "teardown-failed"
    else:
        return

    record = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": outcome,
        "duration": round(getattr(report, "duration", 0.0) or 0.0, 3),
        "message": str(getattr(report, "longrepr", "") or "")[:4000],
    }
    try:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass

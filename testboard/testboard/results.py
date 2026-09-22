"""Read what a run actually did, from its JUnit XML.

A crashed or killed run leaves this file missing or half-written. That is expected, not a second
error to report: the run already has a status explaining itself, and inventing "0 passed" from an
absent file would be the same lie as rendering a failed collection as an empty suite.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("testboard.results")


@dataclass
class Totals:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    cases: list[dict] = field(default_factory=list)
    cleanup_warning: str | None = None

    @property
    def passed(self) -> int:
        return max(self.tests - self.failures - self.errors - self.skipped, 0)

    @property
    def outcome(self) -> str:
        if self.errors:
            return "error"
        if self.failures:
            return "failed"
        if self.tests == 0:
            return "error"
        return "passed"

    def as_dict(self) -> dict:
        return {
            "tests": self.tests, "passed": self.passed, "failures": self.failures,
            "errors": self.errors, "skipped": self.skipped,
            "duration": round(self.duration, 2), "cases": self.cases,
            "cleanup_warning": self.cleanup_warning,
        }


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse(path: Path) -> Totals | None:
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        log.info("unreadable JUnit XML at %s: %s", path, exc)
        return None

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = Totals()
    for suite in suites:
        # Tolerant of a malformed attribute. int("") and int("N/A") raise, and an uncaught
        # ValueError made "one attribute is odd" indistinguishable from "this file is not a
        # report at all" — so the caller reported a parse failure for something almost entirely
        # readable.
        totals.tests += _int(suite.get("tests"))
        totals.failures += _int(suite.get("failures"))
        totals.errors += _int(suite.get("errors"))
        totals.skipped += _int(suite.get("skipped"))
        totals.duration += _float(suite.get("time"))

        for case in suite.iter("testcase"):
            status, message = "passed", ""
            for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
                node = case.find(tag)
                if node is not None:
                    status = label
                    message = (node.get("message") or node.text or "").strip()
                    break
            totals.cases.append({
                "name": case.get("name", ""),
                "classname": case.get("classname", ""),
                "time": _float(case.get("time")),
                "status": status,
                "message": message[:2000],
            })
    return totals


# Teardown that swallows its own exceptions is the dangerous case: the test passes, the data is
# left mutated, and the two look identical in any report that only reads pass/fail. The repo's
# cleanup fixture prints rather than raises, so the evidence is in the log, not the XML.
CLEANUP_SIGNALS = (
    "could not clean up",
    "cleanup failed",
    "warning: cleanup",
    "failed to delete",
)


def detect_cleanup_warning(log_text: str) -> str | None:
    for line in log_text.splitlines():
        lowered = line.lower()
        if any(signal in lowered for signal in CLEANUP_SIGNALS):
            return line.strip()[:300]
    return None

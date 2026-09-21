"""Reading the project map and the last drift result for display.

The one thing this module exists to prevent: rendering "no data" as "no problem". A drift check
that could not reach the application reports zero breaking changes, and so does a clean one. Only
the `outcome` field tells them apart, so every accessor here returns an explicit state rather than
a count that a template might render as a reassuring zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import MAP_SCHEMA_MAX, MAP_SCHEMA_MIN
from .config import Config

UNKNOWN_OUTCOMES = {"unreachable", "unsettled", "no-map"}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_days(value: str | None) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    delta = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    return max(delta, 0.0)          # clock skew should read as "just now", never as negative


@dataclass
class MapInfo:
    present: bool
    captured_at: str | None = None
    age: float | None = None
    freshness: str = "unknown"      # fresh | warn | stale | unknown | missing
    schema_version: int | None = None
    schema_ok: bool = True
    schema_note: str = ""
    base_url: str | None = None
    route_count: int = 0
    unreachable_at_capture: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.unreachable_at_capture is None:
            self.unreachable_at_capture = []


def map_info(config: Config) -> MapInfo:
    path = config.repo_root / config.drift.map
    if not path.exists():
        return MapInfo(present=False, freshness="missing",
                       schema_note=f"no {config.drift.map} in this repository")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return MapInfo(present=False, freshness="missing", schema_ok=False,
                       schema_note=f"{config.drift.map} could not be read: {exc}")

    version = data.get("schema_version")
    schema_ok, note = True, ""
    if not isinstance(version, int):
        schema_ok, note = False, "the map has no schema_version; recapture it"
    elif version > MAP_SCHEMA_MAX:
        # Warn, do not refuse the whole app: the test list and the Run button do not read the map.
        schema_ok = False
        note = (f"the map is schema v{version}; this testboard reads up to v{MAP_SCHEMA_MAX}. "
                f"Drift results may be incomplete — upgrade testboard.")
    elif version < MAP_SCHEMA_MIN:
        schema_ok = False
        note = (f"the map is schema v{version}, older than the minimum v{MAP_SCHEMA_MIN}. "
                f"Recapture it with build_project_map.py before trusting a drift report.")

    captured = data.get("captured_at")
    age = age_days(captured)
    if age is None:
        freshness = "unknown"
    elif age >= config.drift.stale_after_days:
        freshness = "stale"
    elif age >= config.drift.warn_after_days:
        freshness = "warn"
    else:
        freshness = "fresh"

    routes = data.get("routes", [])
    return MapInfo(
        present=True, captured_at=captured, age=age, freshness=freshness,
        schema_version=version if isinstance(version, int) else None,
        schema_ok=schema_ok, schema_note=note,
        base_url=data.get("base_url"), route_count=len(routes),
        unreachable_at_capture=[r.get("route") for r in routes if not r.get("reachable")],
    )


@dataclass
class DriftInfo:
    known: bool                     # False means nothing has been compared, not "nothing changed"
    outcome: str = "never-run"
    checked_at: str | None = None
    age: float | None = None
    breaking: list[dict] = None     # type: ignore[assignment]
    benign: list[dict] = None       # type: ignore[assignment]
    stale_test_files: list[str] = None  # type: ignore[assignment]
    baseline_url: str | None = None
    checked_url: str | None = None
    routes_unreachable: list[dict] = None  # type: ignore[assignment]
    routes_unsettled: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        for field_name in ("breaking", "benign", "stale_test_files", "routes_unreachable",
                           "routes_unsettled"):
            if getattr(self, field_name) is None:
                setattr(self, field_name, [])

    @property
    def headline(self) -> str:
        if not self.known:
            return "not checked yet"
        if self.outcome in UNKNOWN_OUTCOMES:
            return "unknown — nothing was compared"
        if self.breaking:
            return f"{len(self.breaking)} breaking"
        if self.benign:
            return f"clean, {len(self.benign)} benign change(s)"
        return "clean"

    @property
    def tone(self) -> str:
        if not self.known or self.outcome in UNKNOWN_OUTCOMES:
            return "unknown"
        return "bad" if self.breaking else "good"


def drift_info(config: Config) -> DriftInfo:
    path = config.state_dir / "drift-status.json"
    if not path.exists():
        return DriftInfo(known=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DriftInfo(known=False)

    outcome = data.get("outcome", "never-run")
    return DriftInfo(
        known=True,
        outcome=outcome,
        checked_at=data.get("checked_at"),
        age=age_days(data.get("checked_at")),
        breaking=data.get("breaking", []),
        benign=data.get("benign", []),
        stale_test_files=data.get("stale_test_files", []),
        baseline_url=data.get("baseline_url"),
        checked_url=data.get("checked_url"),
        routes_unreachable=data.get("routes_unreachable", []),
        routes_unsettled=data.get("routes_unsettled", []),
    )

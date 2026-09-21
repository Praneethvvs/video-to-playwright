"""Load and validate `testboard.yaml`.

Everything repo-specific lives in that file. If making a repository work would mean editing code in
this package, the fact belongs here instead — that rule is what keeps an upgrade a version bump
rather than a merge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import CONFIG_SCHEMA_MAX, CONFIG_SCHEMA_MIN

CONFIG_NAME = "testboard.yaml"


class ConfigError(Exception):
    """Raised for anything that should stop the server starting."""


@dataclass(frozen=True)
class Markers:
    destructive: str = "writes"
    quarantine: str = "quarantine"


@dataclass(frozen=True)
class Timeouts:
    default_seconds: int = 900
    by_marker: dict[str, int] = field(default_factory=dict)

    def for_markers(self, markers: list[str]) -> int:
        """The most generous timeout any of this test's markers asks for."""
        candidates = [self.by_marker[m] for m in markers if m in self.by_marker]
        return max(candidates) if candidates else self.default_seconds


@dataclass(frozen=True)
class Runner:
    language: str
    framework: str
    python: str
    test_paths: list[str]
    extra_args: list[str]
    markers: Markers
    timeouts: Timeouts


@dataclass(frozen=True)
class Environment:
    base_url_env: str
    passthrough_env: list[str]

    def base_url(self) -> str | None:
        return os.environ.get(self.base_url_env)

    def subprocess_env(self) -> dict[str, str]:
        """Only the variables the repo declared, plus the UTF-8 forcing this needs on Windows.

        `text=True` on a subprocess pipe decodes with locale.getpreferredencoding(), which is cp1252
        on a default Windows install. That has already produced mojibake in this project once, so
        the child is pinned to UTF-8 and the parent decodes explicitly.
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
        return env


@dataclass(frozen=True)
class Drift:
    script: str
    config: str
    map: str
    warn_after_days: int
    stale_after_days: int


@dataclass(frozen=True)
class Artifacts:
    retain_runs_per_test: int
    retain_failed_traces_per_test: int
    total_cap_mb: int
    disk_floor_mb: int


@dataclass(frozen=True)
class Safety:
    confirm_before_run: list[str]
    never_batch: list[str]


@dataclass(frozen=True)
class Config:
    repo_root: Path
    repo_name: str
    runner: Runner
    environment: Environment
    drift: Drift
    artifacts: Artifacts
    safety: Safety
    source_path: Path

    # --- derived paths, all under the gitignored state directory --------------------------------
    @property
    def state_dir(self) -> Path:
        return self.repo_root / ".testboard"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "testboard.db"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "testboard.log"

    def repo_python(self) -> Path:
        """The interpreter that runs the tests — the repo's, never testboard's own.

        Accepts either an interpreter path or a virtualenv root, so one committed `testboard.yaml`
        works on a Windows laptop and a Linux container without a per-platform edit.
        """
        candidate = (self.repo_root / self.runner.python).resolve()
        if candidate.is_file():
            return candidate
        for relative in ("Scripts/python.exe", "bin/python", "bin/python3"):
            resolved = candidate / relative
            if resolved.is_file():
                return resolved
        return candidate  # reported as missing by whoever tries to use it


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{CONFIG_NAME}: missing required key '{key}' under {where}")
    return d[key]


def load(repo_root: Path) -> Config:
    path = repo_root / CONFIG_NAME
    if not path.exists():
        raise ConfigError(
            f"no {CONFIG_NAME} in {repo_root}.\n"
            f"Scaffold one with:  python scripts/install_testboard.py --repo {repo_root}"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_NAME} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must be a mapping at the top level")

    # Schema first, and fatally. This file is trusted for environment variable names and for which
    # tests are destructive; a shape we do not recognise is not something to guess at.
    version = raw.get("schema_version")
    if version is None:
        raise ConfigError(f"{CONFIG_NAME}: missing 'schema_version'")
    if not isinstance(version, int):
        raise ConfigError(f"{CONFIG_NAME}: 'schema_version' must be an integer, got {version!r}")
    if version > CONFIG_SCHEMA_MAX:
        raise ConfigError(
            f"{CONFIG_NAME} is schema v{version}; this testboard understands up to "
            f"v{CONFIG_SCHEMA_MAX}. Upgrade testboard:\n"
            f"  python scripts/install_testboard.py --repo {repo_root} --upgrade"
        )
    if version < CONFIG_SCHEMA_MIN:
        raise ConfigError(
            f"{CONFIG_NAME} is schema v{version}; this testboard needs at least "
            f"v{CONFIG_SCHEMA_MIN}. Migrate it:\n"
            f"  python scripts/install_testboard.py --repo {repo_root} --migrate-config"
        )

    r = _require(raw, "runner", "top level")
    language = r.get("language", "python")
    framework = r.get("framework", "pytest")
    if (language, framework) != ("python", "pytest"):
        # Refuse rather than half-work. The runner and the inventory collector are the only
        # language-specific parts, so another adapter is additive — but it does not exist yet, and
        # pretending otherwise is how the skill's own TypeScript claim went stale.
        raise ConfigError(
            f"{CONFIG_NAME}: runner is {language}/{framework}; this testboard supports python/pytest "
            f"only. A second adapter is a code change, not a configuration one."
        )

    timeouts_raw = r.get("timeouts", {}) or {}
    default_timeout = int(timeouts_raw.get("default_seconds", 900))
    by_marker = {
        k[: -len("_seconds")]: int(v)
        for k, v in timeouts_raw.items()
        if k.endswith("_seconds") and k != "default_seconds"
    }

    markers_raw = r.get("markers", {}) or {}
    env_raw = _require(raw, "environment", "top level")
    drift_raw = raw.get("drift", {}) or {}
    age_raw = drift_raw.get("map_age", {}) or {}
    art_raw = raw.get("artifacts", {}) or {}
    safety_raw = raw.get("safety", {}) or {}

    return Config(
        repo_root=repo_root.resolve(),
        repo_name=(raw.get("repo", {}) or {}).get("name", repo_root.name),
        runner=Runner(
            language=language,
            framework=framework,
            python=str(_require(r, "python", "runner")),
            test_paths=list(r.get("test_paths") or ["tests"]),
            extra_args=[str(a) for a in (r.get("extra_args") or [])],
            markers=Markers(
                destructive=markers_raw.get("destructive", "writes"),
                quarantine=markers_raw.get("quarantine", "quarantine"),
            ),
            timeouts=Timeouts(default_seconds=default_timeout, by_marker=by_marker),
        ),
        environment=Environment(
            base_url_env=str(_require(env_raw, "base_url_env", "environment")),
            passthrough_env=[str(v) for v in (env_raw.get("passthrough_env") or [])],
        ),
        drift=Drift(
            script=drift_raw.get("script", "scripts/check_drift.py"),
            config=drift_raw.get("config", "drift-config.json"),
            map=drift_raw.get("map", "project-map.json"),
            warn_after_days=int(age_raw.get("warn_after_days", 14)),
            stale_after_days=int(age_raw.get("stale_after_days", 30)),
        ),
        artifacts=Artifacts(
            retain_runs_per_test=int(art_raw.get("retain_runs_per_test", 20)),
            retain_failed_traces_per_test=int(art_raw.get("retain_failed_traces_per_test", 10)),
            total_cap_mb=int(art_raw.get("total_cap_mb", 5000)),
            disk_floor_mb=int(art_raw.get("disk_floor_mb", 500)),
        ),
        safety=Safety(
            confirm_before_run=[str(m) for m in (safety_raw.get("confirm_before_run") or [])],
            never_batch=[str(m) for m in (safety_raw.get("never_batch") or [])],
        ),
        source_path=path,
    )

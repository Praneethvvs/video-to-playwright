"""The artifact sweep.

This is the part that decides whether the service is still alive in six months. It had no tests,
which for the one subsystem whose job is deleting things is the wrong way round: every property
here is about what it must *not* delete, and a sweep that quietly takes one too many is invisible
until somebody goes looking for a trace that should have been there.
"""

import textwrap

import pytest

from testboard import retention
from testboard.config import load
from testboard.db import Database

CONFIG = textwrap.dedent("""
    schema_version: 1
    repo:
      name: fixture
    runner:
      language: python
      framework: pytest
      python: venv
      test_paths: [tests]
    environment:
      base_url_env: FIXTURE_BASE_URL
    artifacts:
      retain_runs_per_test: {keep}
      retain_failed_traces_per_test: 2
      total_cap_mb: {cap}
      disk_floor_mb: 500
""")


@pytest.fixture()
def make(tmp_path):
    """Build an installation. The config dataclasses are frozen, so the knobs go in the YAML."""
    opened = []

    def build(keep=2, cap=5000):
        (tmp_path / "testboard.yaml").write_text(
            CONFIG.format(keep=keep, cap=cap), encoding="utf-8")
        (tmp_path / "tests").mkdir(exist_ok=True)
        config = load(tmp_path)
        config.runs_dir.mkdir(parents=True, exist_ok=True)
        database = Database(config.db_path)
        opened.append(database)
        return config, database

    yield build
    for database in opened:
        database.close()


@pytest.fixture()
def setup(make):
    return make()


def finished_run(config, database, target, *, status="passed", pinned=False, kb=64):
    run_id = database.enqueue(kind="pytest", target=target, label=target, destructive=False,
                              timeout_seconds=60, requested_by="local")
    database.update_run(run_id, status=status, pinned=int(pinned))
    run_dir = config.runs_dir / f"run-{run_id}"
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts" / "trace.zip").write_bytes(b"x" * (kb * 1024))
    (run_dir / "stdout.log").write_text("log\n", encoding="utf-8")
    return run_id


def artifacts_exist(config, run_id):
    return (config.runs_dir / f"run-{run_id}" / "artifacts").exists()


def test_the_newest_runs_per_test_are_kept(setup):
    config, db = setup
    ids = [finished_run(config, db, "t.py::test_a") for _ in range(5)]
    retention.sweep(config, db)
    assert [artifacts_exist(config, i) for i in ids] == [False, False, False, True, True]


def test_each_test_is_counted_separately(setup):
    """Two tests with two runs each must both survive a `retain_runs_per_test` of 2."""
    config, db = setup
    a = [finished_run(config, db, "t.py::test_a") for _ in range(2)]
    b = [finished_run(config, db, "t.py::test_b") for _ in range(2)]
    retention.sweep(config, db)
    assert all(artifacts_exist(config, i) for i in a + b)


def test_a_pinned_run_is_never_pruned(setup):
    config, db = setup
    pinned = finished_run(config, db, "t.py::test_a", pinned=True)
    for _ in range(4):
        finished_run(config, db, "t.py::test_a")
    retention.sweep(config, db)
    assert artifacts_exist(config, pinned)


def test_the_global_cap_still_keeps_the_newest_run_of_every_test(make):
    """The cap binds before the per-test rule is satisfied, which is where this gets dangerous.

    "Free space until you are under the cap" applied naively takes the only surviving trace of a
    test with it — and the newest run of a test is the one somebody is about to look at.
    """
    config, db = make(keep=1, cap=0.1)          # anything at all is over the cap
    newest = {}
    for target in ("t.py::test_a", "t.py::test_b", "t.py::test_c"):
        for _ in range(3):
            newest[target] = finished_run(config, db, target, kb=256)
    retention.sweep(config, db)
    for target, run_id in newest.items():
        assert artifacts_exist(config, run_id), f"the cap took the newest run of {target}"


def test_a_pin_survives_the_global_cap_too(make):
    config, db = make(keep=1, cap=0.1)
    pinned = finished_run(config, db, "t.py::test_a", pinned=True, kb=256)
    for _ in range(4):
        finished_run(config, db, "t.py::test_a", kb=256)
    retention.sweep(config, db)
    assert artifacts_exist(config, pinned)


def test_a_running_run_is_never_touched(setup):
    """Pruning underneath a live run would delete the trace it is in the middle of writing."""
    config, db = setup
    for _ in range(4):
        finished_run(config, db, "t.py::test_a")
    live = db.enqueue(kind="pytest", target="t.py::test_a", label="live", destructive=False,
                      timeout_seconds=60, requested_by="local")
    db.update_run(live, status="running")
    (config.runs_dir / f"run-{live}" / "artifacts").mkdir(parents=True, exist_ok=True)
    retention.sweep(config, db)
    assert artifacts_exist(config, live)


def test_rows_are_never_deleted_only_directories(setup):
    """History is text and timestamps and stays forever; only the bytes go."""
    config, db = setup
    ids = [finished_run(config, db, "t.py::test_a") for _ in range(5)]
    retention.sweep(config, db)
    assert {r["id"] for r in db.history(limit=100)} >= set(ids)
    pruned = db.get_run(ids[0])
    assert pruned["artifacts_pruned"] == 1, "a pruned run must say so rather than look untouched"


def test_a_second_sweep_does_not_re_report_the_same_bytes(setup):
    """`artifacts_pruned` is what stops a quiet sweep claiming it freed the same MB again."""
    config, db = setup
    for _ in range(5):
        finished_run(config, db, "t.py::test_a")
    first = retention.sweep(config, db)
    second = retention.sweep(config, db)
    assert first["pruned"] == 3
    assert second["pruned"] == 0
    assert second["freed_mb"] == 0


def test_the_summary_is_recorded_for_the_overview(setup):
    config, db = setup
    finished_run(config, db, "t.py::test_a")
    summary = retention.sweep(config, db)
    assert db.get_meta("last_retention_sweep") == summary
    assert summary["disk_used_percent"] >= 0


def test_an_empty_installation_sweeps_without_complaint(setup):
    config, db = setup
    assert retention.sweep(config, db)["pruned"] == 0

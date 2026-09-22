"""The approval gate.

The property under test: **a test only enters the suite because a person said so.** It was
possible to bypass this entirely on the repository testboard is for — one with no tests yet —
because "the inventory table is empty" was standing in for "no baseline has been taken".
"""

import pytest

from testboard.db import Database

GENERATED = [
    {"nodeid": "e2e/t.py::test_a[chromium]", "file": "e2e/t.py",
     "name": "test_a[chromium]", "markers": ["smoke"]},
    {"nodeid": "e2e/t.py::test_b[chromium]", "file": "e2e/t.py",
     "name": "test_b[chromium]", "markers": []},
]


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


def states(database):
    return {r["name"]: r["state"] for r in database.inventory(present_only=False)}


def test_the_first_real_collection_is_the_baseline(db):
    db.replace_inventory([dict(GENERATED[0])])
    row = db.inventory()[0]
    assert row["state"] == "approved"
    assert row["origin"] == "baseline"


def test_a_later_arrival_is_pending(db):
    db.replace_inventory([dict(GENERATED[0])])
    db.replace_inventory(GENERATED, origin="merged")
    assert states(db) == {"test_a[chromium]": "approved", "test_b[chromium]": "pending"}


def test_an_empty_repo_then_generation_does_not_approve(db):
    """The reported bypass.

    An empty repo collects nothing (pytest exit 5) so the table stays empty. The next refresh is
    the one fired after a generation run — and it must not conclude it is the first collection.
    """
    db.replace_inventory([])
    new = db.replace_inventory(GENERATED, origin="generated", source_id=7)
    assert len(new) == 2
    assert set(states(db).values()) == {"pending"}
    assert {r["source_id"] for r in db.inventory()} == {7}


def test_generation_can_never_establish_the_baseline(db):
    db.replace_inventory(GENERATED, origin="generated", source_id=3)
    assert not db.get_meta("baseline_established")
    assert set(states(db).values()) == {"pending"}


def test_an_empty_collection_does_not_establish_the_baseline(db):
    db.replace_inventory([])
    assert not db.get_meta("baseline_established")
    db.replace_inventory([dict(GENERATED[0])], origin="merged")
    # Still the genuine first suite, so it is the baseline rather than a pending arrival.
    assert states(db) == {"test_a[chromium]": "approved"}


def test_a_decision_is_recorded_against_a_person(db):
    db.replace_inventory([dict(GENERATED[0])])
    db.replace_inventory(GENERATED, origin="merged")
    db.decide(GENERATED[1]["nodeid"], "approved", "Jane Doe")
    row = [r for r in db.inventory() if r["name"] == "test_b[chromium]"][0]
    assert row["state"] == "approved"
    assert row["decided_by"] == "Jane Doe"
    assert row["decided_at"]


def test_approval_survives_a_re_collection(db):
    db.replace_inventory([dict(GENERATED[0])])
    db.replace_inventory(GENERATED, origin="merged")
    db.decide(GENERATED[1]["nodeid"], "approved", "someone")
    db.replace_inventory(GENERATED, origin="merged")
    assert set(states(db).values()) == {"approved"}


def test_state_counts_ignores_absent_rows(db):
    db.replace_inventory(GENERATED)
    db.replace_inventory([dict(GENERATED[0])])      # one test disappeared from the repo
    assert db.state_counts()["approved"] == 1


def test_a_disappeared_test_keeps_its_history(db):
    """History is append-only; a renamed or deleted test is not retconned out of it."""
    db.replace_inventory(GENERATED)
    db.replace_inventory([dict(GENERATED[0])])
    kept = [r["name"] for r in db.inventory(present_only=False)]
    assert "test_b[chromium]" in kept

"""Importing a run that happened elsewhere.

Two properties matter more than anything else here: an import must never approve a test, and it
must never attribute a pipeline result to the wrong test.
"""

import pytest

from testboard import ingest
from testboard.db import Database

KNOWN = [
    {"nodeid": "e2e/tests/test_dash.py::test_renders[chromium]",
     "file": "e2e/tests/test_dash.py", "name": "test_renders[chromium]", "markers": ["smoke"]},
    {"nodeid": "e2e/tests/test_ledger.py::test_lists[chromium]",
     "file": "e2e/tests/test_ledger.py", "name": "test_lists[chromium]", "markers": []},
]


def junit(cases, failures=0, errors=0, skipped=0):
    body = "".join(
        f'<testcase classname="{c}" name="{n}" time="1.5">{extra}</testcase>'
        for c, n, extra in cases
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="3.0">{body}</testsuite></testsuites>'
    ).encode()


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.replace_inventory(KNOWN)
    yield database
    database.close()


def test_a_matching_report_records_results(db, tmp_path):
    xml = junit([
        ("e2e.tests.test_dash", "test_renders[chromium]", ""),
        ("e2e.tests.test_ledger", "test_lists[chromium]", ""),
    ])
    result = ingest.junit(db, xml=xml, label="pipeline #1", requested_by="ci",
                          state_dir=tmp_path)
    assert result.recorded == 2
    assert result.unknown == []
    latest = db.latest_results()
    assert set(latest) == {k["nodeid"] for k in KNOWN}
    assert all(r["outcome"] == "passed" for r in latest.values())


def test_an_import_never_approves_a_test(db, tmp_path):
    """A pipeline is not a person. Approval exists to record which person decided."""
    db.replace_inventory(KNOWN + [
        {"nodeid": "e2e/tests/test_new.py::test_new[chromium]", "file": "e2e/tests/test_new.py",
         "name": "test_new[chromium]", "markers": []}], origin="merged")
    assert len(db.pending_tests()) == 1

    ingest.junit(db, xml=junit([("e2e.tests.test_new", "test_new[chromium]", "")]),
                 label="pipeline", requested_by="ci", state_dir=tmp_path)

    assert len(db.pending_tests()) == 1, "an import changed a test's state"


def test_an_import_never_adds_inventory_rows(db, tmp_path):
    """The pipeline may be on a branch whose tests do not exist in this checkout."""
    before = {r["nodeid"] for r in db.inventory(present_only=False)}
    result = ingest.junit(
        db, xml=junit([("e2e.tests.test_elsewhere", "test_only_on_a_branch[chromium]", "")]),
        label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert {r["nodeid"] for r in db.inventory(present_only=False)} == before
    assert result.recorded == 0
    assert result.unknown, "an unmatched case must be reported, not silently dropped"


def test_an_unmatched_case_is_reported_not_guessed(db, tmp_path):
    result = ingest.junit(db, xml=junit([("totally.unrelated", "test_nope", "")]),
                          label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 0
    assert len(result.unknown) == 1


def test_an_ambiguous_name_is_not_attributed(db, tmp_path):
    """Two tests sharing a function name must not be resolved by guessing."""
    db.replace_inventory(KNOWN + [
        {"nodeid": "e2e/tests/test_other.py::test_renders[chromium]",
         "file": "e2e/tests/test_other.py", "name": "test_renders[chromium]", "markers": []}],
        origin="merged")
    result = ingest.junit(
        db, xml=junit([("unrecognised.module", "test_renders[chromium]", "")]),
        label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 0, "an ambiguous case was attributed to one of two candidates"


def test_a_foreign_file_is_never_matched_by_function_name(db, tmp_path):
    """The false-green case.

    A report naming a file this checkout has never collected must not be recorded against
    whatever local test happens to share the function name. That fired precisely when the
    pipeline's layout differed — the case the caller most needs told about — and turned "we could
    not match this" into "that test is passing".
    """
    result = ingest.junit(
        db, xml=junit([("suite.smoke.test_checkout", "test_renders[chromium]", "")]),
        label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 0, "a foreign file was matched on function name alone"
    assert result.unknown, "and it was not even reported as unmatched"
    assert db.latest_results() == {}


def test_a_name_containing_a_separator_cannot_address_a_nodeid(db, tmp_path):
    """A hand-shaped report must not be able to nominate the test it marks as passing."""
    db.replace_inventory(KNOWN + [
        {"nodeid": "e2e/tests/test_login.py::TestSession::test_expiry",
         "file": "e2e/tests/test_login.py", "name": "test_expiry", "markers": []}],
        origin="merged")
    result = ingest.junit(
        db, xml=junit([("", "TestSession::test_expiry", "")]),
        label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 0
    assert "TestSession::test_expiry" not in db.latest_results()


def test_an_empty_classname_is_not_guessed_at(db, tmp_path):
    result = ingest.junit(db, xml=junit([("", "test_renders[chromium]", "")]),
                          label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 0


def test_the_same_file_under_a_different_root_is_still_matched(db, tmp_path):
    """The one rescue that is safe: the file agrees, only the prefix differs."""
    result = ingest.junit(
        db, xml=junit([("tests.e2e.tests.test_dash", "test_renders[chromium]", "")]),
        label="pipeline", requested_by="ci", state_dir=tmp_path)
    assert result.recorded == 1
    assert KNOWN[0]["nodeid"] in db.latest_results()


def test_failures_are_carried_through(db, tmp_path):
    xml = junit([("e2e.tests.test_dash", "test_renders[chromium]",
                  '<failure message="boom">trace</failure>')], failures=1)
    result = ingest.junit(db, xml=xml, label="pipeline", requested_by="ci", state_dir=tmp_path)
    run = db.get_run(result.run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "test_failure"
    assert db.latest_results()[KNOWN[0]["nodeid"]]["outcome"] == "failed"


def test_the_run_is_marked_as_imported(db, tmp_path):
    result = ingest.junit(db, xml=junit([("e2e.tests.test_dash", "test_renders[chromium]", "")]),
                          label="pipeline #7", requested_by="ci",
                          git_sha="abc1234", git_branch="main",
                          source_url="https://example/build/7", state_dir=tmp_path)
    run = db.get_run(result.run_id)
    assert run["kind"] == "ci", "an imported run must stay distinguishable from a local one"
    assert run["git_sha"] == "abc1234"
    assert run["git_branch"] == "main"
    assert "example/build/7" in run["message"]


def test_rubbish_is_refused(db, tmp_path):
    with pytest.raises(ValueError):
        ingest.junit(db, xml=b"this is not xml", label="x", requested_by="ci",
                     state_dir=tmp_path)


def test_an_oversized_report_is_refused(db, tmp_path):
    with pytest.raises(ValueError):
        ingest.junit(db, xml=b"x" * (ingest.MAX_XML_BYTES + 1), label="x",
                     requested_by="ci", state_dir=tmp_path)


def test_the_scratch_file_does_not_linger(db, tmp_path):
    ingest.junit(db, xml=junit([("e2e.tests.test_dash", "test_renders[chromium]", "")]),
                 label="x", requested_by="ci", state_dir=tmp_path)
    assert not (tmp_path / "ingest.xml").exists()

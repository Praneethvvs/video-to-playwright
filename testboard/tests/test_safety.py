"""The two gates that stand between a button and something you cannot undo.

A test marked `writes` changes data in an environment other people are using, and the dashboard
is reachable by anyone who can reach the port. So the confirmation is enforced on the server, not
by the dialog: a POST that skips the page has skipped the only thing that asked.

The disk floor is the other one. Playwright writing a trace into a full volume produces a
truncated file and a failure that looks like a test failure, so the run is refused before it
starts rather than after it has made a mess.
"""

import textwrap

import pytest
from starlette.testclient import TestClient

from testboard import auth, preflight
from testboard.app import create_app
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
      markers:
        destructive: writes
        quarantine: quarantine
    environment:
      base_url_env: FIXTURE_BASE_URL
    safety:
      confirm_before_run: [writes]
      never_batch: [writes]
""")

SUITE = [
    {"nodeid": "t.py::test_read[chromium]", "file": "t.py", "name": "test_read[chromium]",
     "markers": ["smoke"]},
    {"nodeid": "t.py::test_write[chromium]", "file": "t.py", "name": "test_write[chromium]",
     "markers": ["regression", "writes"]},
]


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "testboard.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    return tmp_path


@pytest.fixture()
def client(repo):
    config = load(repo)
    database = Database(config.db_path)
    database.replace_inventory(SUITE)           # the first collection: the baseline, approved
    database.close()
    app = create_app(config, auth.Policy("trusted", None, "127.0.0.1"))
    return TestClient(app)


def queued(client):
    return [r for r in client.app.state.db.history(limit=50) if r["kind"] == "pytest"]


def post_run(client, **data):
    return client.post("/run", data=data, headers={"Sec-Fetch-Site": "same-origin"},
                       follow_redirects=False)


# --- the destructive gate ---------------------------------------------------------------------
def test_a_destructive_run_without_confirmation_starts_nothing(client):
    """The dialog is in the page; the rule is on the server.

    Anything that can reach the port can post this form. If the check lived only in JavaScript,
    "are you sure" would be decorative.
    """
    post_run(client, target="t.py::test_write[chromium]", label="write")
    assert queued(client) == []


def test_the_refusal_explains_itself_on_the_page(client):
    """Not a 400 body. Somebody just pressed a button and needs to know what to do next."""
    response = post_run(client, target="t.py::test_write[chromium]", label="write")
    assert response.status_code == 303
    flash = client.app.state.db.get_meta("last_upload")
    assert flash and "writes" in flash[0]
    assert "was not started" in flash[0]


def test_a_confirmed_destructive_run_is_queued_and_marked(client):
    post_run(client, target="t.py::test_write[chromium]", label="write", confirmed="yes")
    rows = queued(client)
    assert len(rows) == 1
    assert rows[0]["destructive"] == 1, "the banner and the history both key off this"


def test_an_ordinary_run_needs_no_confirmation(client):
    post_run(client, target="t.py::test_read[chromium]", label="read")
    rows = queued(client)
    assert len(rows) == 1
    assert rows[0]["destructive"] == 0


def test_run_all_excludes_destructive_tests_rather_than_asking_about_them(client):
    """`never_batch` means the question never has to be asked, because they are not included.

    Narrowing has to happen before the confirmation check, or "run all" is gated on a marker
    belonging to a test it was never going to run.
    """
    post_run(client, target="", label="")
    rows = queued(client)
    assert len(rows) == 1
    assert rows[0]["target"] == "-m not writes"
    assert rows[0]["destructive"] == 0


def test_a_marker_expression_selecting_writes_is_still_gated(client):
    post_run(client, target="-m writes", label="all writes")
    assert queued(client) == []
    post_run(client, target="-m writes", label="all writes", confirmed="yes")
    assert len(queued(client)) == 1


def test_running_a_whole_file_is_gated_by_what_is_in_it(client):
    """The file holds one destructive test, so running the file is a destructive run."""
    post_run(client, target="t.py", label="the file")
    assert queued(client) == []


# --- the disk floor ---------------------------------------------------------------------------
def fake_disk(monkeypatch, free_mb):
    import collections

    usage = collections.namedtuple("usage", "total used free")
    free = free_mb * 1024**2
    monkeypatch.setattr(preflight.shutil, "disk_usage",
                        lambda _p: usage(100 * 1024**3, 100 * 1024**3 - free, free))


def test_a_low_disk_refuses_the_run(repo, monkeypatch):
    """Refused before pytest starts, not after Playwright writes half a trace into a full disk.

    Checked at the `check_disk` seam rather than through `before_run`, because the interpreter
    check runs first and a fixture repo has no virtualenv — which would make this pass or fail
    for a reason that has nothing to do with the disk.
    """
    fake_disk(monkeypatch, free_mb=10)
    check = preflight.check_disk(load(repo))
    assert check.ok is False
    assert check.reason == "disk_low"
    assert "floor" in check.detail and "truncated" in check.detail


def test_a_healthy_disk_does_not_trip_the_floor(repo, monkeypatch):
    """Asserted so the floor cannot be left permanently on by a units mistake.

    500 MB expressed in bytes and compared against a megabyte figure would refuse every run on
    every machine, and the message would be confidently wrong about why.
    """
    fake_disk(monkeypatch, free_mb=50_000)
    assert preflight.check_disk(load(repo)).ok is True


def test_exactly_at_the_floor_is_allowed(repo, monkeypatch):
    fake_disk(monkeypatch, free_mb=500)
    assert preflight.check_disk(load(repo)).ok is True


def test_an_unreadable_disk_does_not_block_runs(repo, monkeypatch):
    """Not knowing the free space is not a reason to refuse to run anything."""
    def boom(_p):
        raise OSError("no")

    monkeypatch.setattr(preflight.shutil, "disk_usage", boom)
    check = preflight.check_disk(load(repo))
    assert check.ok is True
    assert "could not read" in check.detail

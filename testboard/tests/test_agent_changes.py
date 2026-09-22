"""What a generation run did, including the parts the approval gate never sees.

The gate answers one question — may this *new* test join the suite. On the first real generation
run against the trial repository the agent deleted an existing test outright and stripped
assertions out of three more files, narrowing the suite to only what the recording happened to
narrate. The run finished green, one test showed up awaiting approval, and nothing anywhere said
coverage had gone down.

These tests pin the reporting. Deleting is not forbidden — sometimes an assertion pinned to
fixture data really should go — but it has to be visible.
"""

import subprocess
from pathlib import Path

import pytest

from testboard import gitinfo
from testboard.executor import Executor
from testboard.inventory import CollectResult


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "kept.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def collected(*nodeids, ok=True):
    return CollectResult(
        ok=ok,
        items=[{"nodeid": n, "file": n.split("::")[0], "name": n.split("::")[-1], "markers": []}
               for n in nodeids],
        exit_code=0, detail="", reason="collected",
    )


# --- reading the working tree -----------------------------------------------------------------
def test_a_clean_tree_is_empty_not_unknown(repo):
    """The distinction the whole function exists for.

    `_run` returns None for empty output, so the obvious implementation reported a clean tree as
    "could not tell" — and "nothing changed" and "I have no idea" must never render the same.
    """
    assert gitinfo.changed_paths(repo) == []


def test_a_modified_file_is_reported_as_modified(repo):
    (repo / "kept.py").write_text("x = 2\n", encoding="utf-8")
    assert gitinfo.changed_paths(repo) == [gitinfo.Change("modified", "kept.py")]


def test_a_new_file_is_reported_as_untracked(repo):
    (repo / "added.py").write_text("y = 1\n", encoding="utf-8")
    assert gitinfo.changed_paths(repo) == [gitinfo.Change("untracked", "added.py")]


def test_a_deleted_file_is_reported_as_deleted(repo):
    (repo / "kept.py").unlink()
    assert gitinfo.changed_paths(repo) == [gitinfo.Change("deleted", "kept.py")]


def test_a_rename_does_not_produce_a_phantom_entry(repo):
    """`--porcelain -z` splits a rename across two fields.

    Without consuming the second one, the old path is parsed as its own entry and comes back as a
    change with two characters sliced off the front of its name.
    """
    git(repo, "mv", "kept.py", "moved.py")
    changes = gitinfo.changed_paths(repo)
    assert [c.status for c in changes] == ["renamed"]
    assert changes[0].path in ("moved.py", "kept.py")


def test_a_directory_without_git_is_unknown(tmp_path):
    assert gitinfo.changed_paths(tmp_path) is None


def test_a_path_with_a_space_survives(repo):
    (repo / "two words.py").write_text("z = 1\n", encoding="utf-8")
    assert gitinfo.changed_paths(repo) == [gitinfo.Change("untracked", "two words.py")]


# --- what the run reports ---------------------------------------------------------------------
def test_a_removed_test_is_named(repo):
    before = collected("t.py::test_a", "t.py::test_b")
    after = collected("t.py::test_a")
    out = Executor._describe_agent_changes((before, []), after, repo)
    assert out["removed_nodeids"] == ["t.py::test_b"]


def test_a_removal_is_stated_loudly_in_the_log(repo):
    out = Executor._describe_agent_changes(
        (collected("t.py::test_a", "t.py::test_b"), []), collected("t.py::test_a"), repo)
    text = "\n".join(Executor._changes_as_lines(out))
    assert "WARNING" in text
    assert "t.py::test_b" in text


def test_pre_existing_edits_are_not_blamed_on_the_agent(repo):
    """Somebody mid-change when they press Convert is normal, and not the agent's work."""
    (repo / "kept.py").write_text("x = 2\n", encoding="utf-8")
    dirty_before = gitinfo.changed_paths(repo)
    out = Executor._describe_agent_changes(
        (collected("t.py::test_a"), dirty_before), collected("t.py::test_a"), repo)
    assert out["files"] == []
    assert out["tree"] == "known"


def test_an_edit_the_agent_made_is_reported(repo):
    dirty_before = gitinfo.changed_paths(repo)
    (repo / "kept.py").write_text("x = 3\n", encoding="utf-8")
    out = Executor._describe_agent_changes(
        (collected("t.py::test_a"), dirty_before), collected("t.py::test_a"), repo)
    assert out["files"] == [{"status": "modified", "path": "kept.py"}]


def test_no_before_snapshot_is_said_rather_than_guessed(repo):
    (repo / "new.py").write_text("q = 1\n", encoding="utf-8")
    out = Executor._describe_agent_changes((collected("t.py::test_a"), None), collected(), repo)
    assert out["tree"] == "no-baseline"
    assert "may predate" in "\n".join(Executor._changes_as_lines(out))


def test_an_unreadable_tree_is_unknown_not_clean(tmp_path):
    out = Executor._describe_agent_changes((collected(), []), collected(), tmp_path)
    assert out["tree"] == "unknown"
    assert "incomplete" in "\n".join(Executor._changes_as_lines(out))


def test_a_failed_collection_does_not_invent_removals(repo):
    """If the suite could not be collected afterwards, every test looks removed.

    Reporting that would be worse than reporting nothing: a collection error already has its own
    banner, and "42 tests were deleted" on top of it is noise that hides the real problem.
    """
    before = collected("t.py::test_a", "t.py::test_b")
    after = CollectResult(ok=False, items=[], exit_code=2, detail="ImportError",
                          reason="collection-error")
    out = Executor._describe_agent_changes((before, []), after, repo)
    assert out["removed_nodeids"] == []


def test_nothing_at_all_produces_no_lines(repo):
    out = Executor._describe_agent_changes((collected("t.py::test_a"), []),
                                           collected("t.py::test_a"), repo)
    assert Executor._changes_as_lines(out) == []


# --- and on the screen ------------------------------------------------------------------------
RENDER_CONFIG = """
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
"""


@pytest.fixture()
def client(tmp_path):
    from starlette.testclient import TestClient

    from testboard import auth
    from testboard.app import create_app
    from testboard.config import load

    (tmp_path / "testboard.yaml").write_text(RENDER_CONFIG, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    app = create_app(load(tmp_path), auth.Policy("trusted", None, "127.0.0.1"))
    return TestClient(app)


def _run_with(client, changes):
    import json as _json

    db = client.app.state.db
    run_id = db.enqueue(kind="generate", target="recording:1", label="Convert recording",
                        requested_by="local", timeout_seconds=60, destructive=False)
    db.update_run(run_id, status="passed", changes_json=_json.dumps(changes))
    return run_id


def test_the_run_page_names_a_deleted_test(client):
    run_id = _run_with(client, {
        "removed_nodeids": ["e2e/tests/test_x.py::test_create_does_not_apply_the_indicator"],
        "files": [{"status": "modified", "path": "e2e/tests/test_x.py"}],
        "tree": "known",
    })
    body = client.get(f"/runs/{run_id}").text
    assert "no longer exist" in body
    assert "test_create_does_not_apply_the_indicator" in body
    assert "What this run changed" in body


def test_the_run_page_does_not_claim_a_clean_tree_it_could_not_read(client):
    run_id = _run_with(client, {"removed_nodeids": [], "files": [], "tree": "unknown"})
    body = client.get(f"/runs/{run_id}").text
    assert "could not be read" in body
    assert "Nothing in the working tree changed" not in body


def test_a_run_without_changes_shows_no_panel(client):
    db = client.app.state.db
    run_id = db.enqueue(kind="pytest", target="tests/test_a.py::test_a", label="one test",
                        requested_by="local", timeout_seconds=60, destructive=False)
    db.update_run(run_id, status="passed")
    body = client.get(f"/runs/{run_id}").text
    assert "What this run changed" not in body


# --- the run-status API -----------------------------------------------------------------------
def test_a_queued_run_reports_itself_as_unfinished(client):
    db = client.app.state.db
    run_id = db.enqueue(kind="pytest", target="", label="all", destructive=False,
                        timeout_seconds=60, requested_by="local")
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "queued"
    assert body["finished"] is False
    assert body["url"] == f"/runs/{run_id}"


def test_a_crashed_run_counts_as_finished(client):
    """The set of terminal statuses is the server's to know.

    A poller that hardcodes ('passed', 'failed') loops forever on a run that crashed or timed
    out, which is exactly the situation somebody is polling to find out about.
    """
    db = client.app.state.db
    for status in ("passed", "failed", "error", "timeout", "cancelled", "crashed"):
        run_id = db.enqueue(kind="pytest", target="", label=status, destructive=False,
                            timeout_seconds=60, requested_by="local")
        db.update_run(run_id, status=status)
        assert client.get(f"/api/runs/{run_id}").json()["finished"] is True, status


def test_an_unknown_run_is_a_404_not_an_empty_object(client):
    response = client.get("/api/runs/99999")
    assert response.status_code == 404
    assert "no such run" in response.json()["error"]


def test_the_status_api_needs_the_token_too(tmp_path):
    from starlette.testclient import TestClient

    from testboard import auth
    from testboard.app import create_app
    from testboard.config import load

    (tmp_path / "testboard.yaml").write_text(RENDER_CONFIG, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    guarded = TestClient(create_app(load(tmp_path), auth.Policy("token", "s" * 32, "0.0.0.0")))
    assert guarded.get("/api/runs/1").status_code == 401

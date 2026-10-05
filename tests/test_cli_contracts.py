"""What the scripts promise a caller that is not a human.

An agent following SKILL.md runs these by name and reads the exit code. A script that fails and
exits 0 does not look like a failure — it looks like a result, and the run continues on top of it.

`extract_frames.py` exited 0 for a file that did not exist, writing an index with zero frames and
a duration of None. That reads as "this recording has nothing in it" rather than "the path was
wrong". `read_transcript.py` accepted an empty transcript the same way, and explained it with the
wrong reason: an empty `.vtt` was blamed on "an unexpected dialect", and an empty `.txt` on a
Teams `.docx` export, which is not even the format in hand.

These are contract tests. They run the real CLIs in a subprocess, because the exit code *is* the
thing under test and calling the functions directly would not exercise it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def run(script: str, *args: str):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )


# --- a path that is not there -------------------------------------------------------------------
@pytest.mark.parametrize("script,arg", [
    ("probe_media.py", "nope.mp4"),
    ("extract_frames.py", "nope.mp4"),
    ("grab_frame.py", "nope.mp4"),
    ("read_transcript.py", "nope.vtt"),
])
def test_a_missing_file_is_an_error_everywhere(script, arg, tmp_path):
    """All four, so the odd one out cannot drift back. extract_frames was the odd one out."""
    extra = ["--at", "1"] if script == "grab_frame.py" else []
    if script == "extract_frames.py":
        extra = ["--out", str(tmp_path / "frames")]
    result = run(script, str(tmp_path / arg), *extra)
    assert result.returncode != 0, f"{script} exited 0 for a file that does not exist"
    assert "not found" in (result.stdout + result.stderr).lower()


# --- an empty transcript ------------------------------------------------------------------------
@pytest.mark.parametrize("name,body", [
    ("empty.vtt", ""),
    ("empty.txt", ""),
    ("blank.txt", "   \n\n  \n"),
    ("blank.vtt", "\n\n\n"),
])
def test_an_empty_transcript_is_refused(name, body, tmp_path):
    """Narration is the only source of expected results, so nothing can be built on none of it."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    result = run("read_transcript.py", str(path))
    assert result.returncode == 1, f"{name} was accepted"
    assert "empty" in result.stdout.lower()


def test_the_empty_message_does_not_blame_the_export_format(tmp_path):
    """An empty .txt was explained as a Teams .docx export, which is not the format in hand."""
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    out = run("read_transcript.py", str(path)).stdout.lower()
    assert "docx" not in out
    assert "dialect" not in out


def test_a_transcript_with_content_is_still_accepted(tmp_path):
    """The guard must not catch a real file."""
    path = tmp_path / "ok.vtt"
    path.write_text("WEBVTT\n\n00:01.000 --> 00:04.000\n<v Alex>Open the dashboard.\n",
                    encoding="utf-8")
    result = run("read_transcript.py", str(path))
    assert result.returncode == 0
    assert "1 utterances" in result.stdout


def test_an_unsupported_extension_is_refused_rather_than_guessed(tmp_path):
    path = tmp_path / "notes.pdf"
    path.write_bytes(b"%PDF-1.4 not really")
    result = run("read_transcript.py", str(path))
    assert result.returncode == 1
    assert "unsupported" in result.stdout.lower()


# --- every script answers --help ----------------------------------------------------------------
@pytest.mark.parametrize("script", [
    "probe_media.py", "extract_frames.py", "grab_frame.py",
    "read_transcript.py", "build_project_map.py", "check_drift.py",
])
def test_every_script_starts(script):
    """An agent runs these by name. One that cannot start surfaces as a confusing transcript
    rather than as an error."""
    result = run(script, "--help")
    assert result.returncode == 0, f"{script} --help failed: {result.stderr[:200]}"
    assert "usage:" in result.stdout.lower()

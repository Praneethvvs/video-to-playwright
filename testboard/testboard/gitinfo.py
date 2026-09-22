"""What the working tree was when something ran.

A green run against an unknown commit tells you very little three weeks later, and `dirty` is the
field that matters most: a pass from a tree with uncommitted edits is not a pass anybody else can
reproduce. Recording it at the moment of the run is the only time it is knowable.

Every call is best-effort. A repository without git, a shallow clone, git missing from a container
— none of those are reasons to refuse to run a test, so they produce empty fields rather than an
error.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("testboard.gitinfo")

TIMEOUT = 5


@dataclass
class Commit:
    sha: str | None = None
    branch: str | None = None
    subject: str | None = None
    author: str | None = None
    dirty: bool | None = None

    def as_fields(self) -> dict:
        return {f"git_{k}": (int(v) if isinstance(v, bool) else v)
                for k, v in asdict(self).items()}


def _run(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            timeout=TIMEOUT, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s failed: %s", " ".join(args), exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def describe(repo: Path) -> Commit:
    if not (repo / ".git").exists():
        return Commit()
    sha = _run(repo, "rev-parse", "--short", "HEAD")
    if sha is None:
        return Commit()
    # --porcelain is empty exactly when the tree is clean; None means the command itself failed,
    # which is not the same as clean and must not be reported as it.
    status = _run(repo, "status", "--porcelain")
    dirty = None if status is None and _run(repo, "rev-parse", "HEAD") is None else bool(status)
    return Commit(
        sha=sha,
        branch=_run(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        subject=(_run(repo, "log", "-1", "--pretty=%s") or "")[:200] or None,
        author=_run(repo, "log", "-1", "--pretty=%an"),
        dirty=dirty,
    )

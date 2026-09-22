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


@dataclass
class Change:
    status: str          # a short word: added | modified | deleted | renamed | untracked
    path: str            # repo-relative, POSIX separators

    @property
    def shrinks_coverage(self) -> bool:
        return self.status in ("deleted", "modified")


_PORCELAIN = {
    "??": "untracked", "A": "added", "M": "modified", "D": "deleted",
    "R": "renamed", "C": "added", "T": "modified", "U": "modified",
}


def changed_paths(repo: Path) -> list[Change] | None:
    """What is different in the working tree right now.

    `None` means the question could not be answered — no git, git missing, a timeout. That is
    reported as "unknown", never as "nothing changed": those two look identical on a screen and
    only one of them is safe to believe.
    """
    if not (repo / ".git").exists():
        return None
    # Not `_run`: it returns None for empty output, and empty output here is the meaningful
    # answer "the tree is clean". Collapsing that into the failure case would report a clean
    # tree as unknown, which is the inverse of the mistake this function exists to avoid.
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=str(repo), capture_output=True, text=True,
            timeout=TIMEOUT, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git status failed: %s", exc)
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout

    changes: list[Change] = []
    fields = [f for f in raw.split("\0") if f]
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        # A rename is two NUL-separated fields: the code and new path, then the old path. Without
        # consuming the second one, the old path is parsed as its own entry and comes out as a
        # nonsense change with a two-character status sliced off its front.
        if code[0] == "R" or code[1] == "R":
            index += 1
        word = _PORCELAIN.get(code.strip()[:1] if code != "??" else "??", "modified")
        if code == "??":
            word = "untracked"
        changes.append(Change(word, path.replace("\\", "/")))
    return changes

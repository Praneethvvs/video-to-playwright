"""Spawning and killing process trees, correctly, on Windows and Linux.

pytest is not the process that needs killing. It starts a Playwright driver, which starts Chromium.
Killing the pytest PID alone reliably orphans the browser, and an orphaned headless Chromium holds
a VPN socket open and eats memory until someone notices — which, unattended, is never.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

log = logging.getLogger("testboard.procs")

IS_WINDOWS = sys.platform == "win32"


async def spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> asyncio.subprocess.Process:
    """Start a child in its own process group so the whole tree can be killed later.

    stderr is folded into stdout: the log a human reads should be the log the run produced, in the
    order it produced it, not two interleaved streams they have to reconcile.
    """
    kwargs: dict = {
        "cwd": str(cwd),
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "stdin": asyncio.subprocess.DEVNULL,
        # readline() raises ValueError when a single line exceeds the stream limit, which
        # defaults to 64 KiB. A Playwright failure prints the whole DOM of the element it could
        # not click, and that is routinely larger — so the default turns a normal failure into a
        # dead pump and a run that hangs until its timeout.
        "limit": 4 * 1024 * 1024,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # setsid: the child leads its own session, so killpg reaches every descendant without
        # touching testboard's own process group.
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


async def kill_tree(proc: asyncio.subprocess.Process, grace_seconds: float = 10.0) -> None:
    """Kill a running child and everything it started."""
    if proc.returncode is not None:
        return
    await _kill_pid_tree(proc.pid, grace_seconds)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning("pid %s did not reap within 5s after kill", proc.pid)


async def _kill_pid_tree(pid: int, grace_seconds: float) -> None:
    if IS_WINDOWS:
        # proc.terminate() on Windows is a soft console signal that does not walk the child tree.
        # taskkill /T /F is the only reliable option, and pytest has no graceful-shutdown behaviour
        # worth waiting for anyway.
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
        )
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        await asyncio.sleep(0.25)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def process_created_at(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def pid_is_same_process(pid: int | None, created_at: float | None) -> bool:
    """Is this PID still the process we started, or has the number been recycled?

    PIDs are reused, quickly on Linux and on a long-lived container. `os.kill(pid, 0)` only proves
    *something* is there. Comparing the recorded creation time is what makes the difference between
    reaping our orphan and killing a stranger.
    """
    if not pid or created_at is None:
        return False
    actual = process_created_at(pid)
    return actual is not None and abs(actual - created_at) < 1.0


async def kill_stale_pid(pid: int, created_at: float) -> bool:
    """Kill a tree left behind by a previous testboard, if it really is ours. Returns True if so."""
    if not pid_is_same_process(pid, created_at):
        return False
    await _kill_pid_tree(pid, grace_seconds=5.0)
    return True


BROWSER_NAMES = ("chrome", "chromium", "headless_shell", "msedge", "firefox", "webkit")

# Flags a browser only carries when something automated launched it. Requiring one of these is
# what makes it safe to also look at the working directory: a Playwright browser inherits
# pytest's cwd, which is the repository root, and so could a browser the person at the keyboard
# started from a terminal in that same directory. Killing somebody's actual browser because it
# was old and started from the wrong folder would be a far worse bug than leaking one of ours.
AUTOMATION_MARKERS = (
    "--enable-automation",
    "--remote-debugging-pipe",
    "--remote-debugging-port",
    "--headless",
    "--disable-field-trial-config",
    "-juggler-pipe",                    # Firefox, under Playwright
)


def sweep_orphan_browsers(*roots: Path, older_than_seconds: float = 1800,
                          run_in_progress: bool = False) -> int:
    """Kill browsers left behind by a run that died without cleaning up.

    Correct process-group handling covers the ordinary cases. This covers the rest: a SIGKILL
    cannot be trapped, so pytest can die mid-teardown and leave Chromium behind.

    The first version of this could never fire, for two independent reasons. It skipped any
    browser whose parent PID still existed — but on Windows an orphan keeps its dead parent's
    number, which is frequently already recycled and therefore "exists", and in a container an
    init shim gives a reparented orphan a live non-1 parent. And it required the browser's
    working directory to be under the state directory, while a Playwright browser inherits
    pytest's, which is the repository root.

    So ownership is now established from evidence instead: the process must be a browser, older
    than the threshold, and either working inside one of our directories or running against a
    profile inside one. Nothing is killed at all while a run is in progress, because a live run's
    browser is by definition not an orphan.
    """
    if run_in_progress:
        return 0

    killed = 0
    candidates = 0
    cutoff = time.time() - older_than_seconds
    resolved = [r.resolve() for r in roots]

    def ours(path: Path) -> bool:
        return any(path == root or root in path.parents for root in resolved)

    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not any(b in name for b in BROWSER_NAMES):
                continue
            if (proc.info.get("create_time") or 0) > cutoff:
                continue
            candidates += 1

            try:
                argv = proc.cmdline()
            except (psutil.AccessDenied, OSError):
                continue

            # Two independent conditions, both required. Automation launched it, AND it was
            # working in one of our directories. Either alone is not evidence: the person at the
            # keyboard may have a browser open, and they may have started it from this repo.
            automated = any(any(m in arg for m in AUTOMATION_MARKERS) for arg in argv)
            if not automated:
                continue

            located = False
            try:
                located = ours(Path(proc.cwd()).resolve())
            except (psutil.AccessDenied, OSError, ValueError):
                pass
            if not located:
                for arg in argv:
                    if arg.startswith("--user-data-dir="):
                        try:
                            located = ours(Path(arg.split("=", 1)[1]).resolve())
                        except (OSError, ValueError):
                            located = False
                        break
            if not located:
                continue

            proc.kill()
            killed += 1
            log.warning("killed orphaned browser pid=%s name=%s", proc.info["pid"], name)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            continue

    # Logged either way. A sweep that never kills anything is otherwise indistinguishable from a
    # sweep that is silently filtering out every real orphan, which is what it was doing.
    if candidates:
        log.info("orphan sweep: %d aged browser(s) seen, %d attributable to us and killed",
                 candidates, killed)
    return killed

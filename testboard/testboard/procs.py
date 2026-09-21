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


def sweep_orphan_browsers(under: Path, older_than_seconds: float = 1800) -> int:
    """Kill browsers that outlived whatever started them.

    Correct process-group handling covers the ordinary cases. This covers the rest: a SIGKILL
    cannot be trapped, so pytest can die mid-teardown and leave Chromium behind. Scoped to
    processes whose working directory sits under our own state directory, so nothing belonging to
    the person using the machine is ever a candidate.
    """
    killed = 0
    cutoff = time.time() - older_than_seconds
    under = under.resolve()
    for proc in psutil.process_iter(["pid", "name", "create_time", "ppid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not any(b in name for b in BROWSER_NAMES):
                continue
            if (proc.info.get("create_time") or 0) > cutoff:
                continue
            if psutil.pid_exists(proc.info.get("ppid") or 0) and (proc.info.get("ppid") or 0) > 1:
                continue  # still has a live parent; not an orphan
            cwd = Path(proc.cwd()).resolve()
            if under not in cwd.parents and cwd != under:
                continue
            proc.kill()
            killed += 1
            log.warning("killed orphaned browser pid=%s name=%s", proc.info["pid"], name)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return killed

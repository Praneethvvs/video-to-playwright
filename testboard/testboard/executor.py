"""The single-worker run loop.

One rule above all others: **an HTTP handler never spawns a subprocess.** Pressing Run inserts a
queued row and returns. This loop is the only caller of `spawn`. Without that discipline two fast
clicks, or two browser tabs, race to start the same run.

Runs are serialized because the suite mutates shared data in a dev environment that people are
also using — the repository says so plainly, and `pytest-xdist` being installed but unused is a
loaded gun. Drift checks and map captures go through the same queue, as rows with a different
`kind`, because they drive the same browser against the same host. Two independent "something is
running" flags that can both be true is how a small feature becomes a maintenance problem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import preflight, procs, results
from .config import Config
from .db import Database, utcnow
from .streaming import LogBus

log = logging.getLogger("testboard.executor")

KIND_PYTEST = "pytest"
KIND_DRIFT = "check_drift"
KIND_MAP = "build_map"

JOB_LABELS = {KIND_DRIFT: "Drift check", KIND_MAP: "Recapture project map"}


@dataclass
class Enqueued:
    run_id: int | None
    error: str | None = None
    duplicate_of: int | None = None


class Executor:
    def __init__(self, config: Config, database: Database, bus: LogBus):
        self.config = config
        self.db = database
        self.bus = bus
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._housekeeper: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._current_run_id: int | None = None
        self._orphans_to_kill: list[tuple[int, float]] = []

    # --- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        self.reconcile()
        self._worker = asyncio.create_task(self._worker_loop(), name="testboard-worker")
        self._housekeeper = asyncio.create_task(self._housekeeping_loop(), name="testboard-housekeeping")

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        for task in (self._worker, self._housekeeper):
            if task:
                task.cancel()
        if self._proc and self._proc.returncode is None:
            await procs.kill_tree(self._proc)

    def reconcile(self) -> None:
        """Nothing can legitimately be `running` at startup; this process just began.

        The recorded PID may since have been recycled and belong to a stranger, so the process
        creation time recorded at launch is compared too. If it matches, the orphan is ours and is
        killed — a run detached from its own log stream is worse than a re-run. If it does not, the
        row is closed and nothing is touched.
        """
        stale = self.db.query("SELECT * FROM runs WHERE status IN ('running', 'queued')")
        for row in stale:
            if row["status"] == "queued":
                self.db.update_run(row["id"], status="cancelled", error_reason="cancelled",
                                   finished_at=utcnow(),
                                   message="still queued when testboard restarted")
                continue
            still_ours = procs.pid_is_same_process(row["pid"], row["pid_created_at"])
            if still_ours:
                # Killed below, once there is an event loop to do it on.
                self._orphans_to_kill.append((row["pid"], row["pid_created_at"]))
            self.db.update_run(
                row["id"], status="crashed", error_reason="crashed", finished_at=utcnow(),
                message=("testboard restarted mid-run; its process was still alive and is being "
                         "reaped" if still_ours else
                         "testboard restarted mid-run; its process was already gone"),
            )
        if stale:
            log.warning("reconciled %d run(s) left behind by a previous process", len(stale))

    async def reap_orphans(self) -> None:
        """Kill the trees reconcile identified, now that an event loop exists."""
        while self._orphans_to_kill:
            pid, created = self._orphans_to_kill.pop()
            if await procs.kill_stale_pid(pid, created):
                log.warning("killed orphaned run tree pid=%s", pid)

    # --- enqueueing ----------------------------------------------------------------------
    def enqueue_pytest(self, *, target: str, label: str, markers: list[str],
                       requested_by: str) -> Enqueued:
        existing = self.db.already_pending(KIND_PYTEST, target)
        if existing:
            return Enqueued(None, "that is already queued or running", existing["id"])
        destructive = self.config.runner.markers.destructive in markers
        timeout = self.config.runner.timeouts.for_markers(markers)
        run_id = self.db.enqueue(kind=KIND_PYTEST, target=target, label=label,
                                 destructive=destructive, timeout_seconds=timeout,
                                 requested_by=requested_by)
        self._wake.set()
        return Enqueued(run_id)

    def enqueue_job(self, kind: str, requested_by: str) -> Enqueued:
        existing = self.db.already_pending(kind, "")
        if existing:
            return Enqueued(None, f"{JOB_LABELS[kind]} is already queued or running", existing["id"])
        run_id = self.db.enqueue(kind=kind, target="", label=JOB_LABELS[kind], destructive=False,
                                 timeout_seconds=600, requested_by=requested_by)
        self._wake.set()
        return Enqueued(run_id)

    async def cancel(self, run_id: int) -> bool:
        row = self.db.get_run(run_id)
        if not row or row["status"] not in ("queued", "running"):
            return False
        self.db.update_run(run_id, cancel_requested=1)
        if row["status"] == "queued":
            self.db.update_run(run_id, status="cancelled", error_reason="cancelled",
                               finished_at=utcnow(), message="cancelled before it started")
            return True
        if self._current_run_id == run_id and self._proc:
            await procs.kill_tree(self._proc)
        return True

    def queue_position(self, run_id: int) -> int | None:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'queued' AND id < ?", (run_id,)
        )
        return row["n"] if row else None

    # --- the loop ------------------------------------------------------------------------
    async def _worker_loop(self) -> None:
        await self.reap_orphans()
        while not self._stopping.is_set():
            row = self.db.next_queued()
            if row is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._execute(row)
            except Exception:                      # noqa: BLE001 - the loop must survive anything
                log.exception("run %s blew up in the executor", row["id"])
                self.db.update_run(row["id"], status="error", error_reason="test_error",
                                   finished_at=utcnow(),
                                   message="testboard failed while running this; see its log")

    async def _housekeeping_loop(self) -> None:
        from . import retention
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(3600)
                retention.sweep(self.config, self.db)
                procs.sweep_orphan_browsers(self.config.state_dir)
            except asyncio.CancelledError:
                raise
            except Exception:                      # noqa: BLE001
                log.exception("housekeeping pass failed")

    # --- execution -----------------------------------------------------------------------
    def run_dir(self, run_id: int) -> Path:
        # Named by the database id, never by timestamp or nodeid: timestamps collide under a fast
        # double click, and a nodeid carries brackets and slashes straight into a path.
        return self.config.runs_dir / f"run-{run_id}"

    async def _execute(self, row) -> None:
        run_id = row["id"]
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "stdout.log"

        check = await preflight.before_run(self.config)
        if not check.ok:
            # Decided here, by a check testboard performed, rather than inferred from a traceback
            # afterwards. The VPN being down must not look like a failing test.
            log_path.write_text(check.detail + "\n", encoding="utf-8")
            self.db.update_run(run_id, status="error", error_reason=check.reason,
                               started_at=utcnow(), finished_at=utcnow(), message=check.detail)
            return

        argv, env = self._command(row, run_dir)
        self.bus.open(run_id, log_path)
        self._current_run_id = run_id

        try:
            proc = await procs.spawn(argv, cwd=self.config.repo_root, env=env)
        except OSError as exc:
            self.db.update_run(run_id, status="error", error_reason="venv_broken",
                               started_at=utcnow(), finished_at=utcnow(),
                               message=f"could not start {argv[0]}: {exc}")
            self.bus.finish(run_id)
            self._current_run_id = None
            return

        self._proc = proc
        # Persisted before the first line is read, so a crash in this window still reconciles.
        self.db.update_run(run_id, status="running", started_at=utcnow(), pid=proc.pid,
                           pid_created_at=procs.process_created_at(proc.pid))

        pump = asyncio.create_task(self._pump(run_id, proc, log_path))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=row["timeout_seconds"])
        except asyncio.TimeoutError:
            timed_out = True
            await procs.kill_tree(proc)
        finally:
            await pump
            self.bus.finish(run_id)
            self._proc = None
            self._current_run_id = None

        await self._finalise(row, run_dir, proc.returncode, timed_out)

    def _command(self, row, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        cfg = self.config
        python = str(cfg.repo_python())
        env = cfg.environment.subprocess_env()

        if row["kind"] == KIND_PYTEST:
            argv = [python, "-m", "pytest"]
            target = row["target"]
            if target.startswith("-m "):
                argv += ["-m", target[3:]]
            elif target:
                # One argv element. Nodeids carry brackets from parametrisation and slashes from
                # fixture-generated ids; never route them through a shell.
                argv.append(target)
            argv += [
                "--junitxml", str(run_dir / "results.xml"),
                "--output", str(run_dir / "artifacts"),
                "-p", "testboard_collect_plugin",
                *cfg.runner.extra_args,
            ]
            if not target:
                argv += list(cfg.runner.test_paths)
            env["TESTBOARD_REPORT_OUT"] = str(run_dir / "reports.jsonl")
            plugin_dir = cfg.state_dir / "plugins"
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (f"{plugin_dir}{os.pathsep}{existing}" if existing
                                 else str(plugin_dir))
            return argv, env

        if row["kind"] == KIND_DRIFT:
            argv = [
                python, str(cfg.repo_root / cfg.drift.script),
                "--map", str(cfg.repo_root / cfg.drift.map),
                "--tests", cfg.runner.test_paths[0].split("/")[0],
                "--status-out", str(run_dir / "drift-status.json"),
            ]
            return argv, env

        if row["kind"] == KIND_MAP:
            argv = [
                python, str(cfg.repo_root / "scripts" / "build_project_map.py"),
                "--base-url", cfg.environment.base_url() or "",
                "--routes", str(cfg.repo_root / "routes.txt"),
                "--out", str(run_dir / "project-map.json"),
            ]
            channel = self._drift_channel()
            if channel:
                argv += ["--channel", channel]
            return argv, env

        raise ValueError(f"unknown run kind {row['kind']!r}")

    def _drift_channel(self) -> str | None:
        path = self.config.repo_root / self.config.drift.config
        try:
            return json.loads(path.read_text(encoding="utf-8-sig")).get("channel")
        except (OSError, json.JSONDecodeError):
            return None

    async def _pump(self, run_id: int, proc, log_path: Path) -> None:
        """Read the child's output once, fan it out, and persist it.

        Bytes, decoded explicitly. `text=True` decodes with locale.getpreferredencoding(), which is
        cp1252 on a default Windows install, and that has already produced mojibake in this project.
        """
        with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as fh:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                fh.write(line + "\n")
                fh.flush()
                self.bus.publish(run_id, line)

    async def _finalise(self, row, run_dir: Path, exit_code: int | None, timed_out: bool) -> None:
        run_id = row["id"]
        cancelled = bool(self.db.get_run(run_id)["cancel_requested"])
        log_text = ""
        log_path = run_dir / "stdout.log"
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")

        fields: dict = {"finished_at": utcnow(), "exit_code": exit_code}

        if timed_out:
            fields.update(status="timeout", error_reason="timeout",
                          message=f"killed after {row['timeout_seconds']}s")
        elif cancelled:
            fields.update(status="cancelled", error_reason="cancelled", message="cancelled")
        elif row["kind"] == KIND_PYTEST:
            fields.update(**self._finalise_pytest(run_id, run_dir, exit_code, log_text))
        elif row["kind"] == KIND_DRIFT:
            fields.update(**self._finalise_drift(run_dir, exit_code))
        elif row["kind"] == KIND_MAP:
            fields.update(**self._finalise_map(run_dir, exit_code))

        self.db.update_run(run_id, **fields)

    def _finalise_pytest(self, run_id: int, run_dir: Path, exit_code: int | None,
                         log_text: str) -> dict:
        reports = run_dir / "reports.jsonl"
        records: list[dict] = []
        if reports.exists():
            for line in reports.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if records:
            self.db.record_results(run_id, records)

        totals = results.parse(run_dir / "results.xml")
        payload = totals.as_dict() if totals else None
        if payload is not None:
            warning = results.detect_cleanup_warning(log_text)
            payload["cleanup_warning"] = warning

        if exit_code == 5:
            return {"status": "error", "error_reason": "test_error",
                    "message": "no tests matched that selection",
                    "totals_json": json.dumps(payload) if payload else None}
        if totals is None:
            return {"status": "error", "error_reason": "test_error",
                    "message": "the run produced no readable results; see the log"}

        status = totals.outcome
        message = None
        if payload and payload.get("cleanup_warning"):
            # Teardown that swallows its exception leaves mutated data behind and still reports a
            # pass. Saying so is the difference between a clean run and one that merely looks clean.
            message = "cleanup may not have completed — dev data may be left mutated"
        return {"status": status,
                "error_reason": None if status == "passed" else "test_failure",
                "totals_json": json.dumps(payload) if payload else None,
                "message": message}

    def _finalise_drift(self, run_dir: Path, exit_code: int | None) -> dict:
        status_file = run_dir / "drift-status.json"
        if not status_file.exists():
            return {"status": "error", "error_reason": "test_error",
                    "message": "the drift check produced no status file"}
        # Latest-wins copy, so the overview does not have to hunt through run directories.
        shutil.copyfile(status_file, self.config.state_dir / "drift-status.json")
        payload = json.loads(status_file.read_text(encoding="utf-8"))
        outcome = payload.get("outcome")
        if outcome in ("unreachable", "unsettled", "no-map"):
            return {"status": "error", "error_reason": "environment_unreachable",
                    "message": f"nothing was compared: {outcome}"}
        breaking = len(payload.get("breaking", []))
        return {"status": "failed" if breaking else "passed",
                "error_reason": "test_failure" if breaking else None,
                "message": (f"{breaking} breaking change(s)" if breaking
                            else "no breaking drift")}

    def _finalise_map(self, run_dir: Path, exit_code: int | None) -> dict:
        captured = run_dir / "project-map.json"
        if exit_code != 0 or not captured.exists():
            return {"status": "error", "error_reason": "environment_unreachable",
                    "message": "the capture did not produce a map"}
        # Deliberately not written over the committed map. A service that edits tracked files and
        # a CI job that watches them is a loop; a human reviews this and commits it.
        return {"status": "passed",
                "message": "captured — review it and copy it over project-map.json to accept"}

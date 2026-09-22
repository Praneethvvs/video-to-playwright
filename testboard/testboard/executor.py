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

from . import gitinfo, preflight, procs, results
from .config import Config
from .db import Database, utcnow
from .streaming import LogBus

log = logging.getLogger("testboard.executor")

KIND_PYTEST = "pytest"
KIND_DRIFT = "check_drift"
KIND_MAP = "build_map"
KIND_GENERATE = "generate"
KIND_COLLECT = "collect"

JOB_LABELS = {
    KIND_DRIFT: "Drift check",
    KIND_MAP: "Recapture project map",
    KIND_COLLECT: "Re-collect tests",
}


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
        self._agent = None

    # --- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        self.reconcile()
        self._worker = asyncio.create_task(self._worker_loop(), name="testboard-worker")
        self._housekeeper = asyncio.create_task(self._housekeeping_loop(), name="testboard-housekeeping")

    async def stop(self) -> None:
        """Bring the worker down without abandoning whatever it was doing.

        A generation run has no PID to kill, so killing `_proc` left the agent to be torn down by
        process exit — possibly partway through writing test files into the working tree, with
        nothing said about it afterwards. Interrupting it first at least gives the SDK the chance
        to stop cleanly.
        """
        self._stopping.set()
        self._wake.set()

        if self._agent is not None:
            try:
                await self._agent.interrupt()
            except Exception:                       # noqa: BLE001 - shutdown must not raise
                log.exception("could not interrupt the agent during shutdown")
        if self._proc and self._proc.returncode is None:
            await procs.kill_tree(self._proc)

        tasks = [t for t in (self._worker, self._housekeeper) if t]
        for task in tasks:
            task.cancel()
        # Awaited, so their finally blocks run before the loop closes. Cancelling without
        # awaiting means the cleanup may simply not happen.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    def enqueue_generation(self, source_id: int, label: str, requested_by: str) -> Enqueued:
        target = f"source:{source_id}"
        existing = self.db.already_pending(KIND_GENERATE, target)
        if existing:
            return Enqueued(None, "that recording is already being worked on", existing["id"])
        run_id = self.db.enqueue(
            kind=KIND_GENERATE, target=target, label=label, destructive=False,
            timeout_seconds=self.config.generation.timeout_seconds, requested_by=requested_by,
        )
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
        if self._current_run_id == run_id:
            # A generation run is an in-process agent, not a subprocess, so it is interrupted
            # through the SDK rather than killed. Same button, same queue, different mechanism.
            if self._agent is not None:
                await self._agent.interrupt()
            elif self._proc:
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
            # Inside the try as well: a database error on the dequeue itself would otherwise
            # kill the worker task, and a dead worker means every future run sits queued for
            # ever with nothing in the interface admitting it.
            try:
                row = self.db.next_queued()
            except Exception:                      # noqa: BLE001
                log.exception("could not read the queue; retrying shortly")
                await asyncio.sleep(5)
                continue
            if row is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._execute(row)
            except Exception as exc:               # noqa: BLE001 - the loop must survive anything
                log.exception("run %s blew up in the executor", row["id"])
                # Say what went wrong. "See its log" is useless advice when the failure happened
                # before anything was written to that log, which is exactly when it happens.
                self.db.update_run(
                    row["id"], status="error", error_reason="test_error", finished_at=utcnow(),
                    message=f"testboard itself failed while starting this run: "
                            f"{type(exc).__name__}: {exc}",
                )
            finally:
                # Belt and braces. Each execute path clears its own state, but a failure in the
                # few lines before their try blocks would otherwise pin the executor to a dead
                # run and leave its SSE stream open for the life of the process.
                if self._current_run_id == row["id"]:
                    self.bus.finish(row["id"])
                    self._current_run_id = None
                    self._agent = None
                    self._proc = None

    async def _housekeeping_loop(self) -> None:
        from . import retention
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(3600)
                retention.sweep(self.config, self.db)
                # Both directories: a Playwright browser inherits pytest's working directory,
                # which is the repository root, not the state directory.
                procs.sweep_orphan_browsers(
                    self.config.state_dir, self.config.repo_root,
                    run_in_progress=self._current_run_id is not None,
                )
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

        if row["kind"] == KIND_GENERATE:
            await self._execute_generation(row, run_dir, log_path)
            return

        if row["kind"] == KIND_COLLECT:
            # Collection starts a second pytest in the same repository. Even though it only
            # imports conftest rather than running anything, it belongs in the queue: the one
            # rule here is that a subprocess is started by this loop and nowhere else, and an
            # untimed spawn from an HTTP handler had no kill path, no PID recorded and no way
            # for anybody to see that it was happening.
            self.db.update_run(run_id, status="running", started_at=utcnow())
            from . import inventory
            result = await inventory.refresh(self.config, self.db)
            message = (f"{len(result.items)} test(s) collected"
                       + (f", {len(result.new_nodeids)} new and awaiting approval"
                          if result.new_nodeids else "")) if result.ok else result.detail[-500:]
            log_path.write_text(f"{message}\n", encoding="utf-8")
            self.db.update_run(
                run_id, finished_at=utcnow(),
                status="passed" if result.ok else "failed",
                error_reason=None if result.ok else "collection_error",
                exit_code=result.exit_code, message=message[:2000],
            )
            return

        # Cancel can land between the worker dequeuing this row and the process starting. The
        # handler sets cancel_requested and finds nothing running to kill, so without this check
        # the run reports "cancelled" and then executes in full anyway.
        if self.db.get_run(run_id)["cancel_requested"]:
            self.db.update_run(run_id, status="cancelled", error_reason="cancelled",
                               started_at=utcnow(), finished_at=utcnow(),
                               message="cancelled before it started")
            return

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

        # The exact command, at the top of the log and in the file — the pump appends rather than
        # truncating so this survives a reload. Without it, "why did that run pick those tests"
        # is guesswork, and a selection bug stays invisible until somebody counts the results.
        header = "$ " + " ".join(a if " " not in a else f'"{a}"' for a in argv)
        log_path.write_text(header + "\n\n", encoding="utf-8", newline="\n")
        self.bus.publish(run_id, header)
        self.bus.publish(run_id, "")

        self._proc = proc
        # Persisted before the first line is read, so a crash in this window still reconciles.
        # to_thread: describe() shells out to git five times, and doing that on the event loop
        # stalls every SSE stream and HTTP request for its duration.
        commit = await asyncio.to_thread(gitinfo.describe, self.config.repo_root)
        self.db.update_run(run_id, status="running", started_at=utcnow(), pid=proc.pid,
                           pid_created_at=await asyncio.to_thread(
                               procs.process_created_at, proc.pid),
                           **commit.as_fields())

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

    async def _execute_generation(self, row, run_dir: Path, log_path: Path) -> None:
        """Drive the agent in-process, through the same queue everything else uses.

        No subprocess, so no process group to kill and no pipe to scrape — but it still holds the
        single worker slot, because a generation run reads and writes the same working tree a test
        run does, and two of those at once is a merge conflict with extra steps.
        """
        from . import agent as agentmod

        run_id = row["id"]
        source_id = int(row["target"].split(":", 1)[1])
        source = self.db.get_source(source_id)

        available = agentmod.availability()
        if not available.ok:
            log_path.write_text(available.detail + "\n", encoding="utf-8")
            self.db.update_run(run_id, status="error", error_reason="agent_unavailable",
                               started_at=utcnow(), finished_at=utcnow(),
                               message=available.detail)
            return
        if source is None:
            self.db.update_run(run_id, status="error", error_reason="test_error",
                               started_at=utcnow(), finished_at=utcnow(),
                               message="that recording no longer exists")
            return
        if not source["transcript_text"]:
            detail = ("This recording has no transcript. The video shows what was done; only the "
                      "narration says why, and what was supposed to happen. Upload the .vtt "
                      "before generating.")
            log_path.write_text(detail + "\n", encoding="utf-8")
            self.db.update_run(run_id, status="error", error_reason="agent_unavailable",
                               started_at=utcnow(), finished_at=utcnow(), message=detail)
            return

        if self.db.get_run(run_id)["cancel_requested"]:
            self.db.update_run(run_id, status="cancelled", error_reason="cancelled",
                               started_at=utcnow(), finished_at=utcnow(),
                               message="cancelled before it started")
            return

        cfg = self.config
        self.bus.open(run_id, log_path)
        self._current_run_id = run_id
        handle = None

        # Everything from bus.open() onward is inside this try, not just the agent call. Opening
        # a stream and then raising before reaching the guarded section leaves the SSE generator
        # live forever — is_live() stays true, the browser's log panel never terminates, and the
        # subscriber is never released. Setting up the run is exactly where a mistake is most
        # likely, so it is exactly what has to be covered.
        try:
            commit = await asyncio.to_thread(gitinfo.describe, cfg.repo_root)
            self.db.update_run(run_id, status="running", started_at=utcnow(),
                               **commit.as_fields())
            handle = log_path.open("w", encoding="utf-8", errors="replace", newline="\n")

            def emit(line: str) -> None:
                handle.write(line + "\n")
                handle.flush()
                self.bus.publish(run_id, line)

            video = (cfg.state_dir / source["video_path"]) if source["video_path"] else None
            prompt = agentmod.PROMPT.format(
                video=video or "(no video was uploaded — work from the transcript)",
                transcript=source["transcript_name"] or "(inline)",
                base_url=cfg.environment.base_url() or f"${cfg.environment.base_url_env}",
                test_dir=", ".join(cfg.runner.test_paths),
                transcript_text=source["transcript_text"][:400_000],
            )

            self._agent = agentmod.CodexAgent(
                repo_root=cfg.repo_root,
                skill_path=cfg.generation.skill_path(cfg.repo_root),
                model=cfg.generation.model,
                sandbox=cfg.generation.sandbox,
                env=cfg.environment.subprocess_env(),
                state_dir=cfg.state_dir,
            )

            result = await asyncio.wait_for(
                self._agent.run(prompt=prompt, emit=emit),
                timeout=row["timeout_seconds"],
            )
            fields = {
                "finished_at": utcnow(),
                "status": "passed" if result.ok else "failed",
                "error_reason": None if result.ok else "agent_failed",
                "message": (result.final_response or "")[:4000] or result.error or result.status,
                "exit_code": 0 if result.ok else 1,
            }
        except asyncio.TimeoutError:
            if self._agent is not None:
                await self._agent.interrupt()
            fields = {"finished_at": utcnow(), "status": "timeout", "error_reason": "timeout",
                      "message": f"the agent was still working after {row['timeout_seconds']}s"}
        except Exception as exc:                       # noqa: BLE001
            log.exception("generation run %s failed", run_id)
            detail = f"{type(exc).__name__}: {exc}"
            self.bus.publish(run_id, f"ERROR: {detail}")
            if handle is not None:
                handle.write(f"ERROR: {detail}\n")
            fields = {"finished_at": utcnow(), "status": "error", "error_reason": "agent_failed",
                      "message": detail}
        finally:
            if handle is not None:
                handle.close()
            self.bus.finish(run_id)
            self._agent = None
            self._current_run_id = None

        if self.db.get_run(run_id)["cancel_requested"]:
            fields.update(status="cancelled", error_reason="cancelled", message="cancelled")
        self.db.update_run(run_id, **fields)

        # The agent has written into the working tree. Whatever it added is only real once the
        # tests are collected again, so refresh rather than leaving a stale list on screen — and
        # anything new is attributed to this recording, so the Recordings page can say how many
        # tests came out of it and who has signed them off.
        from . import inventory
        collected = await inventory.refresh(self.config, self.db, origin="generated",
                                            source_id=source_id)
        if collected.new_nodeids:
            self.db.update_source(source_id, last_generation_run=run_id)
            # Appended to the log as well as streamed. Publishing only to the bus meant the most
            # useful line of the whole run — what it actually produced — was missing from the log
            # anybody reads afterwards, because the file handle had already been closed.
            tail = [f"{len(collected.new_nodeids)} new test(s) collected, awaiting approval:"]
            tail += [f"  {nodeid}" for nodeid in collected.new_nodeids]
            with log_path.open("a", encoding="utf-8", errors="replace", newline="") as fh:
                for line in tail:
                    fh.write(line + chr(10))
            for line in tail:
                self.bus.publish(run_id, line)

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

            # Anything broader than one named test runs what has been approved and nothing else.
            # Asking "is the target empty" is not enough: "run all" arrives here already narrowed
            # to `-m not writes`, so that check silently never fired and a pending test rode
            # along in a sweep that claimed to be the suite.
            #
            # Deselecting the unapproved keeps the command line short — there are usually a few
            # pending and many approved — and leaves the selection legible in the log.
            # present_only=False deliberately. If a collection ever returns nothing — a broken
            # import, a bad filter — every row is marked absent, and filtering on present would
            # make this list empty and quietly run the unapproved tests in the next sweep.
            unapproved = [t["nodeid"] for t in self.db.inventory(present_only=False)
                          if t["state"] != "approved"]
            if target not in unapproved:
                for nodeid in unapproved:
                    argv += ["--deselect", nodeid]
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
        # Append: the command line was written here before the process started.
        with log_path.open("a", encoding="utf-8", errors="replace", newline="\n") as fh:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # Longer than even the generous limit set in procs.spawn. Take what is
                    # buffered and carry on: losing a line boundary is cosmetic, whereas letting
                    # this propagate kills the pump and hangs the run until its timeout.
                    raw = await proc.stdout.read(65536)
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

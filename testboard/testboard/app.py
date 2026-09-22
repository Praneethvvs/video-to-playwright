"""HTTP surface.

Nodeids are never path segments. Parametrisation puts `[` and `]` in them and fixture-generated
ids routinely put `/` in them too, so a nodeid in a path is a double-encoding bug waiting to
happen. Actions take the target in a form body and links pass it as a query parameter, both of
which the browser encodes correctly without help.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import (__version__, agent as agentmod, drift as driftmod, inventory, media,
               preflight, retention)
from .config import Config
from .db import Database
from .executor import KIND_COLLECT, KIND_DRIFT, KIND_MAP, KIND_PYTEST, Executor
from .streaming import LogBus

log = logging.getLogger("testboard.app")

HERE = Path(__file__).parent


def create_app(config: Config) -> FastAPI:
    database = Database(config.db_path)
    bus = LogBus()
    executor = Executor(config, database, bus)

    app = FastAPI(title=f"testboard — {config.repo_name}", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def reject_cross_site_writes(request: Request, call_next):
        """Refuse a state-changing request that another site caused the browser to make.

        There is no authentication here, so the browser's own cookies are not the thing being
        abused — the browser's *reachability* is. Any page the user has open can auto-submit a
        hidden form to 127.0.0.1:8770 and approve a test under their name, start a destructive
        run, or delete a recording. Every mutation is a plain form POST, so nothing about the
        request shape prevents that.

        `Sec-Fetch-Site` is sent by every current browser and is not forgeable by page script,
        which makes it the cheapest correct defence. Where it is absent (an older browser, curl,
        the pipeline) the Origin header is checked instead, and a request with neither is allowed
        through — refusing those would break every script and give no security, since an attacker
        controls neither header from a page anyway.
        """
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            site = request.headers.get("sec-fetch-site")
            if site and site not in ("same-origin", "same-site", "none"):
                return PlainTextResponse(
                    f"Refused: this {request.method} came from another site "
                    f"(Sec-Fetch-Site: {site}). testboard only accepts changes made from its own "
                    f"pages.", status_code=403,
                )
            if not site:
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                    return PlainTextResponse(
                        f"Refused: Origin {origin} is not this application.", status_code=403,
                    )
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def back_to(request: Request, fallback: str) -> str:
        """Where to send the browser after a form post.

        The Referer is attacker-controllable, so it is only honoured when it points at this
        application. Handing it to a Location header unchecked is an open redirect.
        """
        referer = request.headers.get("referer")
        if not referer:
            return fallback
        try:
            parsed = urlparse(referer)
        except ValueError:
            return fallback
        if parsed.netloc and parsed.netloc != request.url.netloc:
            return fallback
        path = parsed.path or fallback
        return f"{path}?{parsed.query}" if parsed.query else path

    def when(value: str | None) -> Markup:
        """Render a timestamp the browser can localise.

        Everything is stored in UTC, and the server has no idea what timezone the reader is in —
        a container's clock is not their clock. So the ISO value goes out in the markup and the
        page turns it into "12 min ago" with the absolute local time on hover. With JavaScript
        off it degrades to the timestamp itself rather than to nothing.
        """
        if not value:
            return Markup('<span class="muted">—</span>')
        return Markup(f'<time class="t" datetime="{escape(value)}">{escape(value)}</time>')

    templates.env.filters["when"] = when

    app.state.config = config
    app.state.db = database
    app.state.executor = executor

    @app.on_event("startup")
    async def _startup() -> None:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.runs_dir.mkdir(parents=True, exist_ok=True)
        await executor.start()
        app.state.startup_checks = preflight.at_startup(config)
        # Reference retained: asyncio only holds a weak reference to a task, so a bare
        # create_task can be collected mid-flight, and its exception would be swallowed either
        # way. The first collection failing silently is how a dashboard comes up empty with no
        # explanation.
        task = asyncio.create_task(inventory.refresh(config, database))
        app.state.startup_collect = task
        task.add_done_callback(
            lambda t: t.cancelled() or (t.exception() and
                                        log.error("startup collection failed: %s", t.exception()))
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await executor.stop()
        database.close()

    # --- shared view context -------------------------------------------------------------
    def _take_flash() -> list[str]:
        messages = database.get_meta("last_upload") or []
        if messages:
            database.set_meta("last_upload", [])
        return messages

    def base_context(request: Request) -> dict:
        active = database.active()
        return {
            "request": request,
            "version": __version__,
            "repo_name": config.repo_name,
            "base_url": config.environment.base_url(),
            "base_url_env": config.environment.base_url_env,
            "active": active,
            "queued": database.queued(),
            # Shown on every page, not just the run's own. Somebody opening a second tab needs to
            # know dev data is mid-mutation before they click Run.
            "destructive_running": bool(active and active["destructive"]),
            "startup_checks": getattr(app.state, "startup_checks", []),
            "collect": database.get_meta("collect", {}),
            "test_total": (database.get_meta("collect", {}) or {}).get("count", 0),
            "pending_total": database.state_counts()["pending"],
            "source_total": database.one(
                "SELECT COUNT(*) AS n FROM sources")["n"],
            # Read once and cleared. Left in the database it becomes a message about an
            # upload from hours ago, shown at the top of the page as though it just happened.
            "last_upload": _take_flash(),
        }

    def render(name: str, request: Request, **extra) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name=name, context={**base_context(request), **extra}
        )

    # --- pages ---------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        tests = database.inventory()
        latest = database.latest_results()
        quarantine = config.runner.markers.quarantine
        quarantined = [t for t in tests if quarantine in t["markers"]]
        last_run = database.one(
            "SELECT * FROM runs WHERE kind = 'pytest' AND status NOT IN ('queued','running') "
            "ORDER BY id DESC LIMIT 1"
        )
        # Outcomes are reported across the approved suite only. Mixing in tests nobody has
        # accepted yet makes "3 failed" mean something different from one day to the next.
        results = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "unknown": 0}
        for t in tests:
            if t["state"] != "approved":
                continue
            row = latest.get(t["nodeid"])
            results[row["outcome"] if row and row["outcome"] in results else "unknown"] += 1
        counts = {
            "approved": sum(1 for t in tests if t["state"] == "approved"),
            "pending": sum(1 for t in tests if t["state"] == "pending"),
            "rejected": sum(1 for t in tests if t["state"] == "rejected"),
        }
        return render(
            "overview.html", request,
            map_info=driftmod.map_info(config),
            drift=driftmod.drift_info(config),
            test_count=len(tests),
            counts=counts,
            results=results,
            quarantined=quarantined,
            last_run=last_run,
            sweep=database.get_meta("last_retention_sweep"),
        )

    @app.get("/tests", response_class=HTMLResponse)
    async def tests_page(request: Request):
        tests = database.inventory()
        latest = database.latest_results()
        drift = driftmod.drift_info(config)
        stale_files = set(drift.stale_test_files)
        for t in tests:
            row = latest.get(t["nodeid"])
            t["last"] = dict(row) if row else None
            t["destructive"] = config.runner.markers.destructive in t["markers"]
            t["quarantined"] = config.runner.markers.quarantine in t["markers"]
            # Inference, and labelled as such in the template: the drift check finds stale files by
            # substring search, not by a real link between a test and a map entry.
            t["maybe_stale"] = t["file"] in stale_files
        by_file: dict[str, list] = {}
        for t in tests:
            by_file.setdefault(t["file"], []).append(t)
        markers = sorted({m for t in tests for m in t["markers"]})
        counts = {
            "approved": sum(1 for t in tests if t["state"] == "approved"),
            "pending": sum(1 for t in tests if t["state"] == "pending"),
            "rejected": sum(1 for t in tests if t["state"] == "rejected"),
        }
        return render("tests.html", request, by_file=by_file, markers=markers,
                      total=len(tests), never_batch=config.safety.never_batch,
                      counts=counts,
                      pending=[t for t in tests if t["state"] == "pending"],
                      sources={s["id"]: s for s in database.sources()})

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request, target: str | None = Query(None)):
        rows = database.history(limit=200, target=target)
        present = database.present_nodeids()
        return render("runs.html", request, rows=rows, target=target, present=present)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: int):
        row = database.get_run(run_id)
        if row is None:
            return HTMLResponse("<h1>No such run</h1>", status_code=404)
        run_dir = executor.run_dir(run_id)
        totals = json.loads(row["totals_json"]) if row["totals_json"] else None
        artifacts = []
        art_dir = run_dir / "artifacts"
        if art_dir.exists():
            artifacts = sorted(
                p.relative_to(art_dir).as_posix()
                for p in art_dir.rglob("*") if p.is_file()
            )
        return render(
            "run.html", request, run=row, totals=totals, artifacts=artifacts,
            results=database.results_for_run(run_id),
            position=executor.queue_position(run_id) if row["status"] == "queued" else None,
            live=row["status"] in ("queued", "running"),
            present=database.present_nodeids(),
        )

    @app.get("/drift", response_class=HTMLResponse)
    async def drift_page(request: Request):
        return render("drift.html", request,
                      map_info=driftmod.map_info(config),
                      drift=driftmod.drift_info(config))

    # --- actions -------------------------------------------------------------------------
    @app.post("/run")
    async def start_run(request: Request, target: str = Form(""), label: str = Form(""),
                        confirmed: str = Form("")):
        tests = {t["nodeid"]: t for t in database.inventory()}
        destructive_marker = config.runner.markers.destructive

        # Narrow the selection first. "Run all" excludes destructive tests by policy rather than
        # by the person remembering, and the confirmation question must then be asked about what
        # will actually run — not about what the unnarrowed selection would have contained.
        if not target and destructive_marker in config.safety.never_batch:
            target = f"-m not {destructive_marker}"
            label = label or f"Whole suite (excluding {destructive_marker})"

        if target in tests:
            markers = tests[target]["markers"]
        elif target.startswith("-m "):
            expression = target[3:]
            markers = [] if expression.startswith("not ") else [expression]
        elif target:
            markers = sorted({m for t in tests.values() if t["file"] == target
                              for m in t["markers"]})
        else:
            markers = sorted({m for t in tests.values() for m in t["markers"]})

        if any(m in config.safety.confirm_before_run for m in markers) and confirmed != "yes":
            # Back to the page with an explanation, not a JSON blob. The browser gets here when
            # JavaScript is off or the confirm dialog was bypassed, and a raw 400 body is a dead
            # end for someone who just pressed a button.
            gated = [m for m in markers if m in config.safety.confirm_before_run]
            database.set_meta("last_upload", [
                f"That run was not started: it includes tests marked {', '.join(gated)}, which "
                f"change data in {config.environment.base_url() or config.environment.base_url_env}. "
                f"Use the Run button on the page so the confirmation is recorded."
            ])
            return RedirectResponse(back_to(request, "/tests"), status_code=303)

        result = executor.enqueue_pytest(
            target=target, label=label or target or "Whole suite",
            markers=markers, requested_by=_who(request),
        )
        if result.run_id is None:
            return RedirectResponse(f"/runs/{result.duplicate_of}", status_code=303)
        return RedirectResponse(f"/runs/{result.run_id}", status_code=303)

    @app.post("/jobs/{kind}")
    async def start_job(request: Request, kind: str):
        if kind not in (KIND_DRIFT, KIND_MAP, KIND_COLLECT):
            return JSONResponse({"error": "unknown job"}, status_code=404)
        result = executor.enqueue_job(kind, requested_by=_who(request))
        target_id = result.run_id or result.duplicate_of
        return RedirectResponse(f"/runs/{target_id}", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: int):
        await executor.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/pin")
    async def pin_run(run_id: int):
        row = database.get_run(run_id)
        if row:
            database.update_run(run_id, pinned=0 if row["pinned"] else 1)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/tests/decide")
    async def decide(request: Request, nodeid: str = Form(...), state: str = Form(...)):
        """Promote a draft into the suite, or reject it. Who decided is part of the record."""
        if state not in ("approved", "rejected", "pending"):
            return JSONResponse({"error": "unknown state"}, status_code=400)
        database.decide(nodeid, state, _who(request))
        return RedirectResponse(back_to(request, "/tests"), status_code=303)

    @app.post("/tests/decide-all")
    async def decide_all(request: Request, state: str = Form(...), file: str = Form("")):
        if state not in ("approved", "rejected"):
            return JSONResponse({"error": "unknown state"}, status_code=400)
        who = _who(request)
        for test in database.pending_tests():
            if not file or test["file"] == file:
                database.decide(test["nodeid"], state, who)
        return RedirectResponse(back_to(request, "/tests"), status_code=303)

    @app.post("/inventory/refresh")
    async def refresh_inventory(request: Request):
        # Enqueued, not run here. Collecting starts a pytest process, and this handler is not
        # allowed to do that — see the executor's module docstring.
        outcome = executor.enqueue_job(KIND_COLLECT, _who(request))
        database.set_meta("last_upload", [
            "Re-collecting tests. It will appear in Runs, and the list refreshes when it finishes."
            if outcome.run_id else "A re-collect is already queued."
        ])
        # Back where the button was pressed. Sending someone from Overview to Tests because a
        # different page happens to host the same action is the kind of small wrongness that
        # makes an interface feel unreliable.
        return RedirectResponse(back_to(request, "/tests"), status_code=303)

    @app.post("/maintenance/sweep")
    async def manual_sweep():
        retention.sweep(config, database)
        return RedirectResponse("/", status_code=303)

    # --- recordings and transcripts ---------------------------------------------------------
    def _source_view(row, all_tests: list[dict] | None = None) -> dict:
        """A recording plus what came out of it: runs, tests, and how many still need a decision.

        `all_tests` lets the list page load the inventory once instead of once per recording.
        """
        runs = database.history(limit=10, target=f"source:{row['id']}")
        tests = [t for t in (all_tests if all_tests is not None else database.inventory())
                 if t["source_id"] == row["id"]]
        return {
            "row": row,
            "runs": runs,
            "last_run": runs[0] if runs else None,
            "tests": tests,
            "approved": [t for t in tests if t["state"] == "approved"],
            "pending": [t for t in tests if t["state"] == "pending"],
            "rejected": [t for t in tests if t["state"] == "rejected"],
        }

    @app.get("/recordings", response_class=HTMLResponse)
    async def recordings_page(request: Request):
        all_tests = database.inventory()
        views = [_source_view(row, all_tests) for row in database.sources()]
        return render("recordings.html", request, views=views,
                      agent=agentmod.availability(),
                      discovered=media.discover(config))

    @app.get("/recordings/{source_id}", response_class=HTMLResponse)
    async def recording_page(request: Request, source_id: int):
        row = database.get_source(source_id)
        if row is None:
            return HTMLResponse("<h1>No such recording</h1>", status_code=404)
        cues = json.loads(row["transcript_cues"]) if row["transcript_cues"] else []
        return render("recording.html", request, source=row, cues=cues[:400],
                      cue_total=len(cues), view=_source_view(row),
                      agent=agentmod.availability())

    @app.post("/recordings/upload")
    async def upload(request: Request, files: list[UploadFile] = File(default=[]),
                     source_id: int | None = Form(None)):
        # Optional rather than required. A missing field makes FastAPI answer 422 with a JSON
        # validation blob, which is a dead end in a browser that just posted a form.
        messages, target = [], source_id
        real = [f for f in files if f and f.filename]
        if not real:
            database.set_meta("last_upload", ["No file was chosen."])
            return RedirectResponse(back_to(request, "/recordings"),
                                    status_code=303)
        for upload_file in real:
            try:
                accepted = await media.accept(
                    config, database, filename=upload_file.filename or "upload",
                    stream=upload_file, uploaded_by=_who(request), source_id=target,
                )
                # A video and its transcript arriving together belong to one recording, so the
                # first file decides the source and the rest attach to it.
                target = accepted.source_id
                messages.append(accepted.message)
            except ValueError as exc:
                # The exception already names the file; prefixing it again reads as a stutter.
                messages.append(str(exc))
            finally:
                await upload_file.close()

        database.set_meta("last_upload", messages)
        if target:
            return RedirectResponse(f"/recordings/{target}", status_code=303)
        return RedirectResponse("/recordings", status_code=303)

    @app.post("/recordings/import")
    async def import_discovered(request: Request, path: str = Form(...)):
        candidate = (config.repo_root / path).resolve()
        # is_relative_to rather than a parents membership test: the latter is false for a file
        # sitting directly in the root on some paths, and neither is a substitute for resolving
        # first, which is what actually defeats `..`.
        if not candidate.is_relative_to(config.repo_root) or not candidate.is_file():
            database.set_meta("last_upload", [f"{path}: not a file in this repository"])
            return RedirectResponse("/recordings", status_code=303)

        class _FileStream:
            """Adapts a file on disk to the same async read() the upload path expects."""
            def __init__(self, handle):
                self._handle = handle

            async def read(self, size: int = -1) -> bytes:
                return self._handle.read(size)

        with candidate.open("rb") as handle:
            accepted = await media.accept(
                config, database, filename=candidate.name, stream=_FileStream(handle),
                uploaded_by=_who(request),
            )
        return RedirectResponse(f"/recordings/{accepted.source_id}", status_code=303)

    @app.post("/recordings/{source_id}/generate")
    async def generate(request: Request, source_id: int):
        row = database.get_source(source_id)
        if row is None:
            return JSONResponse({"error": "no such recording"}, status_code=404)
        result = executor.enqueue_generation(
            source_id, f"Generate tests from “{row['title']}”", _who(request),
        )
        return RedirectResponse(f"/runs/{result.run_id or result.duplicate_of}", status_code=303)

    @app.post("/recordings/{source_id}/delete")
    async def delete_recording(source_id: int):
        media.delete(config, database, source_id)
        return RedirectResponse("/recordings", status_code=303)

    @app.post("/recordings/{source_id}/rename")
    async def rename_recording(source_id: int, title: str = Form(...)):
        database.update_source(source_id, title=title[:120])
        return RedirectResponse(f"/recordings/{source_id}", status_code=303)

    @app.get("/recordings/{source_id}/video")
    async def video(source_id: int):
        row = database.get_source(source_id)
        if row is None or not row["video_path"]:
            return PlainTextResponse("no video for this recording", status_code=404)
        path = config.state_dir / row["video_path"]
        if not path.is_file():
            return PlainTextResponse("the file is no longer on disk", status_code=404)
        guessed = mimetypes.guess_type(path.name)[0] or "video/mp4"
        return FileResponse(path, media_type=guessed)

    # --- streaming and files --------------------------------------------------------------
    @app.get("/runs/{run_id}/stream")
    async def stream(run_id: int, request: Request):
        log_path = executor.run_dir(run_id) / "stdout.log"

        async def events():
            since = 0
            header = request.headers.get("last-event-id")
            if header and header.isdigit():
                since = int(header)

            # A queued run has no stream yet, because nothing has opened one. Answering "done"
            # here told the page the run had finished, the page reloaded, and it queued again —
            # a reload loop that only stops when the run reaches the front of the queue. Wait
            # for it to start instead, heartbeating so the connection survives a proxy.
            waited = 0.0
            while not bus.is_live(run_id):
                row = database.get_run(run_id)
                if row is None or row["status"] not in ("queued", "running"):
                    break
                if waited and waited % 10 < 0.5:
                    yield ": waiting for the run to start\n\n"
                await asyncio.sleep(0.5)
                waited += 0.5

            for seq, line in bus.backlog(run_id, since, log_path):
                yield _sse(seq, line)
                since = seq

            if not bus.is_live(run_id):
                yield "event: done\ndata: finished\n\n"
                return

            async for seq, line in bus.subscribe(run_id):
                if seq is None:
                    # Heartbeat. Proxies close idle connections silently, and the failed write on
                    # a dead client is how its subscription gets cleaned up.
                    yield ": heartbeat\n\n"
                    continue
                yield _sse(seq, line)
            yield "event: done\ndata: finished\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
    async def raw_log(run_id: int):
        path = executor.run_dir(run_id) / "stdout.log"
        if not path.exists():
            return PlainTextResponse("no log for this run", status_code=404)
        # to_thread: a long run's log is megabytes, and reading it on the event loop stalls every
        # other request and every live stream for the duration.
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        return PlainTextResponse(text)

    @app.get("/runs/{run_id}/artifact")
    async def artifact(run_id: int, name: str = Query(...)):
        root = (executor.run_dir(run_id) / "artifacts").resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        guessed = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=guessed, filename=target.name)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    return app


def _sse(seq: int, line: str) -> str:
    # id: lets the browser resume with Last-Event-ID after a dropped connection, natively, with no
    # client library involved.
    payload = line.replace("\r", "")
    return f"id: {seq}\ndata: {payload}\n\n"


USER_HEADER_OK = re.compile(r"^[\w.@+-]{1,64}$")


def _who(request: Request) -> str:
    """Who to record against a run or an approval.

    An authenticating proxy in front of this sets one of these headers. The value is sanity-
    checked rather than trusted verbatim: it lands in an audit field that is meant to answer
    "who approved this test", and a 4 KB header or one full of markup would make that record
    useless or actively misleading.

    With nothing in front, a loopback caller is recorded as "local" rather than "you" — "you"
    reads as a name in an audit trail and is not one.
    """
    forwarded = (request.headers.get("x-forwarded-user")
                 or request.headers.get("x-remote-user") or "").strip()
    if forwarded and USER_HEADER_OK.match(forwarded):
        return forwarded
    if forwarded:
        log.warning("ignoring an implausible user header: %r", forwarded[:80])
    client = request.client.host if request.client else "unknown"
    return "local" if client in ("127.0.0.1", "::1") else client

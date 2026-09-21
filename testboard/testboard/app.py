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
from pathlib import Path

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
from .executor import KIND_DRIFT, KIND_MAP, KIND_PYTEST, Executor
from .streaming import LogBus

log = logging.getLogger("testboard.app")

HERE = Path(__file__).parent


def create_app(config: Config) -> FastAPI:
    database = Database(config.db_path)
    bus = LogBus()
    executor = Executor(config, database, bus)

    app = FastAPI(title=f"testboard — {config.repo_name}", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

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
        asyncio.create_task(inventory.refresh(config, database))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await executor.stop()
        database.close()

    # --- shared view context -------------------------------------------------------------
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
            "source_total": len(database.sources()),
            "last_upload": database.get_meta("last_upload") or [],
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
        counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "unknown": 0}
        for t in tests:
            row = latest.get(t["nodeid"])
            counts[row["outcome"] if row and row["outcome"] in counts else "unknown"] += 1
        return render(
            "overview.html", request,
            map_info=driftmod.map_info(config),
            drift=driftmod.drift_info(config),
            test_count=len(tests),
            counts=counts,
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
        return render("tests.html", request, by_file=by_file, markers=markers,
                      total=len(tests), never_batch=config.safety.never_batch)

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
            return JSONResponse(
                {"error": "confirmation required",
                 "markers": [m for m in markers if m in config.safety.confirm_before_run]},
                status_code=400,
            )

        result = executor.enqueue_pytest(
            target=target, label=label or target or "Whole suite",
            markers=markers, requested_by=_who(request),
        )
        if result.run_id is None:
            return RedirectResponse(f"/runs/{result.duplicate_of}", status_code=303)
        return RedirectResponse(f"/runs/{result.run_id}", status_code=303)

    @app.post("/jobs/{kind}")
    async def start_job(request: Request, kind: str):
        if kind not in (KIND_DRIFT, KIND_MAP):
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

    @app.post("/inventory/refresh")
    async def refresh_inventory():
        await inventory.refresh(config, database)
        return RedirectResponse("/tests", status_code=303)

    @app.post("/maintenance/sweep")
    async def manual_sweep():
        retention.sweep(config, database)
        return RedirectResponse("/", status_code=303)

    # --- recordings and transcripts ---------------------------------------------------------
    @app.get("/recordings", response_class=HTMLResponse)
    async def recordings_page(request: Request):
        rows = database.sources()
        runs = {}
        for row in rows:
            runs[row["id"]] = database.history(limit=5, target=f"source:{row['id']}")
        return render("recordings.html", request, sources=rows, runs=runs,
                      agent=agentmod.availability(),
                      discovered=media.discover(config))

    @app.get("/recordings/{source_id}", response_class=HTMLResponse)
    async def recording_page(request: Request, source_id: int):
        row = database.get_source(source_id)
        if row is None:
            return HTMLResponse("<h1>No such recording</h1>", status_code=404)
        cues = json.loads(row["transcript_cues"]) if row["transcript_cues"] else []
        return render("recording.html", request, source=row, cues=cues[:400],
                      cue_total=len(cues),
                      runs=database.history(limit=20, target=f"source:{source_id}"),
                      agent=agentmod.availability())

    @app.post("/recordings/upload")
    async def upload(request: Request, files: list[UploadFile] = File(...),
                     source_id: int | None = Form(None)):
        messages, target = [], source_id
        for upload_file in files:
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
                messages.append(f"{upload_file.filename}: {exc}")
            finally:
                await upload_file.close()

        database.set_meta("last_upload", messages)
        if target:
            return RedirectResponse(f"/recordings/{target}", status_code=303)
        return RedirectResponse("/recordings", status_code=303)

    @app.post("/recordings/import")
    async def import_discovered(request: Request, path: str = Form(...)):
        candidate = (config.repo_root / path).resolve()
        if config.repo_root not in candidate.parents or not candidate.is_file():
            return JSONResponse({"error": "not a file in this repository"}, status_code=400)

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
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))

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


def _who(request: Request) -> str:
    """Best effort. Enough to answer "who started this" when two people share a dev environment."""
    forwarded = request.headers.get("x-forwarded-user") or request.headers.get("x-remote-user")
    if forwarded:
        return forwarded
    client = request.client.host if request.client else "unknown"
    return "you" if client in ("127.0.0.1", "::1") else client

"""Accepting an uploaded recording or transcript.

Drop a file in and it is kept: the transcript's text goes into the database, the video goes on
disk with its digest recorded. Splitting them that way is deliberate —

* the transcript is small, it is the thing a generation run actually reads, and having it in the
  database means a run needs nothing from the filesystem;
* the video is tens or hundreds of megabytes, nothing ever queries its bytes, and putting it in
  SQLite would bloat every backup and every VACUUM of a database whose whole value is being small
  enough to keep forever.

Uploads are streamed to disk in chunks rather than read into memory, because a screen recording is
routinely larger than the process should ever hold at once.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import transcripts
from .config import Config
from .db import Database

log = logging.getLogger("testboard.media")

CHUNK = 1024 * 1024
MAX_VIDEO_MB = 2048
UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


def safe_name(name: str) -> str:
    """A filename that cannot escape its directory or surprise a shell.

    The upload names the file, and the upload is not trusted: path separators, `..` and control
    characters all go. The original is kept in the database for display.
    """
    cleaned = UNSAFE.sub("_", Path(name).name).strip() or "upload"
    return cleaned[:150]


@dataclass
class Accepted:
    source_id: int
    kind: str                  # video | transcript
    message: str
    duplicate: bool = False


def media_dir(config: Config, source_id: int) -> Path:
    return config.state_dir / "media" / str(source_id)


async def accept(config: Config, database: Database, *, filename: str, stream,
                 uploaded_by: str, source_id: int | None = None) -> Accepted:
    """Store one uploaded file, creating or updating a source."""
    name = safe_name(filename)

    if transcripts.is_transcript(name):
        return await _accept_transcript(config, database, name, stream, uploaded_by, source_id)
    if transcripts.is_video(name):
        return await _accept_video(config, database, name, stream, uploaded_by, source_id)

    raise ValueError(
        f"{name}: not a recording or a transcript. Recordings: "
        f"{', '.join(transcripts.VIDEO_SUFFIXES)}. Transcripts: "
        f"{', '.join(transcripts.SUPPORTED)}."
    )


def _title_from(name: str) -> str:
    stem = Path(name).stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip()[:120] or name


async def _accept_video(config: Config, database: Database, name: str, stream,
                        uploaded_by: str, source_id: int | None) -> Accepted:
    digest = hashlib.sha256()
    size = 0
    scratch = config.state_dir / "media" / f".incoming-{name}"
    scratch.parent.mkdir(parents=True, exist_ok=True)

    with scratch.open("wb") as fh:
        while True:
            chunk = await stream.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_VIDEO_MB * 1024 * 1024:
                fh.close()
                scratch.unlink(missing_ok=True)
                raise ValueError(f"{name} is larger than the {MAX_VIDEO_MB} MB limit")
            digest.update(chunk)
            fh.write(chunk)

    sha = digest.hexdigest()

    # The same recording uploaded twice is the same recording. Attaching rather than duplicating
    # keeps a source's transcript and its video together when they arrive in either order.
    existing = database.source_by_video_digest(sha)
    if existing and source_id is None:
        scratch.unlink(missing_ok=True)
        return Accepted(existing["id"], "video",
                        f"that recording is already here as “{existing['title']}”", duplicate=True)

    if source_id is None:
        source_id = database.create_source(_title_from(name), uploaded_by)

    target_dir = media_dir(config, source_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    scratch.replace(target)

    database.update_source(
        source_id,
        video_name=name,
        video_path=str(target.relative_to(config.state_dir).as_posix()),
        video_bytes=size,
        video_sha256=sha,
    )
    return Accepted(source_id, "video", f"{name} stored ({size // (1024 * 1024)} MB)")


async def _accept_transcript(config: Config, database: Database, name: str, stream,
                             uploaded_by: str, source_id: int | None) -> Accepted:
    raw = b""
    while True:
        chunk = await stream.read(CHUNK)
        if not chunk:
            break
        raw += chunk
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError(f"{name} is implausibly large for a transcript")

    if source_id is None:
        source_id = database.create_source(_title_from(name), uploaded_by)

    # .docx has to be parsed from a real file: it is a zip, and zipfile wants a seekable path.
    target_dir = media_dir(config, source_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_bytes(raw)

    parsed = transcripts.parse(target, raw)

    import json
    database.update_source(
        source_id,
        transcript_name=name,
        transcript_text=parsed.text,
        transcript_cues=json.dumps(parsed.cues),
        transcript_bytes=len(raw),
        has_timestamps=int(parsed.has_timestamps),
    )
    if parsed.duration:
        row = database.get_source(source_id)
        if row and not row["video_duration"]:
            database.update_source(source_id, video_duration=parsed.duration)

    detail = f"{len(parsed.cues)} cue(s)"
    if not parsed.has_timestamps:
        detail += " — no usable timestamps"
    return Accepted(source_id, "transcript", f"{name} read, {detail}")


def delete(config: Config, database: Database, source_id: int) -> None:
    import shutil
    shutil.rmtree(media_dir(config, source_id), ignore_errors=True)
    database.delete_source(source_id)


def discover(config: Config) -> list[Path]:
    """Recordings and transcripts already sitting in the repository, not yet registered.

    Teams drops these into an `assets/` or `recordings/` folder and they stay there. Offering to
    adopt what is already present beats asking someone to re-upload a file they already have.
    """
    found: list[Path] = []
    for folder in ("assets", "recordings", "media", "docs"):
        base = config.repo_root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and (transcripts.is_video(path.name)
                                   or path.suffix.lower() in (".vtt", ".srt", ".docx")):
                found.append(path)
    return found

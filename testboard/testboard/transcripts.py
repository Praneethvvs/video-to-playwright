"""Read a transcript into ordered, timestamped utterances.

Handles `.vtt`, `.srt`, `.docx` and `.txt` with no third-party dependency — a `.docx` is a zip of
XML, so it is unpacked directly rather than pulling in python-docx for one file format.

This mirrors the skill's `scripts/read_transcript.py`, which exists as a CLI for an agent to call.
The logic lives here too so that an upload can be parsed in-process, without the server shelling
out to a script that may or may not be present in the repository it is serving.

The distinction that matters downstream: **whether the transcript has per-cue timestamps.** A Teams
`.vtt` does; the `.docx` export of the same meeting usually collapses to a single block with one
timestamp. With timestamps, narration can be aligned to video frames. Without them, alignment is by
content alone — slower, approximate, and the main source of questions a human has to answer.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

# The hours field is optional in the WebVTT spec, and ffmpeg, Whisper and several meeting
# exporters omit it for anything under an hour: `00:01.000 --> 00:04.000`. Requiring HH meant a
# perfectly valid .vtt matched no cues at all, fell through to plain-line parsing, and handed the
# agent a "transcript" containing the WEBVTT header, the cue numbers and the --> arrows — while
# the page blamed the user's export format for having no timestamps.
CUE_TIME = re.compile(
    r"((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3})"
)
SPEAKER = re.compile(r"^<v\s+([^>]+)>(.*)$")
SUPPORTED = (".vtt", ".srt", ".docx", ".txt", ".md")


@dataclass
class Transcript:
    cues: list[dict]
    text: str
    has_timestamps: bool
    note: str = ""

    @property
    def duration(self) -> float | None:
        stamps = [c["end_seconds"] for c in self.cues if c.get("end_seconds") is not None]
        return max(stamps) if stamps else None


def _seconds(stamp: str) -> float | None:
    """Seconds from `HH:MM:SS.mmm` or `MM:SS.mmm`, both of which appear in real cue files."""
    try:
        parts = stamp.split(":")
        if len(parts) == 2:
            parts = ["0", *parts]
        hours, minutes, rest = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(rest.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _from_cue_format(text: str) -> list[dict]:
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        match = CUE_TIME.search(block)
        if not match:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        body, speaker = [], None
        for line in lines:
            if CUE_TIME.search(line) or line.isdigit() or line.upper() == "WEBVTT":
                continue
            voiced = SPEAKER.match(line)
            if voiced:
                speaker = voiced.group(1).strip()
                line = voiced.group(2)
            line = re.sub(r"</?v[^>]*>", "", line).strip()
            if line:
                body.append(line)
        if body:
            cues.append({
                "start": match.group(1), "end": match.group(2),
                "start_seconds": _seconds(match.group(1)),
                "end_seconds": _seconds(match.group(2)),
                "speaker": speaker, "text": " ".join(body),
            })
    return cues


WORD_PARA = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.S)
# Text runs and line breaks, in document order. A <w:br/> sits *between* runs rather than inside
# one, so it has to be matched in the same pass or every spoken line in a paragraph runs together
# into "...obsolescence case.So the prerequisite is...".
WORD_RUN = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|(<w:br\s*/>)", re.S)
# Teams writes the speaker and offset at the head of the utterance, in the same paragraph as the
# first line of speech: "Jane Doe   0:10So this recording is for...".
# NOT re.DOTALL. With it, `.{0,58}?` crosses a newline, so the speaker name swallows the first
# line of speech whenever the header sits on its own line. The name is therefore matched without
# newlines, and only the body is allowed to span them.
TEAMS_HEADER = re.compile(
    r"^(?P<who>[^\d\n]{1,58}?)[ \t]{2,}(?P<at>\d{1,2}:\d{2}(?::\d{2})?)(?P<rest>[\s\S]*)$"
)


def _normalise_offset(stamp: str) -> str:
    """`0:10` and `1:02:03` both become `HH:MM:SS`, so one parser handles every source."""
    parts = stamp.split(":")
    if len(parts) == 2:
        parts = ["0", *parts]
    return ":".join(p.zfill(2) for p in parts)


def _unescape(text: str) -> str:
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&apos;", "'"), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


def _from_docx(path: Path) -> list[dict]:
    """Pull paragraphs out of a .docx without a third-party dependency.

    Only the text inside <w:t> elements is taken. Stripping every tag instead looks simpler and is
    wrong: Word stores revision ids and table geometry as attributes and bare numbers elsewhere in
    the document, and those end up concatenated onto the front of real sentences.

    Teams puts the speaker and an offset on their own line ("Jane Doe   0:10"). That is a real
    timestamp, just not in cue format, so it is lifted onto the utterance that follows it. It
    makes a .docx export far more useful than the "one undated block" it first appears to be —
    though a .vtt is still better, because its times bracket each line rather than starting it.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return []

    paragraphs: list[str] = []
    for block in WORD_PARA.findall(xml):
        pieces = []
        for text, brk in WORD_RUN.findall(block):
            pieces.append("\n" if brk else text)
        joined = _unescape("".join(pieces)).strip()
        if joined:
            paragraphs.append(joined)

    cues: list[dict] = []
    speaker: str | None = None
    for paragraph in paragraphs:
        stamp = None
        header = TEAMS_HEADER.match(paragraph)
        if header:
            speaker = header.group("who").strip()
            stamp = header.group("at")
            paragraph = header.group("rest")

        pending = stamp
        for line in (ln.strip() for ln in paragraph.split("\n")):
            if not line:
                continue
            # Only the first *non-empty* line carries the offset. Counting enumerate() instead
            # loses it whenever the header is followed by a blank run, which is most of the time.
            # The rest of the utterance follows it in time, but Teams never says when, and an
            # invented timestamp is worse than an absent one.
            start = _normalise_offset(pending) if pending else None
            pending = None
            cues.append({
                "start": start, "end": None,
                "start_seconds": _seconds(f"{start}.000") if start else None,
                "end_seconds": None, "speaker": speaker, "text": line,
            })
    return cues


def _from_plain(text: str) -> list[dict]:
    return [{"start": None, "end": None, "start_seconds": None, "end_seconds": None,
             "speaker": None, "text": ln.strip()}
            for ln in text.splitlines() if ln.strip()]


def parse(path: Path, raw: bytes | None = None) -> Transcript:
    suffix = path.suffix.lower()

    cue_format_failed = False
    if suffix == ".docx":
        cues = _from_docx(path)
    else:
        text = (raw if raw is not None else path.read_bytes()).decode("utf-8", errors="replace")
        cues = _from_cue_format(text) if suffix in (".vtt", ".srt") else []
        if not cues and suffix in (".vtt", ".srt"):
            # Do not quietly fall through to plain lines. A cue file that parsed to nothing is a
            # file we failed to read, and treating its raw text as narration puts the WEBVTT
            # header, the cue numbers and the --> arrows into the agent's prompt as though a
            # human had said them.
            cue_format_failed = True
        if not cues:
            cues = _from_plain(text)

    timed = sum(1 for c in cues if c.get("start_seconds") is not None)
    # A quarter of the cues carrying a time is enough to align narration to video. The floor is
    # one, not two: a short .vtt with a single fully-timed cue is timestamped, and calling it
    # otherwise produced a warning that blamed a Teams .docx export for a perfectly good file.
    has_timestamps = bool(cues) and timed >= max(1, len(cues) // 4)

    note = ""
    if cue_format_failed:
        # Blaming the export format would be wrong and misleading here: this IS the .vtt, and we
        # could not read it.
        note = (f"This looked like a cue file but no cues could be read from it, so its raw lines "
                f"are being shown instead. That usually means an unexpected dialect of "
                f"{suffix} — check the file before generating anything from it.")
    elif not has_timestamps:
        note = ("Almost no per-cue timestamps, which is typical of a Teams .docx export. Ask for "
                "the .vtt if there is one: without times, narration can only be matched to the "
                "video by content, which is slower and less certain.")

    lines = []
    for cue in cues:
        prefix = f"[{cue['start']}] " if cue.get("start") else ""
        who = f"{cue['speaker']}: " if cue.get("speaker") else ""
        lines.append(f"{prefix}{who}{cue['text']}")

    return Transcript(cues=cues, text="\n".join(lines), has_timestamps=has_timestamps, note=note)


def is_transcript(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED


VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi")


def is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_SUFFIXES

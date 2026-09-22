"""Read a meeting/screen-recording transcript into ordered, timestamped utterances.

Supports `.vtt`, `.srt`, `.docx`, `.txt` and `.md`, with no third-party dependencies — a `.docx` is
just a zip of XML, so it is parsed directly rather than requiring python-docx.

**Ask for the `.vtt` if you were given a `.docx`.** Teams exports both. A `.vtt` brackets each line
with a start and an end time, which is what lets narration be aligned to video frames. A `.docx`
carries only the offset Teams prints at the head of each utterance — better than nothing, and this
script does lift it, but still one number per speaker turn rather than a range per line.

Usage:
    python read_transcript.py <transcript> [--json] [--classify]

Every parsing rule below was written against a real file that broke the previous one. The notes
say which, because each looks like an arbitrary complication until you meet the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

SUPPORTED = (".vtt", ".srt", ".docx", ".txt", ".md")

# Phrases that signal each kind of content. Narrators are remarkably consistent about these, which
# makes a crude keyword pass genuinely useful as a first cut — but it is a triage aid, not a parser.
EXPECTATION_MARKERS = (
    "you should see", "you will see", "should not be able", "you won't be able",
    "i see", "notice that", "you should", "expect", "verify", "check that", "confirm",
)
PRECONDITION_MARKERS = ("prerequisite", "before you", "you have a", "already been", "assuming")
RULE_MARKERS = ("must", "cannot", "can't", "not able", "only", "always", "never", "takes precedence")

# The hours field is optional in the WebVTT spec, and ffmpeg, Whisper and several meeting
# exporters omit it for anything under an hour: `00:01.000 --> 00:04.000`. Requiring HH meant a
# perfectly valid .vtt matched no cues at all, fell through to plain-line parsing, and produced a
# "transcript" containing the WEBVTT header, the cue numbers and the --> arrows — while the
# warning blamed the user's export format for having no timestamps.
CUE_TIME = re.compile(
    r"((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3})"
)
SPEAKER = re.compile(r"^<v\s+([^>]+)>(.*)$")

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


def _with_fraction(stamp: str | None) -> str | None:
    """`00:00:10` becomes `00:00:10.000`; anything already fractional is left alone.

    Both separators count as fractional. Testing for "." only meant an SRT stamp — which uses a
    comma, `00:00:01,000` — had `.000` appended to it, and the result parsed to nothing. The cue
    text came through fine, so the file looked parsed and merely untimed.
    """
    if not stamp:
        return None
    return stamp if ("." in stamp or "," in stamp) else f"{stamp}.000"


def _cue(start, end, speaker, text) -> dict:
    return {
        "start": start, "end": end,
        "start_seconds": _seconds(_with_fraction(start)) if start else None,
        "end_seconds": _seconds(_with_fraction(end)) if end else None,
        "speaker": speaker, "text": text,
    }


def _from_cue_format(text: str) -> list[dict]:
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        match = CUE_TIME.search(block)
        if not match:
            continue
        body, speaker = [], None
        for line in (ln.strip() for ln in block.splitlines() if ln.strip()):
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
            cues.append(_cue(match.group(1), match.group(2), speaker, " ".join(body)))
    return cues


def _from_docx(path: Path) -> list[dict]:
    """Pull paragraphs out of a .docx without a third-party dependency.

    Only the text inside <w:t> elements is taken. Stripping every tag instead looks simpler and is
    wrong: Word stores revision ids and table geometry as attributes and bare numbers elsewhere in
    the document, and those end up concatenated onto the front of real sentences.

    Teams puts the speaker and an offset at the head of each turn ("Jane Doe   0:10"). That is
    a real timestamp, just not in cue format, so it is lifted onto the utterance that follows.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return []

    paragraphs: list[str] = []
    for block in WORD_PARA.findall(xml):
        pieces = ["\n" if brk else text for text, brk in WORD_RUN.findall(block)]
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
            # The rest of the turn follows it in time, but Teams never says when, and an invented
            # timestamp is worse than an absent one.
            start = _normalise_offset(pending) if pending else None
            pending = None
            cues.append(_cue(start, None, speaker, line))
    return cues


def _from_plain(text: str) -> list[dict]:
    return [_cue(None, None, None, ln.strip()) for ln in text.splitlines() if ln.strip()]


def classify(text: str) -> list[str]:
    low = text.lower()
    tags = []
    if any(m in low for m in PRECONDITION_MARKERS):
        tags.append("precondition")
    if any(m in low for m in EXPECTATION_MARKERS):
        tags.append("expectation")
    if any(m in low for m in RULE_MARKERS):
        tags.append("rule")
    return tags or ["action"]


def read(path: Path) -> dict:
    """Parse a transcript. Returns the cues, whether they are usefully timestamped, and a note.

    The note is the part that matters: it distinguishes "this export has no times" from "this is a
    cue file we failed to read", which look identical in a list of untimed lines.
    """
    suffix = path.suffix.lower()
    cue_format_failed = False

    if suffix == ".docx":
        cues = _from_docx(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        cues = _from_cue_format(text) if suffix in (".vtt", ".srt") else []
        if not cues and suffix in (".vtt", ".srt"):
            # Do not quietly fall through to plain lines. A cue file that parsed to nothing is a
            # file we failed to read, and treating its raw text as narration puts the WEBVTT
            # header, the cue numbers and the --> arrows into the prompt as though a human had
            # said them.
            cue_format_failed = True
        if not cues:
            cues = _from_plain(text)

    timed = sum(1 for c in cues if c.get("start_seconds") is not None)
    # A quarter of the cues carrying a time is enough to align narration to video. The floor is
    # one, not two: a short .vtt with a single fully-timed cue is timestamped, and calling it
    # otherwise produced a warning blaming a Teams export for a perfectly good file.
    has_timestamps = bool(cues) and timed >= max(1, len(cues) // 4)

    note = ""
    if cue_format_failed:
        # Blaming the export format would be wrong here: this IS the .vtt, and we could not read it.
        note = (f"This looked like a cue file but no cues could be read from it, so its raw lines "
                f"are shown instead. That usually means an unexpected dialect of {suffix} — check "
                f"the file before generating anything from it.")
    elif not has_timestamps:
        note = ("Almost no per-cue timestamps, which is typical of a Teams .docx export. Ask for "
                "the .vtt if there is one: without times, narration can only be matched to the "
                "video by content, which is slower and less certain.")

    return {"utterances": cues, "timestamped": timed, "has_timestamps": has_timestamps,
            "note": note}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transcript")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--classify", action="store_true", help="tag each utterance by likely kind")
    args = ap.parse_args()

    path = Path(args.transcript)
    if not path.exists():
        print(f"File not found: {path}")
        return 1
    if path.suffix.lower() not in SUPPORTED:
        print(f"Unsupported transcript type {path.suffix!r}. Supported: {', '.join(SUPPORTED)}")
        return 1

    result = read(path)
    cues = result["utterances"]
    for c in cues:
        c["tags"] = classify(c["text"])

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    speakers = sorted({c["speaker"] for c in cues if c["speaker"]})
    print(f"{len(cues)} utterances, {result['timestamped']} with timestamps"
          + (f", {len(speakers)} speaker(s): {', '.join(speakers)}" if speakers else "")
          + "\n")
    if result["note"]:
        print(f"NOTE  {result['note']}\n")

    if args.classify:
        for kind in ("precondition", "expectation", "rule"):
            hits = [c for c in cues if kind in c["tags"]]
            if not hits:
                continue
            print(f"--- {kind.upper()} ({len(hits)}) " + "-" * 40)
            for c in hits:
                stamp = f"[{c['start']}] " if c["start"] else ""
                who = f"{c['speaker']}: " if c["speaker"] else ""
                print(f"{stamp}{who}{c['text'][:200]}")
            print()
        print("Everything else is narration of actions. Read it in order for the step list.")
    else:
        for c in cues:
            stamp = f"[{c['start']}] " if c["start"] else ""
            who = f"{c['speaker']}: " if c["speaker"] else ""
            print(f"{stamp}{who}{c['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

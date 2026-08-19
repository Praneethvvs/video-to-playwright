"""Read a meeting/screen-recording transcript into ordered, timestamped utterances.

Supports `.vtt`, `.srt`, `.docx` and `.txt`, with no third-party dependencies — `.docx` is just a zip
containing XML, so it is parsed directly rather than requiring python-docx.

**Ask for `.vtt` if you were given `.docx`.** Teams exports both. The `.docx` typically collapses the
whole session into one speaker block with a single timestamp, while the `.vtt` carries per-cue times
that let narration be aligned to video frames. Without timestamps, alignment is by content only —
slow, approximate, and the main reason ambiguities have to go to a human.

Usage:
    python read_transcript.py <transcript> [--json] [--classify]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

# Phrases that signal each kind of content. Narrators are remarkably consistent about these, which
# makes a crude keyword pass genuinely useful as a first cut — but it is a triage aid, not a parser.
EXPECTATION_MARKERS = (
    "you should see", "you will see", "should not be able", "you won't be able",
    "i see", "notice that", "you should", "expect", "verify", "check that", "confirm",
)
PRECONDITION_MARKERS = ("prerequisite", "before you", "you have a", "already been", "assuming")
RULE_MARKERS = ("must", "cannot", "can't", "not able", "only", "always", "never", "takes precedence")


def _from_vtt_or_srt(text: str) -> list[dict]:
    cues: list[dict] = []
    time_re = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
    )
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        m = time_re.search(block)
        if not m:
            continue
        lines = [
            ln.strip()
            for ln in block.splitlines()
            if ln.strip() and not time_re.search(ln) and not ln.strip().isdigit()
        ]
        if not lines:
            continue
        body = " ".join(lines)
        speaker = None
        if sm := re.match(r"^<v\s+([^>]+)>", body):
            speaker = sm.group(1)
            body = body[sm.end():]
        body = re.sub(r"</?v[^>]*>", "", body).strip()
        cues.append({"start": m.group(1), "speaker": speaker, "text": body})
    return cues


def _from_docx(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)
    unescape = lambda s: (  # noqa: E731
        s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'")
    )
    runs = [unescape(r).strip() for r in runs]
    runs = [r for r in runs if r]

    cues: list[dict] = []
    pending_time = None
    for run in runs:
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", run):
            pending_time = run
            continue
        # Long runs are utterances; short ones are usually speaker names or headers.
        if len(run) > 40:
            cues.append({"start": pending_time, "speaker": None, "text": run})
            pending_time = None
        elif not cues:
            continue
    return cues


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


def read(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".vtt", ".srt"):
        return _from_vtt_or_srt(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".docx":
        return _from_docx(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        {"start": None, "speaker": None, "text": ln.strip()}
        for ln in re.split(r"(?<=[.!?])\s+|\n", text)
        if ln.strip()
    ]


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

    cues = read(path)
    for c in cues:
        c["tags"] = classify(c["text"])

    timestamped = sum(1 for c in cues if c["start"])

    if args.json:
        print(json.dumps({"utterances": cues, "timestamped": timestamped}, indent=2))
        return 0

    print(f"{len(cues)} utterances, {timestamped} with timestamps\n")
    if timestamped <= 1 and len(cues) > 5:
        print(
            "WARNING  Almost no timestamps. This is typical of a Teams .docx export.\n"
            "         Ask for the .vtt — it has per-cue times, which is the difference between\n"
            "         aligning narration to frames automatically and doing it by hand.\n"
        )

    if args.classify:
        for kind in ("precondition", "expectation", "rule"):
            hits = [c for c in cues if kind in c["tags"]]
            if not hits:
                continue
            print(f"--- {kind.upper()} ({len(hits)}) " + "-" * 40)
            for c in hits:
                stamp = f"[{c['start']}] " if c["start"] else ""
                print(f"{stamp}{c['text'][:200]}")
            print()
        print("Everything else is narration of actions. Read it in order for the step list.")
    else:
        for c in cues:
            stamp = f"[{c['start']}] " if c["start"] else ""
            print(f"{stamp}{c['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

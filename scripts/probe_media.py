"""Probe a screen recording before planning any work around it.

Two things decide how much a recording is worth, and both are cheap to measure and expensive to
assume:

* **Resolution and bitrate** — a heavily compressed capture of a dense UI is unreadable, and no
  amount of frame extraction fixes it.
* **Whether the audio track carries any activity** — recordings routinely ship with a silent track.
  Discovering that after building a plan around "the narration will tell us" costs real time, because
  without narration every assertion has to come from a human review instead.

This is an **audio-activity heuristic, not speech detection**. It measures loudness and counts
stretches above a noise floor, so it cannot distinguish narration from a UI chime, music, or a noisy
keyboard. Read `SILENT` as reliable — nothing is there — and `NARRATED` as "worth listening to, and
worth asking for the transcript", not as confirmation that anyone spoke.

Usage:
    python probe_media.py <video> [--noise-floor -45]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _ffmpeg import run


def probe(path: Path, speech_threshold_db: float) -> dict:
    info = run(["-i", str(path)])

    out: dict = {"file": str(path), "exists": path.exists()}
    if not path.exists():
        return out

    out["size_mb"] = round(path.stat().st_size / (1024 * 1024), 2)

    if m := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", info):
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        out["duration_seconds"] = round(h * 3600 + mnt * 60 + s, 1)
        out["duration"] = f"{h * 60 + mnt:d}m{int(s):02d}s"

    # Build the dict unconditionally: the fps clause is the most fragile part of the pattern, and
    # letting a partial match leave `out["video"]` undefined made the bitrate line raise KeyError.
    out["video"] = {}
    if m := re.search(r"Video:\s*([^,]+)", info):
        out["video"]["codec"] = m.group(1).strip()
    if m := re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", info, re.S):
        out["video"]["width"] = int(m.group(1))
        out["video"]["height"] = int(m.group(2))
    if m := re.search(r"Video:.*?(\d+(?:\.\d+)?)\s*fps", info, re.S):
        out["video"]["fps"] = float(m.group(1))
    if m := re.search(r"Video:.*?(\d+)\s*kb/s", info):
        out["video"]["bitrate_kbps"] = int(m.group(1))

    out["has_audio_stream"] = bool(re.search(r"Audio:", info))

    if out["has_audio_stream"]:
        vol = run(["-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"])
        mean = re.search(r"mean_volume:\s*(-?\d+\.?\d*) dB", vol)
        peak = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", vol)
        out["audio"] = {
            "mean_volume_db": float(mean.group(1)) if mean else None,
            "max_volume_db": float(peak.group(1)) if peak else None,
        }

        # Count stretches above the noise floor. A track with narration has many; a track with a
        # single UI chime has one or two.
        sil = run(
            [
                "-i", str(path),
                "-af", f"silencedetect=noise={speech_threshold_db}dB:d=0.6",
                "-vn", "-f", "null", "-",
            ]
        )
        gaps = len(re.findall(r"silence_end", sil))
        out["audio"]["non_silent_segments"] = gaps
        out["audio"]["noise_floor_db"] = speech_threshold_db

        # The peak test runs first and overrides the segment count, because a track can register a
        # segment boundary while still being inaudible. Saying so explicitly avoids the confusing
        # combination of "1 non-silent segment" and a SILENT verdict.
        peak_db = out["audio"]["max_volume_db"]
        if peak_db is None or peak_db < -80:
            verdict = (
                f"SILENT — peak {peak_db} dB is inaudible, so the {gaps} detected segment(s) are "
                "artefacts. No narration; every expected result must come from a human."
            )
        elif gaps <= 2:
            verdict = (
                f"NEARLY SILENT — {gaps} stretch(es) above {speech_threshold_db} dB. "
                "Usually a UI chime rather than narration; listen before relying on it."
            )
        else:
            verdict = (
                f"AUDIO PRESENT — {gaps} stretches above {speech_threshold_db} dB. "
                "Likely narration, but this is a loudness heuristic, not speech detection. "
                "Listen to a sample, and ask for the transcript."
            )
        out["audio"]["verdict"] = verdict

    warnings = []
    v = out.get("video", {})
    if v.get("height", 0) < 900:
        warnings.append(
            f"Low resolution ({v.get('width')}x{v.get('height')}) — small UI text may be unreadable."
        )
    if v.get("bitrate_kbps", 10_000) < 500 and v.get("height", 0) >= 900:
        warnings.append(
            f"Low bitrate ({v.get('bitrate_kbps')} kb/s) for this resolution — expect soft text. "
            "Narration, if present, matters more than the frames here."
        )
    if not out.get("has_audio_stream"):
        warnings.append("No audio stream — assertions must come from human review.")
    out["warnings"] = warnings

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument(
        "--noise-floor",
        "--speech-threshold",
        dest="speech_threshold",
        type=float,
        default=-45.0,
        help="dB floor below which audio counts as silence (default: -45)",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args()

    result = probe(Path(args.video), args.speech_threshold)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if not result.get("exists"):
        print(f"File not found: {args.video}")
        return 1

    v = result.get("video", {})
    print(f"File       {Path(result['file']).name}  ({result['size_mb']} MB)")
    print(f"Duration   {result.get('duration', '?')}")
    print(f"Video      {v.get('width')}x{v.get('height')} @ {v.get('fps')}fps, "
          f"{v.get('bitrate_kbps', '?')} kb/s, {v.get('codec', '?')}")
    if a := result.get("audio"):
        print(f"Audio      mean {a['mean_volume_db']} dB, peak {a['max_volume_db']} dB, "
              f"{a['non_silent_segments']} non-silent segment(s)")
        print(f"           -> {a['verdict']}")
    else:
        print("Audio      none")

    for w in result.get("warnings", []):
        print(f"WARNING    {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

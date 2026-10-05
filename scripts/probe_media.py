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


def audio_verdict(*, peak_db: float | None, mean_db: float | None,
                  silences: int, threshold_db: float) -> str:
    """Decide whether a track plausibly carries narration.

    Loudness decides this, not the number of pauses. A pure pause count is the inverse of what it
    looks like: `silencedetect` emits one event per *silent* stretch, so a track that is loud from
    the first frame to the last emits none at all — and scoring that as zero speech reported the
    loudest possible recording as the quietest.

    Separate from probe() so it can be tested without ffmpeg or a media file.
    """
    if peak_db is None or peak_db < -80:
        return (f"SILENT — peak {peak_db} dB is inaudible. No narration; every expected result "
                "must come from a human.")
    if mean_db is None or mean_db < threshold_db:
        return (f"NEARLY SILENT — mean {mean_db} dB is below the {threshold_db} dB floor, though "
                f"it peaks at {peak_db} dB. Usually a UI chime rather than narration; listen "
                "before relying on it.")
    return (f"AUDIO PRESENT — mean {mean_db} dB, above the {threshold_db} dB floor, in "
            f"{silences + 1} audible run(s). Likely narration, but this is a loudness heuristic "
            "and not speech detection. Listen to a sample, and ask for the transcript.")


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

        # `silencedetect` reports SILENCES, not sound. Each `silence_end` marks the end of one
        # quiet stretch, so the count says how often the speaker paused — it is not a count of
        # speech.
        #
        # Counting those and calling them "non-silent segments" inverted the whole test. A track
        # that is loud from the first frame to the last emits *zero* silence events, scored 0, and
        # was reported NEARLY SILENT — the loudest possible recording classified as the quietest.
        # It survived because narration with ordinary pauses does generate many events, which is
        # the one case it was built against.
        #
        # So loudness decides the verdict, and the pauses are only ever supporting detail.
        sil = run(
            [
                "-i", str(path),
                "-af", f"silencedetect=noise={speech_threshold_db}dB:d=0.6",
                "-vn", "-f", "null", "-",
            ]
        )
        silences = len(re.findall(r"silence_end", sil))
        out["audio"]["silent_stretches"] = silences
        # n silences cut the track into at most n+1 audible runs. With no silences at all the
        # whole track is one continuous run, which is the case that used to score zero.
        out["audio"]["audible_runs"] = silences + 1
        out["audio"]["noise_floor_db"] = speech_threshold_db

        out["audio"]["verdict"] = audio_verdict(
            peak_db=out["audio"]["max_volume_db"],
            mean_db=out["audio"]["mean_volume_db"],
            silences=silences,
            threshold_db=speech_threshold_db,
        )

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
              f"{a['silent_stretches']} pause(s)")
        print(f"           -> {a['verdict']}")
    else:
        print("Audio      none")

    for w in result.get("warnings", []):
        print(f"WARNING    {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

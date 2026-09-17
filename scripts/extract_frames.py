"""Extract a readable, timestamped set of frames from a screen recording.

Two sampling strategies are used together because each misses what the other catches:

* **Scene-change detection** catches the moments that matter — a dialog opening, a page changing —
  but misses long stretches where the only change is typing, a spinner, or a slowly filling grid. In
  practice it produced frames for the first 78 seconds of a 212-second recording and nothing after.
* **Uniform time sampling** guarantees coverage but misses fast transitions between samples.

The scene threshold is auto-tuned, because the right value depends entirely on the recording: UI
changes are often local (a dropdown opening moves ~5% of pixels), so a threshold tuned for video
content finds almost nothing.

Usage:
    python extract_frames.py <video> --out frames/ [--target 35] [--uniform-every 15]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from _ffmpeg import run

CANDIDATE_THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.12]


def count_scene_frames(video: Path, threshold: float) -> int:
    out = run([
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})'",
        "-fps_mode", "vfr", "-f", "null", "-", "-stats",
    ])
    hits = re.findall(r"frame=\s*(\d+)", out)
    return int(hits[-1]) if hits else 0


def pick_threshold(video: Path, target: int) -> float:
    """Choose the threshold whose frame count lands closest to `target`."""
    best, best_delta, best_count = CANDIDATE_THRESHOLDS[0], None, 0
    for t in CANDIDATE_THRESHOLDS:
        n = count_scene_frames(video, t)
        delta = abs(n - target)
        print(f"  scene>{t}: {n} frames", file=sys.stderr)
        if best_delta is None or delta < best_delta:
            best, best_delta, best_count = t, delta, n
        if n < target // 3:  # counts only shrink as the threshold rises
            break
    print(f"  -> using scene>{best} ({best_count} frames)", file=sys.stderr)
    return best


def duration_seconds(video: Path) -> float | None:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", run(["-i", str(video)]))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def find_gaps(times: list[float], total: float | None, min_gap: float) -> list[dict]:
    """Stretches with no scene change.

    The video's start and end are boundaries, not just the frames themselves. Missing that hid the
    most interesting gap in a real recording: the last scene change was at 78s of a 212s video, and
    the untouched 134-second tail was where a long-running job ran. Trailing gaps are common,
    because a flow that ends by waiting produces no further scene changes at all.
    """
    edges = [0.0, *sorted(times)]
    if total:
        edges.append(total)
    gaps = []
    for a, b in zip(edges, edges[1:]):
        if b - a > min_gap:
            gaps.append({"from": round(a, 1), "to": round(b, 1), "seconds": round(b - a, 1)})
    return gaps


def extract(video: Path, out_dir: Path, target: int, uniform_every: int, width: int,
            min_gap: float) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    scene_dir = out_dir / "scene"
    uni_dir = out_dir / "uniform"

    # Clear before writing. These directories are pure derived output, and the index is built by
    # zipping globbed filenames against freshly captured timestamps — so a single stale frame from an
    # earlier run with a different video or threshold silently shifts every timestamp after it, and
    # the resulting frames.json looks perfectly valid while being wrong.
    for d in (scene_dir, uni_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    # Native resolution by default — see --width. Downscaling defeats the purpose of reading UI text.
    scale = f",scale={width}:-1" if width else ""

    print("Tuning scene threshold...", file=sys.stderr)
    threshold = pick_threshold(video, target)

    # showinfo prints pts_time for each kept frame, giving real timestamps rather than guesses.
    log = run([
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo{scale}",
        "-fps_mode", "vfr", str(scene_dir / "scene_%03d.png"),
    ])
    times = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", log)]
    scene_files = sorted(scene_dir.glob("scene_*.png"))

    run([
        "-i", str(video),
        "-vf", f"fps=1/{uniform_every}{scale}",
        str(uni_dir / "uniform_%03d.png"),
    ])
    uni_files = sorted(uni_dir.glob("uniform_*.png"))

    total = duration_seconds(video)

    # Always capture the final moment. Uniform sampling lands on multiples of the interval, so the
    # tail is routinely uncovered — and the tail is exactly where a long-running job finishes. This
    # skill warns about that gap, so the tooling should not create it.
    tail_t = None
    if total and total > 1:
        tail_t = round(total - 0.5, 2)
        tail = uni_dir / "uniform_end.png"
        args = ["-ss", str(tail_t), "-i", str(video)]
        if scale:
            args += ["-vf", scale.lstrip(",")]
        args += ["-frames:v", "1", "-y", str(tail)]
        run(args)
        if tail.exists():
            uni_files = [f for f in uni_files if f.name != "uniform_end.png"] + [tail]

    index = {
        "video": str(video),
        "duration_seconds": round(total, 1) if total else None,
        "scene_threshold": threshold,
        "frame_width": width,
        "scene_frames": [
            {"file": f"scene/{f.name}", "t": round(t, 2)}
            for f, t in zip(scene_files, times)
        ],
        "uniform_frames": [
            {
                "file": f"uniform/{f.name}",
                "t": tail_t if f.name == "uniform_end.png" else i * uniform_every,
            }
            for i, f in enumerate(uni_files)
        ],
        "uniform_interval_seconds": uniform_every,
        "scene_gaps": find_gaps(times, total, min_gap),
    }
    (out_dir / "frames.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    return index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--out", default="frames", help="output directory")
    ap.add_argument("--target", type=int, default=35, help="rough number of scene frames to aim for")
    ap.add_argument("--uniform-every", type=int, default=15, help="seconds between uniform samples")
    ap.add_argument(
        "--width",
        type=int,
        default=0,
        help="output frame width in px; 0 (default) keeps native resolution. "
        "Downscaling a UI capture loses exactly the small text you need — only set this if frames "
        "are genuinely too large to handle.",
    )
    ap.add_argument(
        "--gap-seconds",
        type=float,
        default=30.0,
        help="report stretches longer than this with no scene change (default: 30)",
    )
    args = ap.parse_args()

    idx = extract(
        Path(args.video), Path(args.out), args.target,
        args.uniform_every, args.width, args.gap_seconds,
    )

    print(f"\nduration       {idx.get('duration_seconds')}s")
    print(f"scene frames   {len(idx['scene_frames'])}  (threshold {idx['scene_threshold']})")
    print(f"uniform frames {len(idx['uniform_frames'])}  (every {args.uniform_every}s)")
    print(f"index          {Path(args.out) / 'frames.json'}")
    if idx.get("scene_gaps"):
        print("\nStretches with no detected scene change — often a wait, a long-running job, or typing.")
        print("SAMPLE THESE FINELY with grab_frame.py before drawing any conclusion. A gap means")
        print("scene detection found nothing, NOT that nothing happened: a job completing may change")
        print("only a status chip and two columns, which is well below the threshold. Concluding from")
        print("a trailing gap that 'the flow never finished' has already been wrong once — the outcome")
        print("landed in the final 12 seconds and coarse sampling missed it.")
        for g in idx["scene_gaps"]:
            tail = "  <-- reaches end of video; check the last seconds at 1fps" if (
                idx.get("duration_seconds") and abs(g["to"] - idx["duration_seconds"]) < 2
            ) else ""
            print(f"  {g['from']}s -> {g['to']}s  ({g['seconds']}s){tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

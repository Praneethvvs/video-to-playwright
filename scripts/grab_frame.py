"""Grab specific frames at native resolution, optionally cropped and magnified.

Bulk extraction is for mapping a recording; this is for *reading* it. You need this whenever a value
matters — a typed field, a grid cell, a status chip, a toast — because those are exactly the details
that coarse sampling and any downscaling destroy.

Two real failures this exists to prevent:

* A downscaled sample showed a Description field as empty while a later page showed it populated,
  which looked like an application bug. It was being typed between samples.
* A trailing "no scene change" gap was read as "the flow never finished". The job actually completed
  in the final 12 seconds, changing only a status chip and two grid columns — far too small a delta
  for scene detection, and missed entirely by 10-second sampling.

Usage:
    # single moment
    python grab_frame.py video.mp4 --at 44

    # a range at 1fps — the fix for "did anything happen at the end?"
    python grab_frame.py video.mp4 --from 200 --to 213

    # zoom a region (x,y,w,h in source pixels), scaled 2x for legibility
    python grab_frame.py video.mp4 --at 15 --crop 60,430,900,120 --zoom 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _ffmpeg import run


def build_filter(crop: str | None, zoom: float) -> str:
    parts = []
    if crop:
        try:
            x, y, w, h = (int(v) for v in crop.split(","))
        except ValueError:
            sys.exit("--crop expects four integers: x,y,w,h")
        parts.append(f"crop={w}:{h}:{x}:{y}")
    if zoom and zoom != 1:
        # Nearest-neighbour keeps text edges crisp; smooth scaling makes small glyphs mushy.
        parts.append(f"scale=iw*{zoom}:ih*{zoom}:flags=neighbor")
    return ",".join(parts)


def grab_one(video: Path, at: float, out: Path, vf: str) -> Path:
    args = ["-ss", str(at), "-i", str(video)]
    if vf:
        args += ["-vf", vf]
    args += ["-frames:v", "1", "-y", str(out)]
    run(args)
    return out


def grab_range(video: Path, start: float, end: float, fps: float, out_dir: Path, vf: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    chain = f"fps={fps}" + (f",{vf}" if vf else "")
    args = [
        "-ss", str(start), "-to", str(end), "-i", str(video),
        "-vf", chain, "-y", str(out_dir / "at_%03d.png"),
    ]
    run(args)
    files = sorted(out_dir.glob("at_*.png"))
    # Restate the mapping explicitly — off-by-one on frame timing has caused wrong conclusions before.
    for i, f in enumerate(files):
        print(f"  {f.name}  ~= {start + i / fps:.1f}s")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--at", type=float, help="single timestamp in seconds")
    ap.add_argument("--from", dest="start", type=float, help="range start in seconds")
    ap.add_argument("--to", dest="end", type=float, help="range end in seconds")
    ap.add_argument("--fps", type=float, default=1.0, help="frames per second within a range")
    ap.add_argument("--crop", help="x,y,w,h in source pixels")
    ap.add_argument("--zoom", type=float, default=1.0, help="magnification (2 = double size)")
    ap.add_argument("--out", help="output file (single) or directory (range)")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"File not found: {video}")
        return 1

    vf = build_filter(args.crop, args.zoom)

    if args.at is not None:
        out = Path(args.out) if args.out else Path(f"frame_{args.at:g}s.png")
        grab_one(video, args.at, out, vf)
        print(f"wrote {out}")
        return 0

    if args.start is not None and args.end is not None:
        out_dir = Path(args.out) if args.out else Path(f"frames_{args.start:g}-{args.end:g}s")
        files = grab_range(video, args.start, args.end, args.fps, out_dir, vf)
        print(f"wrote {len(files)} frames to {out_dir}")
        return 0

    ap.error("give either --at, or both --from and --to")
    return 2


if __name__ == "__main__":
    sys.exit(main())

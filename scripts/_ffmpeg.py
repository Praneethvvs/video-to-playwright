"""Locate an ffmpeg binary without assuming one is on PATH.

Corporate machines frequently have no ffmpeg and no easy way to install one system-wide. The
`imageio-ffmpeg` wheel bundles a static binary and installs with plain pip into a venv, which is
usually the path of least resistance — and it needs no admin rights.
"""

from __future__ import annotations

import shutil
import sys


def ffmpeg_path() -> str:
    """Return a usable ffmpeg executable path, or exit with actionable instructions."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    sys.exit(
        "ffmpeg not found.\n"
        "Install a user-scope copy (no admin rights, no PATH changes):\n"
        "    pip install imageio-ffmpeg\n"
        "or install ffmpeg system-wide and ensure it is on PATH."
    )


def run(args: list[str]) -> str:
    """Run ffmpeg and return combined output.

    ffmpeg writes its informational output to stderr and exits non-zero for several benign cases
    (such as `-i` with no output file), so output is captured rather than checked.
    """
    import subprocess

    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return (proc.stdout or "") + (proc.stderr or "")

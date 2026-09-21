"""Entry point.

One uvicorn worker, always. With more than one, the "only one run at a time" invariant and the
in-memory log fan-out both break: a browser can land on worker B while the run it wants lives in
worker A, and two workers will happily start two runs against the same shared environment.

Binds to 127.0.0.1 by default. This process can start browsers that mutate a shared dev
environment, so it does not listen beyond the machine until somebody has decided how it is
authenticated.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import ConfigError, load


def _logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        state_dir / "testboard.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    # Rotated for the same reason test artifacts are pruned. Moving the disk-fill bug from the
    # artifacts directory into the application's own log would not be a fix.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


def main() -> int:
    parser = argparse.ArgumentParser(prog="testboard", description=__doc__)
    parser.add_argument("--repo", default=".", help="the test repository to serve (default: .)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--version", action="version", version=f"testboard {__version__}")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    try:
        config = load(repo)
    except ConfigError as exc:
        print(f"testboard: {exc}", file=sys.stderr)
        return 2

    _logging(config.state_dir)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("testboard").warning(
            "listening on %s. This process can start runs that mutate %s — make sure something "
            "in front of it authenticates callers.",
            args.host, config.environment.base_url() or "the target environment",
        )

    print(f"testboard {__version__} — {config.repo_name}")
    print(f"  repo        {config.repo_root}")
    print(f"  interpreter {config.repo_python()}")
    print(f"  serving     http://{args.host}:{args.port}")

    uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

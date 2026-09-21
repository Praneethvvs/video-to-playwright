"""Checks that run before pytest, so an environment problem never has to be inferred afterwards.

The alternative is pattern-matching a traceback for "connection refused", and that approach
eventually mislabels a real defect as an environment problem or the reverse. Neither error is
recoverable by the person reading the result, because both look like a red test.

So: `error_reason` is decided here, by testboard, from a check it performed itself. If the base URL
is unreachable the run is refused and pytest is never started.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("testboard.preflight")


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str | None       # an error_reason value when not ok
    detail: str


OK = Check(True, None, "")


def check_interpreter(config: Config) -> Check:
    python = config.repo_python()
    if not python.exists():
        return Check(False, "venv_broken",
                     f"the repo interpreter {python} does not exist. Check `runner.python` in "
                     f"testboard.yaml and that the test repo's virtualenv has been created.")
    return OK


def check_browsers(config: Config) -> Check:
    """A heuristic, and labelled as one.

    Launching a browser to be certain costs seconds on every boot. The cache directory being
    absent is the case that actually happens — a fresh container where `playwright install` was
    left out of the image — and that is worth catching cheaply.
    """
    import os

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    candidates = [Path(override)] if override else []
    candidates += [
        Path.home() / ".cache" / "ms-playwright",
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
    ]
    for path in candidates:
        try:
            if path.is_dir() and any(path.iterdir()):
                return OK
        except OSError:
            continue

    # A configured browser channel uses a browser already installed on the machine, so an empty
    # Playwright cache is not evidence of anything.
    if "--browser-channel" in config.runner.extra_args:
        return OK
    return Check(False, "browsers_missing",
                 "no Playwright browser cache found. If runs fail to start, the image or machine "
                 "probably needs `playwright install --with-deps chromium`.")


def check_disk(config: Config) -> Check:
    try:
        usage = shutil.disk_usage(config.state_dir if config.state_dir.exists()
                                  else config.repo_root)
    except OSError as exc:
        return Check(True, None, f"could not read disk usage: {exc}")
    free_mb = usage.free // (1024 * 1024)
    if free_mb < config.artifacts.disk_floor_mb:
        return Check(False, "disk_low",
                     f"only {free_mb} MB free, below the {config.artifacts.disk_floor_mb} MB floor. "
                     f"Refusing to start a run rather than write a truncated trace into a full disk.")
    return OK


def _ssl_context():
    """Verify against the operating system's trust store, not certifi's bundle.

    Behind corporate TLS interception the proxy re-signs every response with a root that is
    installed in the OS store and absent from certifi's. Without this the preflight reports
    CERTIFICATE_VERIFY_FAILED and refuses a run against a host that is up and answering — the
    exact false negative that makes a gate untrustworthy.
    """
    import ssl

    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:            # noqa: BLE001 - fall back to the default bundle
        return True


async def check_base_url(config: Config, timeout: float = 3.0) -> Check:
    url = config.environment.base_url()
    if not url:
        return Check(False, "environment_unreachable",
                     f"{config.environment.base_url_env} is not set, so there is no application to "
                     f"test against.")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     verify=_ssl_context()) as client:
            response = await client.get(url)
        if response.status_code >= 500:
            return Check(False, "environment_unreachable",
                         f"{url} answered {response.status_code}. The application is up but not "
                         f"serving, so a red suite would say nothing about the tests.")
        return OK
    except httpx.HTTPError as exc:
        return Check(False, "environment_unreachable",
                     f"could not reach {url}: {type(exc).__name__}: {exc}. "
                     f"Is the VPN connected?")


async def before_run(config: Config) -> Check:
    """Everything that must hold right now. Ordered cheapest first."""
    for check in (check_interpreter(config), check_disk(config)):
        if not check.ok:
            return check
    return await check_base_url(config)


def at_startup(config: Config) -> list[tuple[str, Check]]:
    """Static facts about the deployment, surfaced once rather than on every run."""
    return [
        ("interpreter", check_interpreter(config)),
        ("browsers", check_browsers(config)),
        ("disk", check_disk(config)),
    ]

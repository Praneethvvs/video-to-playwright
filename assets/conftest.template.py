"""Starting point for a pytest-playwright conftest.

Python/pytest shown here; the TypeScript equivalents are direct translations into
`playwright.config.ts` (`use: { reducedMotion, baseURL, viewport }`) and fixtures.

Adapt freely — the parts worth keeping are the comments explaining *why* each setting exists, because
each one is here to prevent a specific class of flake or a specific wrong result.
"""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import Page, expect

# Entity IDs are environment fixtures, not test data. Keeping them here — overridable by env var —
# stops them being scattered through the tests, where changing environments means a find-and-replace.
DEFAULT_BASE_URL = "https://app-dev.example.com"

# Raise the assertion timeout when first paint waits on several API calls. The default 5s produces
# confusing "element not found" failures that are really just slow loads.
EXPECT_TIMEOUT_MS = 20_000


def pytest_configure() -> None:
    expect.set_options(timeout=EXPECT_TIMEOUT_MS)


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("APP_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict, base_url: str) -> dict:
    return {
        **browser_context_args,
        "base_url": base_url,
        # Animations on floating layers (dropdowns, popovers, toasts) are a steady source of flake.
        # Disabling them removes the whole class rather than papering over it with waits.
        "reduced_motion": "reduce",
        "viewport": {"width": 1600, "height": 1000},
        # If the app needs a login, capture it once and reuse:
        # "storage_state": "auth.json",
    }


@pytest.fixture
def fail_on_console_errors(page: Page):
    """Fail a test if the page logged errors.

    Cheap, high-value coverage: a page that renders but throws is a real defect that a purely
    visual assertion sails straight past.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    yield errors

    # Filter known environment noise, and keep the pattern NARROW — match the warning text, never a
    # component's name. Filtering on `ag-?grid` (or any library name) silently hides every real error
    # from the most complex component on the page, which defeats the point of the check. Verify the
    # noise actually exists in your environment before filtering for it at all.
    ignorable = re.compile(r"license key|licence key|favicon", re.IGNORECASE)
    real = [e for e in errors if not ignorable.search(e)]
    assert not real, "Console/page errors during test:\n" + "\n".join(real)

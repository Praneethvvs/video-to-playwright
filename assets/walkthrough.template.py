"""Template: ONE continuous recording of the verified flow, for human sign-off.

This is not a CI test. Keep it outside the configured test paths so a normal run never collects it —
it is slow by design and its only job is to produce a video a reviewer can compare against the
original recording.

Place a sibling `conftest.py` next to it containing only:

    @pytest.fixture(scope="session")
    def browser_context_args(browser_context_args: dict) -> dict:
        # Playwright scales recorded video into an 800x800 box by default, which makes dense UI
        # unreadable. Pin it to the viewport — scoped here so CI failure artifacts stay small.
        return {**browser_context_args, "record_video_size": {"width": 1600, "height": 1000}}

Run:
    pytest <this-dir> --headed --slowmo 1000 --video=on --output=<raw-dir> -s

Then convert for the reviewer:
    ffmpeg -y -i video.webm -c:v libx264 -pix_fmt yuv420p -crf 22 -movflags +faststart walkthrough.mp4
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

# Hold on each narrative beat. Reviewers read the screen, look away, look back — err slow. Expect to
# be told it is still too fast and raise it.
HOLD_MS = 2500


class Narrator:
    """Prints timestamped chapters and holds the frame long enough to read.

    The printed chapters become the chapter list in the handoff doc, with real timings rather than
    guesses — which is what lets a reviewer jump to the moment they care about.
    """

    def __init__(self, page) -> None:
        self.page = page
        self._t0 = time.monotonic()
        self.chapters: list[str] = []

    def beat(self, text: str, hold: int = HOLD_MS) -> None:
        elapsed = time.monotonic() - self._t0
        line = f"[{int(elapsed // 60):02d}:{int(elapsed % 60):02d}] {text}"
        print(line, flush=True)
        self.chapters.append(line)
        self.page.wait_for_timeout(hold)

    def show(self, locator, hold: int = 1200):
        """Scroll something into view before asserting on it.

        A test that passes on an off-screen element is correct but proves nothing to someone
        watching the video, and the video is the entire point here.
        """
        locator.scroll_into_view_if_needed()
        self.page.wait_for_timeout(hold)
        return locator


@pytest.mark.walkthrough
def test_full_walkthrough(page) -> None:
    n = Narrator(page)

    # Follow the ORDER OF THE ORIGINAL RECORDING, not the order of your test files — the reviewer is
    # comparing against their own video and will lose the thread otherwise.
    #
    # Use the SAME page objects and the SAME assertions as the real suite. If the walkthrough can
    # pass while the suite fails, the video is misleading.

    # page.goto("/some/deep/link")
    n.beat("Landing on <the screen the recording opened on>")

    # n.show(some_grid)
    n.beat("<what the narrator pointed out first>")

    # ... action ...
    n.beat("<what changed, quoting the narration where it matches>")

    # Call out assertions the narration never made — these are discoveries from verification, and
    # flagging them in the video is how they get reviewed rather than silently trusted.
    n.beat("<field> auto-filled as <value> — NOT stated in the narration, please confirm")

    # Leave the environment exactly as found, and show that happening.
    n.beat("Cleaned up — <resource> removed, counts back to baseline")

    print("\n=== CHAPTERS ===", flush=True)
    for line in n.chapters:
        print(line, flush=True)

"""Capture a structural map of an application, for drift detection on later runs.

The map answers one question: **what is addressable on each screen, and by what name?** It is built
from the live application, because that is what tests actually run against, and it is committed to the
test repository so the next run can diff against it.

The critical design rule is that the map records **structure, not data**. Roles, accessible names of
controls, tab and column identities — yes. Row values, totals, dates, record names — no. Getting this
wrong makes the map useless: every ordinary data change would look like drift, the report would cry
wolf, and people would stop reading it.

That distinction is not academic. Two real test failures during this workflow's development came from
asserting data as though it were structure: an exact record name in a page heading, and "exactly one
option is selected". Both broke with no defect present. The map deliberately cannot encode either.

What the map is not:

* **Not a source of truth for writing tests.** It records what was true when captured. Locators still
  get verified against the running application before any test ships.
* **Not a substitute for reading the app.** It is a comparison baseline, nothing more.

Usage:
    python build_project_map.py --base-url https://app-dev.example.com \\
        --routes routes.txt --out project-map.json [--channel chrome]

`routes.txt` holds one path per line; blank lines and `#` comments are ignored. Parameterised routes
need concrete values, so substitute real identifiers before listing them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# Roles worth recording. These are the things tests address; everything else is layout noise that
# would generate diff churn without telling anyone anything useful.
INTERESTING_ROLES = (
    "button", "link", "tab", "textbox", "combobox", "checkbox", "radio", "switch",
    "menuitem", "option", "heading", "columnheader", "dialog", "region", "alert",
)

# Strip anything that varies run to run. A name containing a date, a currency amount, a UUID or a
# long digit run is data, and recording it would turn routine activity into false drift.
_VOLATILE = re.compile(
    r"""(
        \b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b      # dates
      | \$\s?[\d,]+(?:\.\d+)?                   # currency
      | \b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b  # uuids
      | \b\d[\d,]{3,}\b                         # long numbers
      | \b\d+(?:\.\d+)?%                        # percentages
    )""",
    re.X | re.I,
)


def is_volatile(name: str) -> bool:
    return bool(_VOLATILE.search(name or ""))


def capture_route(page, base_url: str, route: str) -> dict:
    """Capture one route's addressable structure."""
    entry: dict = {"route": route}
    try:
        page.goto(f"{base_url.rstrip('/')}{route}", wait_until="domcontentloaded", timeout=45_000)
    except Exception as exc:  # noqa: BLE001 - an unreachable route is data, not a crash
        entry["reachable"] = False
        entry["error"] = str(exc)[:200]
        return entry

    # Give client-rendered content a chance to arrive. A screen captured mid-render produces a map
    # that reports missing controls, which reads as drift on the very next run.
    page.wait_for_timeout(4_000)
    entry["reachable"] = True
    entry["title"] = page.title()

    # Controls, by role and accessible name. Volatile names are counted but not recorded, so a screen
    # full of data rows contributes one number rather than hundreds of churning entries.
    controls: dict[str, list[str]] = {}
    volatile_counts: dict[str, int] = {}
    for role in INTERESTING_ROLES:
        names = page.evaluate(
            """(role) => {
                const out = [];
                for (const el of document.querySelectorAll('[role="' + role + '"], ' + (
                    {button:'button', link:'a[href]', textbox:'input,textarea',
                     checkbox:'input[type=checkbox]', radio:'input[type=radio]',
                     heading:'h1,h2,h3,h4,h5,h6'}[role] || ':not(*)'
                ))) {
                    const n = (el.getAttribute('aria-label')
                        || el.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (n) out.push(n.slice(0, 120));
                }
                return [...new Set(out)];
            }""",
            role,
        )
        stable = sorted({n for n in names if not is_volatile(n)})
        vol = len(names) - len(stable)
        if stable:
            controls[role] = stable
        if vol:
            volatile_counts[role] = vol
    entry["controls"] = controls
    entry["volatile_name_counts"] = volatile_counts

    # data-testid attributes: the deliberate test surface, so worth tracking precisely. One
    # disappearing is unambiguously a breaking change.
    entry["test_ids"] = sorted(set(page.evaluate(
        "() => [...document.querySelectorAll('[data-testid]')].map(e => e.getAttribute('data-testid'))"
    )))

    # Grid structure, keyed by a signature derived from the columns rather than by position.
    #
    # Position is unusable as an identity: this application gained a grid between two runs, which
    # under index keying would have reported every column of every grid as both removed and added.
    # Keying on content means an added grid is simply one new entry.
    #
    # Nested containers are also deduplicated — a `.ag-root-wrapper` contains a `[role=grid]`
    # descendant, so a naive selector counts each grid twice and makes the count itself unstable.
    grids = page.evaluate(
        """() => {
            const sel = '.ag-root-wrapper, [role=grid], table';
            const all = [...document.querySelectorAll(sel)];
            const outermost = all.filter(el => !all.some(o => o !== el && o.contains(el)));
            return outermost.map(g => ({
                columns: [...new Set([...g.querySelectorAll('[col-id]')]
                    .map(c => c.getAttribute('col-id')))].sort(),
                headers: [...new Set([...g.querySelectorAll('.ag-header-cell-text, th')]
                    .map(h => (h.textContent || '').trim()).filter(Boolean))].sort(),
            }));
        }"""
    )
    # A grid with no identifiable columns tells us nothing and only adds diff noise.
    entry["grids"] = {
        (",".join(g["columns"][:4]) or "unkeyed"): g for g in grids if g["columns"] or g["headers"]
    }

    frames = page.frames
    if len(frames) > 1:
        entry["iframe_count"] = len(frames) - 1

    return entry


def build(base_url: str, routes: list[str], channel: str | None) -> dict:
    from playwright.sync_api import sync_playwright

    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        for r in routes:
            print(f"  {r}", file=sys.stderr)
            captured.append(capture_route(page, base_url, r))
        browser.close()

    body = {"schema_version": SCHEMA_VERSION, "base_url": base_url, "routes": captured}
    # Fingerprint the comparable content only. The capture timestamp sits outside it, so re-running
    # with no app change produces an identical fingerprint and a clean diff.
    body["fingerprint"] = hashlib.sha256(
        json.dumps(body["routes"], sort_keys=True).encode()
    ).hexdigest()[:16]
    body["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return body


def read_routes(path: Path) -> list[str]:
    # utf-8-sig strips a byte-order mark. Without it, a BOM-prefixed first line fails the
    # startswith("/") test, gets a second slash prepended, and requests "//route" — which some
    # servers tolerate, so the capture appears to succeed while the map records a wrong route.
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().lstrip("﻿")
        if line and not line.startswith("#"):
            out.append(line if line.startswith("/") else "/" + line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--routes", required=True, help="file with one route path per line")
    ap.add_argument("--out", default="project-map.json")
    ap.add_argument("--channel", help="browser channel, e.g. chrome (avoids a bundled download)")
    args = ap.parse_args()

    routes = read_routes(Path(args.routes))
    print(f"Capturing {len(routes)} route(s) from {args.base_url}", file=sys.stderr)
    m = build(args.base_url, routes, args.channel)

    Path(args.out).write_text(json.dumps(m, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    reached = sum(1 for r in m["routes"] if r.get("reachable"))
    print(f"\nfingerprint : {m['fingerprint']}")
    print(f"routes      : {reached}/{len(routes)} reachable")
    print(f"written     : {args.out}")
    if reached < len(routes):
        print("\nUnreachable routes are recorded with their error. Parameterised routes usually need")
        print("real identifiers, and gated screens need a signed-in session — both are worth fixing")
        print("before committing the map, or the first drift report will be mostly noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

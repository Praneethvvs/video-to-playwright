"""Compare the committed project map against the application as it is now.

Run this **before** writing new tests. Applications drift, and drift discovered up front is a fact you
can report; drift discovered halfway through writing a suite looks like your locators are wrong and
sends you hunting for a bug that isn't there.

The output separates two things that matter very differently:

* **Breaking drift** — something disappeared or was renamed, and a committed test or page object
  references it. Those tests are stale: they will fail, or worse, silently assert the wrong thing.
* **Benign drift** — the app gained a tab, a column, a button. Nothing existing references it, so
  nothing is stale. Worth knowing, not worth stopping for.

Most reported drift is benign. Saying so plainly is what keeps the report readable, and a report
nobody reads is the same as no report.

Staleness is determined by searching the test tree for the affected name. That works because the
workflow puts locators in page objects, so a name that matters appears in a small number of files. It
will miss a locator assembled from fragments at runtime — a known limit, not a silent one.

**What this cannot see.** The map records structure, so it detects things appearing and disappearing.
A *modification* — a control that keeps its name and changes what it does — is invisible to it. A rule
changing, a calculation differing, a field meaning something new: all report as "no drift". Only the
tests themselves catch that, which is the whole reason the suite exists alongside this check. Treat a
clean drift report as "no locator has broken", never as "nothing has changed".

Usage, simplest first. With a `drift-config.json` beside the map, no flags are needed:

    python check_drift.py                 # against the deployed environment
    python check_drift.py --local         # against a local build of your branch

    {
      "map": "project-map.json",
      "tests": "e2e",
      "dev_url":   "https://app-dev.example.com",
      "local_url": "http://localhost:4300",
      "channel":   "chrome"
    }

Everything is still overridable:

    python check_drift.py --map project-map.json --tests tests/ --base-url http://localhost:4300
    python check_drift.py --map project-map.json --against fresh-map.json --tests tests/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATUS_SCHEMA = 1


def write_status(path: str | None, payload: dict) -> None:
    """Record the outcome as JSON, on every exit path without exception.

    The printed report and the exit code are for a person and for CI. This file is for anything
    that renders the result later. It is written even when nothing was compared, because an
    unreachable environment and a clean comparison are different facts, and a consumer that cannot
    tell them apart will eventually report "no drift" when the truth is "no idea".
    """
    if not path:
        return
    body = {"schema": STATUS_SCHEMA,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload}
    try:
        Path(path).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # A reporting failure must never change the verdict the gate returns.
        print(f"(could not write status to {path}: {exc})", file=sys.stderr)


def load(path: Path) -> dict:
    # utf-8-sig, because plenty of Windows tooling writes a byte-order mark and a BOM-prefixed map
    # otherwise fails to parse at all — an unhelpful way for a committed baseline to become unusable.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def content_fingerprint(m: dict) -> str:
    """Recompute the fingerprint from the map's own content.

    Never trust the stored `fingerprint` field for the equality check. It is written at capture time,
    so a hand-edited, merge-resolved or partially-written map keeps a hash that no longer describes
    its contents — and the comparison would then report "no drift" for a map that had genuinely
    changed. Recomputing costs nothing and removes the whole failure mode.
    """
    return hashlib.sha256(
        json.dumps(m.get("routes", []), sort_keys=True).encode()
    ).hexdigest()[:16]


def capture_fresh(stored: dict, channel: str | None, script_dir: Path, map_path: Path,
                  base_url_override: str | None = None) -> dict:
    """Re-capture using the stored map's own base_url and route list, so the diff is apples to apples.

    Scratch files are written beside the committed map rather than into the current working directory.
    Using the CWD meant that running this from a project root scattered `.drift-fresh.json` there —
    harmless in itself, but it quietly breaks any "keep everything under this directory" instruction
    and leaves litter a caller did not ask for.
    """
    routes = [r["route"] for r in stored["routes"]]
    work = map_path.parent
    tmp_routes = work / ".drift-routes.txt"
    tmp_routes.write_text("\n".join(routes), encoding="utf-8")
    tmp_map = work / ".drift-fresh.json"
    cmd = [
        sys.executable, str(script_dir / "build_project_map.py"),
        # The gate usually runs against a locally built app, not the environment the baseline came
        # from, so the URL has to be overridable while the route list stays fixed.
        "--base-url", base_url_override or stored["base_url"],
        "--routes", str(tmp_routes),
        "--out", str(tmp_map),
    ]
    if channel:
        cmd += ["--channel", channel]
    subprocess.run(cmd, check=True)
    fresh = load(tmp_map)
    tmp_routes.unlink(missing_ok=True)
    tmp_map.unlink(missing_ok=True)
    return fresh


def find_references(name: str, tests_dir: Path) -> list[str]:
    """Which test files mention this name?

    Substring search on the literal name. Page objects centralise locators, so this is usually exact;
    a name built by concatenation at runtime will be missed.

    Paths come back POSIX-style on every platform. They are compared and displayed downstream, and a
    Windows author reporting `e2e\\pages\\x.py` for what a Linux agent calls `e2e/pages/x.py` turns
    one stale file into two.
    """
    if not name or len(name) < 3 or not tests_dir.exists():
        return []
    hits = []
    needle = name.lower()
    for f in tests_dir.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".ts", ".js", ".tsx"):
            try:
                if needle in f.read_text(encoding="utf-8", errors="replace").lower():
                    hits.append(f.as_posix())
            except OSError:
                continue
    return hits



def diff_route(old: dict, new: dict, tests_dir: Path) -> tuple[list[dict], list[dict]]:
    breaking, benign = [], []
    route = old["route"]

    if old.get("reachable") and not new.get("reachable"):
        breaking.append({
            "route": route, "kind": "route unreachable",
            "detail": new.get("error", "")[:160],
            "affects": [p.as_posix() for p in tests_dir.rglob("*")
                        if p.is_file() and p.suffix in (".py", ".ts", ".js", ".tsx")
                        and route in p.read_text(encoding="utf-8", errors="replace")]
                       if tests_dir.exists() else [],
        })
        return breaking, benign
    if not old.get("reachable"):
        return breaking, benign

    # Controls, per role
    for role in set(old.get("controls", {})) | set(new.get("controls", {})):
        was = set(old.get("controls", {}).get(role, []))
        now = set(new.get("controls", {}).get(role, []))
        for gone in sorted(was - now):
            refs = find_references(gone, tests_dir)
            item = {"route": route, "kind": f"{role} gone", "detail": gone, "affects": refs}
            (breaking if refs else benign).append(item)
        for added in sorted(now - was):
            benign.append({"route": route, "kind": f"{role} added", "detail": added, "affects": []})

    # test-ids: a removed one is a deliberate hook disappearing, so always worth flagging loudly
    for gone in sorted(set(old.get("test_ids", [])) - set(new.get("test_ids", []))):
        refs = find_references(gone, tests_dir)
        (breaking if refs else benign).append(
            {"route": route, "kind": "data-testid gone", "detail": gone, "affects": refs}
        )
    for added in sorted(set(new.get("test_ids", [])) - set(old.get("test_ids", []))):
        benign.append({"route": route, "kind": "data-testid added", "detail": added, "affects": []})

    # Grids are keyed by column signature, not position, so an added grid is one new entry rather
    # than every column of every grid appearing to move.
    old_grids = old.get("grids", {}) or {}
    new_grids = new.get("grids", {}) or {}
    if isinstance(old_grids, list) or isinstance(new_grids, list):
        benign.append({
            "route": route, "kind": "map schema changed",
            "detail": "grids were captured positionally by an older build; regenerate the map",
            "affects": [],
        })
        return breaking, benign

    for key in sorted(set(old_grids) | set(new_grids)):
        og, ng = old_grids.get(key), new_grids.get(key)
        if og and not ng:
            # The whole grid is gone, or changed enough that its first columns no longer match.
            refs = find_references(key.split(",")[0], tests_dir)
            (breaking if refs else benign).append({
                "route": route, "kind": "grid gone or restructured",
                "detail": f"columns {key}", "affects": refs,
            })
            continue
        if ng and not og:
            benign.append({"route": route, "kind": "grid added",
                           "detail": f"columns {key}", "affects": []})
            continue
        for gone in sorted(set(og["columns"]) - set(ng["columns"])):
            refs = find_references(gone, tests_dir)
            (breaking if refs else benign).append(
                {"route": route, "kind": "grid column gone", "detail": f"{gone} (in {key})",
                 "affects": refs}
            )
        for added in sorted(set(ng["columns"]) - set(og["columns"])):
            benign.append({"route": route, "kind": "grid column added",
                           "detail": f"{added} (in {key})", "affects": []})

    return breaking, benign


CONFIG_NAME = "drift-config.json"


def find_config(explicit: str | None) -> tuple[dict, Path | None]:
    """Load `drift-config.json` from an explicit path, the CWD, or a `scripts/` parent.

    The point is that the common cases need no flags at all. A developer checking their branch should
    type one thing, not remember four paths, because a command nobody can recall from memory is a
    command that stops being run.
    """
    candidates = [Path(explicit)] if explicit else [
        Path(CONFIG_NAME),
        Path(__file__).parent.parent / CONFIG_NAME,
    ]
    for c in candidates:
        if c.exists():
            return json.loads(c.read_text(encoding="utf-8-sig")), c
    return {}, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", help="the committed project map (default: from drift-config.json)")
    ap.add_argument("--against", help="a second map file; omit to capture live now")
    ap.add_argument("--tests", help="test tree to search for stale references")
    ap.add_argument("--channel")
    ap.add_argument(
        "--base-url",
        help="check against this URL instead of the map's, e.g. a local build of your branch",
    )
    ap.add_argument(
        "--local",
        action="store_true",
        help="shorthand for the config's local_url — use when checking your own branch",
    )
    ap.add_argument("--config", help=f"path to {CONFIG_NAME} (default: found automatically)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--status-out",
        help="write the outcome as JSON to this path, on every exit path including the ones "
             "where nothing was compared",
    )
    args = ap.parse_args()

    # Config fills in whatever was not passed, so the everyday commands need no flags.
    cfg, cfg_path = find_config(args.config)
    args.map = args.map or cfg.get("map", "project-map.json")
    args.tests = args.tests or cfg.get("tests", "tests")
    args.channel = args.channel or cfg.get("channel")
    if args.local:
        if not cfg.get("local_url"):
            ap.error(f"--local needs a local_url in {CONFIG_NAME}")
        args.base_url = cfg["local_url"]
    elif not args.base_url:
        args.base_url = cfg.get("dev_url")

    if not Path(args.map).exists():
        write_status(args.status_out, {
            "outcome": "no-map", "exit_code": 2, "map_path": args.map,
            "message": f"no project map at {args.map}",
        })
        ap.error(
            f"no project map at {args.map}. Capture one first:\n"
            f"  python scripts/build_project_map.py --base-url <dev-url> "
            f"--routes routes.txt --out {args.map}"
        )

    stored = load(Path(args.map))
    fresh = load(Path(args.against)) if args.against else capture_fresh(
        stored, args.channel, Path(__file__).parent, Path(args.map),
        base_url_override=args.base_url,
    )

    def base_status() -> dict:
        """The facts that hold regardless of which way the comparison went."""
        routes = fresh.get("routes", [])
        return {
            "map_path": args.map,
            "tests_dir": args.tests,
            "baseline_url": stored.get("base_url"),
            "checked_url": fresh.get("base_url"),
            "map_schema_version": stored.get("schema_version"),
            "map_captured_at": stored.get("captured_at"),
            "checked_captured_at": fresh.get("captured_at"),
            "map_fingerprint": content_fingerprint(stored),
            "checked_fingerprint": content_fingerprint(fresh),
            "routes_total": len(routes),
            "routes_reachable": sum(1 for r in routes if r.get("reachable")),
            "routes_unreachable": [
                {"route": r.get("route"), "failure_class": r.get("failure_class"),
                 "error": str(r.get("error", ""))[:200]}
                for r in routes if not r.get("reachable")
            ],
            "routes_unsettled": [
                r.get("route") for r in routes
                if r.get("reachable") and r.get("settled") is False
            ],
            "breaking": [], "benign": [], "stale_test_files": [],
        }

    # Comparing across URLs is the normal case, not a mistake: the baseline is the accepted state of
    # the UI (usually a shared dev deployment) and the check runs against the proposed state (a local
    # build of the branch). The diff between them is exactly what a pull-request gate wants.
    #
    # What must match is the **backend**, not the URL. Structure can depend on data — a grid renders
    # columns for the data it receives — so a local build pointed at mocks or a different API will
    # differ for reasons that have nothing to do with the branch, and those differences land in the
    # breaking column.
    if stored.get("base_url") and fresh.get("base_url") and stored["base_url"] != fresh["base_url"]:
        print(f"Baseline: {stored['base_url']}")
        print(f"Checking: {fresh['base_url']}")
        print("Expected for a pull-request gate. Confirm the build under test talks to the same")
        print("backend as the baseline did, and has mocks disabled — otherwise data differences")
        print("will read as drift.\n")


    # Refuse to compare a capture that never settled. An unsettled route was photographed mid-render,
    # so its controls are missing for harness reasons, not because anyone changed the application —
    # and comparing it reports every control on the screen as having disappeared. Failing loudly here
    # is the difference between a gate people trust and one they turn off.
    # An environment that isn't up is not drift. Without this, forgetting to start the local dev
    # server reports every route as gone and reads as "the application is broken" — the single most
    # likely way a developer meets this tool, and the worst possible first impression.
    env_down = [
        r for r in fresh.get("routes", [])
        if not r.get("reachable") and r.get("failure_class") in ("environment-unreachable", "harness")
    ]
    if env_down and len(env_down) == len([r for r in fresh.get("routes", []) if not r.get("reachable")]):
        print(f"Could not reach {fresh.get('base_url')} — nothing was compared.\n")
        for r in env_down[:3]:
            print(f"  {r['route']}")
            print(f"      {r.get('failure_class')}: {str(r.get('error', ''))[:110]}")
        print("\nIf you are checking a local build, is the dev server running and on the expected")
        print("port? If you are checking a deployed environment, is the VPN up? This is an")
        print("environment problem, not drift, so no tests are implicated.")
        write_status(args.status_out, {**base_status(), "outcome": "unreachable", "exit_code": 2})
        return 2

    unsettled = [
        r["route"] for r in fresh.get("routes", [])
        if r.get("reachable") and r.get("settled") is False
    ]
    if unsettled:
        print("Capture did not settle, so no comparison was made:\n")
        for r in unsettled:
            print(f"  {r}")
        print("\nThese screens were still rendering when sampled. Their controls are missing for")
        print("harness reasons, not because the application changed, so treating this as drift would")
        print("report every control on the screen as gone. Re-run; if it persists, the screen may")
        print("never reach a stable state and needs a route-specific readiness signal.")
        write_status(args.status_out, {**base_status(), "outcome": "unsettled", "exit_code": 2})
        return 2

    stored_fp, fresh_fp = content_fingerprint(stored), content_fingerprint(fresh)
    if stored_fp == fresh_fp:
        print("No drift. Structure matches the committed map exactly.")
        print("\nNote this only checks structure. A control that kept its name and changed what it")
        print("does reports as no drift — only the tests catch that.")
        write_status(args.status_out, {**base_status(), "outcome": "clean", "exit_code": 0})
        return 0

    tests_dir = Path(args.tests)
    by_route = {r["route"]: r for r in fresh["routes"]}
    breaking, benign = [], []
    for old in stored["routes"]:
        new = by_route.get(old["route"])
        if new:
            b, n = diff_route(old, new, tests_dir)
            breaking += b
            benign += n

    # Emitted before the two report paths diverge, so --status-out behaves identically whether or
    # not --json was asked for.
    write_status(args.status_out, {
        **base_status(), "outcome": "drift", "exit_code": 1 if breaking else 0,
        "breaking": breaking, "benign": benign,
        "stale_test_files": sorted({f for d in breaking for f in d.get("affects", [])}),
    })

    if args.json:
        print(json.dumps({"breaking": breaking, "benign": benign}, indent=2))
        return 1 if breaking else 0

    print(f"Map fingerprint  {stored_fp} -> {fresh_fp}")
    print(f"Captured         {stored.get('captured_at')} -> {fresh.get('captured_at')}\n")

    if breaking:
        print(f"BREAKING — {len(breaking)} change(s) that committed tests reference:\n")
        for d in breaking:
            print(f"  {d['route']}: {d['kind']} — {d['detail']}")
            for f in d["affects"][:6]:
                print(f"      stale: {f}")
        print()
    else:
        print("No breaking drift: nothing that disappeared is referenced by any test.\n")

    if benign:
        print(f"Benign — {len(benign)} change(s) nothing references:\n")
        for d in benign:
            print(f"  {d['route']}: {d['kind']} — {d['detail']}")
        print()


    if breaking:
        print("Each change above needs one of three answers, and the third is easy to forget:\n")
        print("  1. NOT INTENDED — the application has a defect. Leave the test alone; fix the app.")
        print("  2. INTENDED, still tested — the thing moved or was renamed. Update the locator.")
        print("  3. INTENDED, no longer exists — the behaviour is gone for good, so the test is")
        print("     redundant. DELETE it. Do not repair a test for something nobody wants any more,")
        print("     and do not leave it skipped forever; a permanently skipped test is clutter that")
        print("     makes the real skips harder to see.")
        print("\nWhat not to do: quietly relax an assertion so it passes against current behaviour.")
        print("That discards what the recording was evidence of, and can enshrine a defect as the")
        print("expected result.")
        print("\nRegenerate the map only after the change is confirmed and deployed. If a change here")
        print("settles something a skipped test was waiting on, unskip it in the same commit.")
    else:
        print("Remember this only checks structure. A control that kept its name and changed what it")
        print("does reports as no drift — only the tests catch that.")
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())

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

Usage:
    python check_drift.py --map project-map.json --tests tests/ [--channel chrome]
    python check_drift.py --map project-map.json --against fresh-map.json --tests tests/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


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
    """
    if not name or len(name) < 3 or not tests_dir.exists():
        return []
    hits = []
    needle = name.lower()
    for f in tests_dir.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".ts", ".js", ".tsx"):
            try:
                if needle in f.read_text(encoding="utf-8", errors="replace").lower():
                    hits.append(str(f))
            except OSError:
                continue
    return hits


def report_open_questions(path: Path) -> None:
    """Print any parked questions that still have no answer.

    A question is answered when the `Answer:` line has content. Anything still blank is a test that
    cannot be written until someone decides something, and saying so on every run is the whole point
    — an unanswered question that nobody is reminded of stays unanswered forever.
    """
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"^##\s+", text, flags=re.M)[1:]
    unanswered = []
    for b in blocks:
        title = b.splitlines()[0].strip()
        m = re.search(r"^Answer:\s*(.*)$", b, re.M)
        answer = (m.group(1).strip() if m else "")
        if not answer or answer.startswith("<"):
            blocks_line = re.search(r"^Blocks:\s*(.*)$", b, re.M)
            unanswered.append((title, blocks_line.group(1).strip() if blocks_line else ""))
    if unanswered:
        print(f"\nOpen questions — {len(unanswered)} still unanswered ({path.name}):\n")
        for title, blocked in unanswered:
            print(f"  {title}")
            if blocked:
                print(f"      blocks: {blocked}")
        print("\nThese block tests that cannot be written until someone decides. They are not drift,")
        print("and they will be reported every run until answered.")


def diff_route(old: dict, new: dict, tests_dir: Path) -> tuple[list[dict], list[dict]]:
    breaking, benign = [], []
    route = old["route"]

    if old.get("reachable") and not new.get("reachable"):
        breaking.append({
            "route": route, "kind": "route unreachable",
            "detail": new.get("error", "")[:160],
            "affects": [str(p) for p in tests_dir.rglob("*") if p.is_file() and route in
                        p.read_text(encoding="utf-8", errors="replace")] if tests_dir.exists() else [],
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", required=True, help="the committed project map")
    ap.add_argument("--against", help="a second map file; omit to capture live now")
    ap.add_argument("--tests", default="tests", help="test tree to search for stale references")
    ap.add_argument("--channel")
    ap.add_argument(
        "--base-url",
        help="check against this URL instead of the one in the map, e.g. a local build on a pull "
        "request. Keep the environment class the same as the baseline's, or config differences "
        "will read as drift.",
    )
    ap.add_argument(
        "--questions",
        help="path to open-questions.md (default: alongside the test tree's parent)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    stored = load(Path(args.map))
    fresh = load(Path(args.against)) if args.against else capture_fresh(
        stored, args.channel, Path(__file__).parent, Path(args.map),
        base_url_override=args.base_url,
    )

    # Compare like with like. A baseline captured against a shared dev deployment and diffed against a
    # local build will differ for reasons that have nothing to do with anyone's changes: different
    # runtime config, different seed data, feature flags. Those land in the breaking column and make a
    # blocking gate look broken, which is how a blocking gate gets switched off.
    if stored.get("base_url") and fresh.get("base_url") and stored["base_url"] != fresh["base_url"]:
        print(f"WARNING  the baseline was captured against {stored['base_url']}")
        print(f"         and this check ran against   {fresh['base_url']}")
        print("         Differences in runtime config or seed data between the two will appear as")
        print("         drift. Capture the baseline from the same kind of environment you gate on.\n")

    questions_path = (
        Path(args.questions) if args.questions else Path(args.tests).parent / "open-questions.md"
    )

    stored_fp, fresh_fp = content_fingerprint(stored), content_fingerprint(fresh)
    if stored_fp == fresh_fp:
        print("No drift. Structure matches the committed map exactly.")
        # Still report parked questions. A clean drift run is precisely when someone concludes
        # everything is fine and moves on, so staying silent here is how a quarantined test becomes
        # permanent.
        report_open_questions(questions_path)
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

    # Surface questions already parked against tests. A scheduled run that reports drift but stays
    # silent about known-unanswered questions lets the gap quietly persist: nobody is reminded, so
    # nobody answers, so the tests stay quarantined indefinitely.
    report_open_questions(questions_path)

    if breaking:
        print("Before writing new tests: confirm whether each breaking change is intended. Do NOT")
        print("relax the affected assertions to match current behaviour — that discards the thing the")
        print("original recording was evidence of, and can enshrine a regression as expected.")
        print("Regenerate the map only once the changes are confirmed.")
        print("\nIf a change here answers a parked question, update open-questions.md and drop the")
        print("test's quarantine marker in the same commit, so the record stays true.")
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())

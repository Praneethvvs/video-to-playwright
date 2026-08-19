# Locator gotchas in modern web apps

Read this before writing locators. Each entry below silently breaks the obvious approach — the test
either fails with a confusing message or, worse, passes for the wrong reason.

Every item was hit in real work. Where an entry says "measured", the numbers came from executing the
locator against a live app, not from reading source.

## Contents

1. [Accessible names — trust Playwright, not other tools](#1-accessible-names)
2. [Strict mode: duplicates you won't predict](#2-strict-mode)
3. [Data grids (AG Grid and friends)](#3-data-grids)
4. [Headless component libraries (Radix, Base UI, Headless UI, MUI)](#4-headless-component-libraries)
5. [Toasts and notifications](#5-toasts-and-notifications)
6. [Async, polling and readiness](#6-async-polling-and-readiness)
7. [Canvas, charts and maps](#7-canvas-charts-and-maps)
8. [SPA runtime config, auth and module federation](#8-spa-runtime-config-auth-and-module-federation)
9. [Selectors never to use](#9-selectors-never-to-use)

---

## 1. Accessible names

**Playwright's aria snapshot is the authority.** Other tools that expose an accessibility tree —
browser-automation MCPs, devtools panels, extensions — can serialize it differently and omit computed
names. Trusting one of those over Playwright has produced entirely wrong locator tables: cells
reported as unnamed were in fact addressable by `get_by_role("gridcell", name=...)` all along.

The cheapest way to see the real tree: write an assertion, let it fail once, and read the **Aria
snapshot** section Playwright prints in the call log.

**A missing `aria-label` proves nothing.** For most roles the accessible name is computed from text
content. Checking the DOM for `aria-label` and concluding "unnamed" is a category error. Elements that
genuinely have no name are typically icon-only buttons and toggle switches.

**Name matching is substring by default, and that cuts both ways.**

```python
page.get_by_role("button", name="Warehouse - North")   # substring — matches the whole card
page.get_by_role("heading", name="Warehouse - North", exact=True)  # the inner heading only
```

Two failure modes to watch:
- A **whole card wrapped in a `<button>`** gets an accessible name that is *all its inner text*
  concatenated. Substring matching works; `exact=True` never will.
- A **card description can contain the label you want**. `get_by_text("Select Adjustment Rate")`
  matched both a label and the description *"Single-select adjustment rate or blended"*. Use
  `exact=True`.

**Things that look like headings often aren't.** Card titles are frequently styled `div`s or `span`s.
Check the snapshot before reaching for `get_by_role("heading")`; `get_by_text` is the fallback.

**`<label for="...">` is often broken.** Dangling `for` attributes pointing at non-existent ids are
common in component libraries that generate ids. When `get_by_label` returns nothing, check whether
the `for` actually resolves — and file it, because it also means screen readers can't label that
field.

---

## 2. Strict mode

Playwright throws when a locator matches more than one element. That's a feature, but the duplicates
are rarely where you expect. In one page, measured:

| Locator | Matches |
|---|---|
| `button "Add Adjustment"` | 2 (one per card) |
| `button "Export CSV"` | 2 |
| `button "Manage"` | 6 (3 rows × 2 grids) |
| `button "Save"` | 2 (two panels) |
| `.ag-root-wrapper` | 2 (two grids) |

**Scope before you address.** Anchor on something that distinguishes the container, ideally something
semantic rather than positional:

```python
# Distinguish two grids by a column only one of them has — not by index
economic  = page.locator(".ag-root-wrapper").filter(has=page.locator('[col-id="vendor"]'))
functional = page.locator(".ag-root-wrapper").filter(has=page.locator('[col-id="apply_to_total"]'))
```

Beware `page.locator("div").filter(has=heading)` — it matches *every ancestor div*, so a subsequent
`.locator(...)` finds the same descendant through multiple paths and trips strict mode anyway. An
XPath to the nearest meaningful ancestor is more reliable:

```python
card = page.get_by_role("heading", name="Inventory Adjustments").locator(
    "xpath=ancestor::*[.//button[normalize-space()='Add Adjustment']][1]"
)
```

`ancestor::` is a reverse axis, so `[1]` is the *nearest* match, not the outermost.

---

## 3. Data grids

Virtualized grids (AG Grid, TanStack Virtual, react-window) break several assumptions at once.

**Row virtualization.** Only the visible window is in the DOM. Of 10,000 rows, perhaps 40 exist.
`get_by_role("row")` will never find row 5,000. Either filter/sort to bring the target into view, or
go through the grid's own API via `page.evaluate`.

**Off-screen columns are usually still in the DOM.** Measured on a grid whose horizontal viewport
showed 787px of 1740px: the `include_in_report_flag` cells were present and addressable by `col-id` while
sitting entirely outside the viewport. Assert via the column attribute rather than scrolling.

**But learn to tell "off-screen" from "genuinely absent", because confusing the two wastes hours.**
When a recording references a column you can't find, the question is whether it's scrolled away or not
there at all. The header row enumerates the *full* column set, so it settles it:

```python
cols = grid.evaluate("""g => ({
    headers: [...new Set([...g.querySelectorAll('.ag-header-cell[col-id]')]
        .map(c => c.getAttribute('col-id')))],
    cells: [...new Set([...g.querySelectorAll('[role=gridcell][col-id]')]
        .map(c => c.getAttribute('col-id')))],
})""")
```

If the column appears in `headers`, it exists — address it by `col-id` and stop worrying about
visibility. If it's absent from `headers`, it is genuinely not on that grid, and no amount of scrolling
will produce it. That's a real finding about the app, not a locator problem: report it as drift and ask,
rather than assuming you must be addressing it wrongly.

Note that virtualization behaviour varies with grid configuration and how far off-screen a column is —
so *measure* on the grid in front of you instead of trusting either generalisation.

**Pinned columns split a row across containers.** In AG Grid a single row renders as up to three
elements (left-pinned / centre / right-pinned), each carrying the same `row-id`. Consequences:

- `[row-id="X"]` matches **3** elements → strict mode violation if you assert on it directly
- `[row-id="X"] [col-id="Y"]` matches exactly **1** → always descend to a cell
- The accessibility tree renders the pinned parts as *separate rows*, which makes it look like the
  action buttons aren't in the row. They are; scope by `row-id`, not by the a11y row.

**`row-id` is usually the entity's real id.** That makes it the most stable handle available, and it
pairs perfectly with API-seeded tests that already know the id they created.

**DOM order is not display order.** Immediately after inserting a row, the new row displayed first but
sat last in the DOM. Never address rows by index.

**The paging footer has transient states.** AG Grid renders `1 to ? of more` while loading, then
briefly `of 0` before data arrives. Both parse as "valid" if you're careless:

```python
# Wrong: accepts the transient zero, so your baseline silently becomes 0
expect(footer).to_have_text(re.compile(r"of\s+[\d,]+"))

# Right: wait for a non-zero total
expect(footer).to_have_text(re.compile(r"of\s+[1-9][\d,]*"))
```

More generally: **"the grid element is visible" is not readiness.** Grids mount before their data
arrives. Wait for rows.

**Inline cell editing** usually needs `press_sequentially`, not `fill` — grids listen for keystrokes.

---

## 4. Headless component libraries

Radix, Base UI, Headless UI, MUI Base and similar all share these traits.

**Generated ids regenerate on every render.** You'll see things like `base-ui-«r36»`, `radix-:r1:`,
`mui-42`. They are not stable across renders, let alone across runs. Never use them as locators, and
be suspicious of any id containing punctuation you wouldn't type.

**Popovers, dropdowns, menus and select options are portalled to `<body>`.** They are *not* inside the
dialog or panel that opened them:

```python
dialog.get_by_role("option", name="…")   # 0 matches — it's portalled out
page.get_by_role("option", name="…")     # correct
```

Measured: 13 of 13 autocomplete options rendered in a body-level container while the dialog was open.

**Dialogs persist in the DOM after closing.** `[role=dialog]` still matches after the dialog is
dismissed. Assert `to_be_hidden()`, never `to_have_count(0)`, and identify a dialog by its content
rather than assuming there's only one:

```python
confirm = page.get_by_role("dialog").filter(has_text="cannot be undone")
```

**Disclosure state is exposed — use it.** `aria-expanded` on menu triggers and accordion headers gives
a clean assertion and a reliable wait, far better than sleeping after a click.

**Menus may resist synthetic pointer events.** Some libraries listen for pointer sequences that
non-trusted clicks don't reproduce. Playwright dispatches *trusted* events so `.click()` normally
works — but if you're driving the browser through some other automation tool during exploration and a
menu won't open, that's likely why, not a broken locator. Focus + `Enter` is the diagnostic.

If you ever add a keyboard fallback for this, **prove the click path is genuinely broken first**, and
delete the fallback once it isn't. A fallback that quietly compensates will also quietly hide a real
regression.

**Grids swallow arrow keys.** Inside a data grid, `ArrowDown` moves the grid's cell focus, not the
open menu's highlight. Focus the target menu item directly.

---

## 5. Toasts and notifications

**The container may be mounted on demand — check, don't assume either way.** In one app Sonner's
`[data-sonner-toaster]` did not exist while no toast was showing; in another, on a later build of the
*same* app, the toast region was always present. So verify with `aria_snapshot()` or a count before
relying on it. The failure mode only bites negative assertions, and it is confusing when it does:

```python
# Fails "element(s) not found" rather than passing
expect(page.locator("[data-sonner-toaster]")).not_to_contain_text("saved")

# Assert absence of the text instead
expect(page.get_by_text("saved")).to_have_count(0)
```

**Toasts auto-dismiss**, so assert immediately after the triggering action. If you must capture one
that may vanish before you can read it, install a `MutationObserver` *before* the action and read the
log afterwards.

**Prefer the labelled region.** Toast libraries usually wrap the list in a named region:
`get_by_role("region", name=re.compile("notification", re.I))`.

**Not every mutation produces a toast.** Verify rather than assume symmetry — in one app, delete
produced a toast and create did not.

---

## 6. Async, polling and readiness

**`expect.toPass()` and `expect.poll()` are JavaScript-only.** Python has `expect.set_options(timeout=)`
and per-assertion `timeout=`, but no generic retrying block. For custom conditions in Python use
`page.wait_for_function`, a small `wait_until(fn, timeout, interval)` helper, or poll the API directly
with `APIRequestContext`.

**Long-running jobs: poll the API, not the spinner.** For jobs that run for minutes, watching the UI
means a long, fragile wait. Poll the status endpoint until it flips, then deep-link or reload. Faster
and far more diagnosable when it fails.

**A network response arriving is not the UI updating.** With React Query and similar, the data lands
before the re-render. Use the response as a gate, then assert on rendered output.

**Disable animations globally** rather than sleeping around them: `reduced_motion: "reduce"` in the
context args kills a whole class of flake from floating layers and transitions.

---

## 7. Canvas, charts and maps

Canvas-rendered content has no queryable DOM. Don't assert on pixels. Assert the underlying data —
through the charting library's API or the API response that fed it. If visual coverage is genuinely
needed, use a masked screenshot snapshot and treat it as a change-detector, not a correctness check.

---

## 8. SPA runtime config, auth and module federation

**Runtime config fetches are a useful seam.** Containerized SPAs often fetch `/config/env.json` at
boot. That file is frequently gitignored and developer-created, which means a fresh checkout renders
an error page rather than the app. It is also an excellent interception point for pointing tests at
mocks or a specific backend.

**Check whether auth is actually exercised.** An app that renders standalone without a token is
convenient for testing and misleading about coverage: if production runs the same UI federated behind
a real login with role-based permissions, a suite against the standalone build proves nothing about
auth, roles, or token expiry. Say so explicitly rather than letting "no login needed" read as a win.

**Module federation fails at runtime, not build time.** Host/remote version skew only shows up in a
browser. If the app is a federated remote, cover both the standalone and host-loaded paths, and watch
for failed `remoteEntry.js` requests.

**Enterprise-licensed components** (grids, charts) log licence errors without a key. That's
environment noise — filter it out of console-error assertions rather than chasing it.

---

## 9. Selectors never to use

| Pattern | Why |
|---|---|
| `nth(2)`, `.first`, `.last` on data rows | DOM order isn't display order, and any insert shifts it |
| Hashed CSS-module classes (`_categoryName_uws0o_60`) | Regenerated on every build |
| Generated ids (`base-ui-«r36»`, `radix-:r1:`) | Regenerated on every render |
| Deep CSS chains (`div > div:nth-child(3) > span`) | Break on any layout change |
| XPath by absolute position | Same, worse |

When none of role / label / placeholder / text / test-id works, the element needs instrumenting.
Record it as a backlog item — `aria-label` on an icon button or toggle is usually a one-line fix that
also resolves a genuine accessibility defect. That backlog is a deliverable, not a workaround.

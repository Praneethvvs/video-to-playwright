# The verification loop

The first draft is always partly wrong. That isn't a failure of prompting — locators derived from
frames or source are *hypotheses*, and the only way to test a hypothesis about a running application
is to run it. This loop is where the suite actually gets made.

In the reference implementation it took six rounds to reach green. Three exposed wrong assumptions
rather than typos, and one bug only surfaced when the run was deliberately slowed down.

## The loop

1. Run the tests.
2. Read the failure properly — including the aria snapshot.
3. Decide which category it is (below).
4. Fix one cause, not one symptom.
5. Repeat until green, then run once more to confirm it wasn't order-dependent.

## Read the aria snapshot — it's free ground truth

Every failing `expect` prints the accessibility tree Playwright itself computed. This is the most
reliable view of the page you can get, and it costs nothing:

```
E   Aria snapshot:
E     - tablist:
E       - tab "Indicators Dashboard" [selected]
E     - heading "Inventory Adjustments" [level=2]
E     - gridcell "Seasonal Markdown - 1042"
E     - switch [checked] [disabled]
```

Read it for more than the immediate failure. That snippet answers four separate questions at once:
which headings are real headings, whether grid cells are named, that tab state is exposed via
`[selected]`, and that two switches are disabled — a fact that turned out to be the best lead on an
unrelated blocked test.

If you need the tree and nothing is failing, write a deliberately wrong assertion and let it fail
once. Faster than any other method.

## Failure categories

**Not found (0 matches).** Usually one of: the element is portalled elsewhere, the name is computed
differently than you assumed, the thing you called a heading is plain text, or the page genuinely
hasn't finished rendering. Check the snapshot before changing the locator — the answer is usually
already printed.

**Strict mode violation (2+ matches).** Playwright lists every match with its `outerHTML`. Read them:
often one is the label and one the description, or one is a wrapper and one the inner element. Fix by
scoping to a container or adding `exact=True` — not by adding `.first`.

**Timeout on a state assertion.** The element exists but never reaches the expected state. Distinguish
"the app is slow" (raise the timeout) from "the app never does this" (your assertion is wrong) by
watching it once with `--headed --slowmo`.

**Passes alone, fails in the suite.** Order dependence or shared state. Almost always a test that
mutates the environment without cleaning up, or two tests competing for the same record.

**A timeout that looks like a broken locator but is a fixture-ordering problem.** If a fixture navigates
during setup, and another fixture (or the test) navigates again, the first one's assertions run against
the wrong page and time out — reading exactly like a bad selector. Before re-deriving a locator that you
already verified by hand, check *when* navigation happens relative to fixture setup. Keep navigation in
the test or in exactly one fixture, never both.

**Restore state in a fixture teardown, not a `try/finally` inside the test.** `finally` runs your cleanup
while the original exception is propagating, so a cleanup that also fails replaces the real failure with a
confusing one and you lose the diagnosis. A teardown fixture keeps the two separate: the test reports why
it failed, and cleanup problems surface as their own message.

**Passes fast, fails slow.** The most valuable category, and the reason to run headed at least once.
A slower run exposes races that a fast one hides — a container that mounts before its data arrives,
a transient loading state that your wait accepted as real. These are the flakes that would otherwise
appear once a fortnight in CI and get dismissed as "just flaky".

## Fix causes, not symptoms

When a wait fails intermittently, the tempting fix is a longer timeout or a sleep. Ask instead what
the *readiness signal* actually is. "The grid element is visible" and "the grid has data" are
different events, and asserting the first while meaning the second is a race that will find you later.

The same discipline applies to fallbacks. If you add an alternative path because the obvious one
failed, either demonstrate the obvious path is genuinely broken, or remove the fallback once it works.
A silent fallback will also silently absorb a real regression.

## Cleanup is part of correctness

Tests that mutate data must restore the environment even when they fail — a teardown fixture, not a
final step in the test body. Then verify it: re-read the target state after the run and confirm it
matches the baseline you recorded at the start. Leftover records from a half-failed run are how a
shared environment becomes untestable, one orphan at a time.

## Knowing when to stop

Green is necessary but not sufficient. Before handing off, confirm:

- Every test was seen to pass, not assumed to
- The suite passes twice in a row
- Deliberately breaking something makes the right test fail with a legible message
- The environment matches its pre-run baseline
- Nothing was quietly weakened to make it pass

That last one deserves a real check. Scan the diff for assertions relaxed, waits lengthened, and
fallbacks added during the loop. Any of those may be correct — but each should be a decision you can
justify, not residue from getting to green.

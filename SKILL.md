---
name: video-to-playwright
description: Turn a screen recording (plus transcript if there is one) of someone manually testing a web app into a verified, passing Playwright suite, and produce a single walkthrough video so a human can confirm the tests match what was demonstrated. Use this whenever someone points at a recording, a Teams/Zoom/Loom capture, a `.mp4`/`.webm`/`.mov`, or a manual QA walkthrough and wants it turned into automated tests, regression coverage, or a PR gate — including phrasings like "automate this flow", "here's a video of our release checks", "can you write Playwright for this", "turn our manual testing into tests", or when they supply a video path and a `.docx`/`.vtt` transcript together. Also use it when converting a narrated demo into end-to-end tests, or when asked to verify that generated tests actually match a recording.
---

# Video → verified Playwright suite

A recording tells you **what a human did and why**. It cannot tell you **how to address anything in
code**. Those come from two different places, and keeping them separate is the whole method:

| Input | Supplies | Cannot supply |
|---|---|---|
| Video frames | the sequence, the UI state at each step | durable locators — pixels carry no roles or test-ids |
| Narration / transcript | intent, expected outcomes, business rules, prerequisites | anything the speaker didn't say aloud |
| App source + running app | how to address every element | which flows anyone cares about |

The suite is the **join** of those three. Everything below exists to make that join reliable.

## The one rule

**Never ship a *test* containing a locator or assertion you have not confirmed against the running
application.** A plausible-looking test that was never executed is worse than no test: it reads as
coverage, passes review, and fails silently later.

This governs test code, not the spec. The spec (step 5) is *supposed* to contain unverified candidate
assertions — that's its job, and marking them as candidates is how they reach a human for
confirmation. The rule bites at the point code gets committed.

In practice this means the first draft is always partly wrong, and that's expected. The value of this
skill is the verify-and-fix loop, not the initial generation. Budget for it.

## Workflow

### 1. Gather inputs and clarify what's missing

Read `references/clarification-loop.md` — it lists what you need, when to ask, and how to keep from
drip-feeding questions.

**Only two things genuinely block starting:** the video path, and a narrated transcript. Ask for the
`.vtt` specifically if only a `.docx` was given — Teams `.docx` exports usually collapse to a single
timestamp, while `.vtt` has per-cue times that let you align narration to frames.

Everything else is better asked *after* watching. In particular, don't open with "what's the app
URL?" — the address bar is usually visible in the footage, so read it from a frame and confirm your
reading later. Recovering it yourself is faster than a round trip, and it demonstrates you watched.
Same for the language and runner: read the target repo rather than asking.

Ask at the checkpoints marked below rather than all at once. Questions asked after you've seen the
footage are much better than questions asked before.

### 1b. Get the two repositories

This workflow normally spans **two** repositories, and they have different rules:

| Repository | Role | You may |
|---|---|---|
| The application repo | where the UI source lives | **read only** — never modify it |
| The test repo | where tests, the project map and fixtures live | read and write; changes land by pull request |

Clone both. You need the app source for the locator vocabulary (step 6), and you need the test repo
before you can check for a project map — the map is test infrastructure and lives there, version
controlled beside the tests it describes.

Ask for both URLs if you were given only one. Along with the dev URL, that is the input set:

```
app repo   + test repo   + dev URL   + recording   + transcript
```

**Match the test repo's existing conventions.** Read it before writing anything: its language and
runner, its directory layout, how it names tests, whether page objects already exist. The templates in
`assets/` are a starting point for an empty repo, not a house style to impose on one that already has
opinions. Whoever maintains that repo will maintain these tests, and a suite that looks foreign gets
rewritten or abandoned.

### 1c. Check the project map

**Always do this before writing or changing a test.** An application drifts, and drift found up front
is a fact you can report; the same drift found halfway through writing a suite looks like your
locators are wrong and sends you hunting a bug that doesn't exist.

Look for `project-map.json` in the test repo you just cloned.

**No map yet — build one and commit it.** It becomes the baseline every later run compares against:

```bash
python scripts/build_project_map.py --base-url <dev-url> --routes routes.txt \
  --out project-map.json [--channel chrome]
```

`routes.txt` is one path per line. Parameterised routes need real identifiers substituted in, and
gated screens need a signed-in session — an unreachable route is recorded with its error, so fix
those before committing or the first drift report is mostly noise.

**Map exists — diff it:**

```bash
python scripts/check_drift.py --map project-map.json --tests <test-dir> [--channel chrome]
```

The report splits **breaking** drift (something disappeared that a committed test references, so
those tests are stale) from **benign** drift (the app gained something; nothing references it).
Exit code is non-zero only for breaking drift, which makes it usable as a scheduled job.

#### What "update the tests" may and may not mean

This is where care is needed, because two different situations look identical in a diff.

**Mechanical drift — fix it.** The thing still exists and means the same, but is addressed
differently: a grid was added so an index moved, a label was reworded without changing intent, a
`data-testid` was renamed. Update the locator, note it in the commit, move on.

**Semantic drift — do not fix it.** Something disappeared, a rule changed, an expected value is
different. Rewriting the assertion to match current behaviour silently discards the thing the
recording was evidence of, and can enshrine a regression as expected. Mark the test stale
(`@pytest.mark.quarantine` or equivalent), keep it out of the gate, and raise it.

When you cannot tell which it is, treat it as semantic. The cost of asking is a question; the cost of
guessing wrong is a test that passes over a real defect.

**Rule out a stale client cache before calling anything drift.** Measured case: after creating a
record, switching to another tab in the same single-page app did not show it — the tab change reused a
cached list — while a full route reload did. Read as drift, that is "the app no longer lists new
records", which is wrong and alarming. Reload the route and re-check before classifying a missing
record as semantic.

**A map captures one state, and structure can depend on state.** Also measured: creating a record
caused columns and controls to appear that were absent from the captured baseline. So a clean drift
report does not guarantee every locator a test needs is present — the map describes the app at rest,
and your test may be looking at it mid-flow. Verify locators in the state the test actually runs in,
which is what step 7 is for.

**Running unattended** — as a scheduled job, with nobody to ask — the same split applies, and it is
what makes automation safe here: fix the mechanical, quarantine the semantic, report both, and never
weaken an assertion to get to green. Regenerate and commit the map only once a human has confirmed
the semantic changes were intended.

#### As a blocking gate on the application's pull requests

The strongest place for this check is the app repo's own pull-request build, against the **locally
built UI from that branch** rather than a shared deployment:

```bash
python scripts/check_drift.py --map project-map.json --tests tests/ \
  --base-url http://localhost:4300
```

Breaking drift fails the build. The reasoning is simply that whoever changed the UI is the person who
knows what the change was for, and they are the cheapest person to update the affected tests — far
cheaper than someone discovering it during a release. The report names the stale files, so the fix is
a known edit rather than an investigation.

Two things make this workable rather than resented:

**Compare like with like.** Capture the baseline from the same kind of environment you gate on. A map
taken from a shared deployment and diffed against a local build differs for reasons that have nothing
to do with anyone's changes — runtime config, seed data, feature flags — and those land in the breaking
column. The script warns when the two URLs differ, but the real fix is capturing the baseline locally
if the gate runs locally.

**Benign drift must not fail.** Adding a tab or a column is normal work and blocks nothing. If the gate
fires on ordinary additions it will be switched off within a fortnight, and a disabled gate is worse
than none. That is why the exit code is non-zero only for the breaking class.

One consequence to accept deliberately: a genuine, unresolved app-versus-recording disagreement will
block every PR touching that area until somebody decides. That is the gate working, but it does mean
the parked questions in step 11b need answering rather than accumulating.

The map records **structure, not data** — roles, names, tab and column identities, never row values
or record names. That is deliberate: both test failures in this workflow's own history came from
asserting data as though it were structure, and a map that encoded data would report drift on every
ordinary day's activity until nobody read it.

### 2. Probe the media before trusting it

The scripts need an ffmpeg binary. If one isn't on PATH, install the bundled wheel — no admin rights,
no PATH changes, and it works inside a project venv, which is where you'll be running from by step 10:

```bash
pip install imageio-ffmpeg
python scripts/probe_media.py <video>
```

Reports duration, resolution, bitrate, and whether the audio track carries any activity. Note the
limit: it measures loudness, not speech, so a `NARRATED` verdict means "worth listening to" rather
than "someone definitely spoke". `SILENT` is the reliable direction.

**Treat a silent recording as a broken input, not a variant to plan around.** A narrated recording is
the standard this workflow is built on, because narration is the only source of expected results.
Going ahead without it means recovering every assertion through conversation, which roughly doubles
the effort and produces a weaker spec.

So if the probe says silent, report it immediately and ask for a re-record with narration. That is
usually minutes of someone's time and saves hours of yours.

Do run the probe even when narration is promised — a flat battery on a headset looks identical to a
successful recording until you measure it. One real case: a 192 kb/s audio track that looked properly
narrated turned out to hold a single half-second chime.

If a re-record genuinely isn't available, the fallback is in
`references/silent-recordings.md`. Read it only in that case; it is a degraded path, not the
main one.

### 3. Extract frames

```bash
python scripts/extract_frames.py <video> --out <dir>
```

Scene-change detection plus a uniform time-based sample. Both are needed: scene detection alone
misses long stretches where the only change is typing or a spinner, and uniform sampling alone misses
fast transitions. Frames are named with their timestamp so you can cite them later.

A long recording yields 60–100 frames, so triage rather than reading them all in order:

1. **Sweep the URL bar first.** Crop just that strip from every frame and read them as one batch — it's
   cheap, and it gives you the route structure and the real entity IDs, which is the single highest-value
   data in any recording. `grab_frame.py --crop` does this.
2. **Read one frame per distinct screen** to build the map of where the flow goes.
3. **Then go deep only where a value matters** — a field being filled, a count changing, a status chip.

That order stops you spending most of your attention on frames that turn out to be the same screen.

**Then read the detail with `scripts/grab_frame.py`.** Bulk extraction maps a recording; it does not
let you read it. Any moment where a *value* matters — a field being typed, a grid cell, a status chip,
a toast — needs a precise grab, at native resolution, cropped and magnified if small:

```bash
python scripts/grab_frame.py <video> --at 16 --crop 60,430,900,140 --zoom 2
python scripts/grab_frame.py <video> --from 200 --to 213      # 1fps over a suspicious stretch
```

Do this for every gap the extractor reports, and always for the final seconds. **A reported gap means
scene detection found nothing, not that nothing happened.** A job completing may change only a status
chip and two columns — well under any threshold. Concluding from a trailing gap that "the flow never
finished" has already been wrong in practice: the outcome landed in the last 12 seconds and coarse
sampling missed it entirely.

### 4. Read the transcript — or handle its absence

```bash
python scripts/read_transcript.py <transcript> --classify
```

Handles `.vtt`, `.srt`, `.docx` and `.txt`, returning ordered utterances with timestamps where
available, tagged as likely preconditions / expectations / rules.

Mine it for four things, and keep them separate:
- **Prerequisites** — state that existed before recording started ("you have a client created…")
- **Actions** — what they did
- **Expectations** — "you should see…", "you should not be able to…" — these become assertions
- **Business rules** — the valuable tests, usually phrased as constraints

Transcription errors are common in domain vocabulary. Flag suspicious terms for confirmation rather
than encoding them.

### 5. Draft the spec, with holes left as holes

Write a markdown spec: preconditions, numbered steps, and assertions quoted from the narration.

Where the recording doesn't say what "correct" means, write an explicit open question. **Do not
invent an assertion to fill the gap.** A generated `to_be_visible()` that nobody asked for is the
main way this pipeline produces fake coverage.

**Checkpoint — ask now:** put the open questions to the requester in one batch. This is the highest-value
question round; you've seen the footage and know exactly what's ambiguous.

**If nobody is there to answer** — a batch job, a queued task, an absent requester — don't stall and
don't guess. Build everything that doesn't depend on the answers, leave the dependent tests unwritten,
and put the questions in the handoff. A suite of four solid tests plus five clearly-stated open
questions is a good outcome. Five solid tests plus four invented assertions is not, and the difference
is invisible until something breaks.

### 6. Build the locator vocabulary from source

If you have the app's source, extract the addressable surface: routes, components, their rendered
ARIA roles and accessible names, and any test-ids. This is what turns "they clicked the thing at
640,320" into `get_by_role('option', name='OPT')`.

If you don't have source access, skip to step 7 — the running app alone is enough, just slower.

### 7. Verify every locator against the running app

Non-negotiable, and where most of the real work is. Confirm each locator resolves to **exactly one**
element in the real application.

**Ask Playwright for the accessibility tree directly.** `locator.aria_snapshot()` returns it as a
string, so a short throwaway script prints exactly what the test runner sees:

```python
page.goto(URL)
print(page.locator("body").aria_snapshot())          # or scope to a card/dialog
```

That one output usually answers a dozen questions at once: which things are really headings, whether
cells and headers have accessible names, what state attributes are exposed (`[selected]`, `[checked]`,
`[disabled]`), and which controls are disabled. Write similar throwaway scripts to count matches and
dump `col-id`s or data attributes — they're cheap, and they're how the locator table gets built.

A deliberately failing `expect` also prints the aria snapshot in its call log, which is handy when a
test is already failing and you want to know why. But don't reach for that when you just want to *look*
at the tree — `aria_snapshot()` is faster and doesn't require breaking something first.

Playwright's snapshot is the **source of truth** because Playwright is what the tests run on. Other
tools that expose an accessibility tree — MCP servers, devtools panels, extensions — can serialize it
differently and omit computed names. Trusting one of those over Playwright has produced confidently
wrong locator tables that had to be retracted.

Driving a browser interactively (via a Playwright/CDP MCP server, if you have one) is useful for
*exploring* an unfamiliar app — opening menus, finding what exists. But treat anything it tells you
about accessible names as a hypothesis, and confirm through Playwright before committing a locator.

Read `references/gotchas-web-frameworks.md` **before** writing locators. It documents traps that cost
hours each: virtualized grids, portalled dropdowns, dialogs that persist after closing, toast
containers that mount on demand, generated ids that change every render. Most modern component
libraries hit several of these.

### 8. Write the tests

Structure them so a broken selector is a one-line fix: page objects for intent, a helpers module for
framework quirks, fixtures for setup. See `assets/` for starting templates.

Prefer role + accessible name. Fall back through label → placeholder → text → test-id → and if none
of those work, that element needs instrumenting — record it as a backlog item rather than reaching for
a positional selector. `nth(0)` is how suites rot.

Tests must leave the environment as they found it. If a test creates something, a fixture deletes it
even when the test fails.

**Find the teardown path before you write the create path.** Recordings often demonstrate creation and
never deletion, and some things simply cannot be deleted through the UI. Check while you're still in
the app (step 7) — if a flow creates a client, a batch and 27 rows with no way to remove them, that
changes the whole approach: unique names per run, an API cleanup route, or an agreement that this suite
only runs against a resettable environment. Discovering it at step 9 means rewriting.

### 9. Run the loop until green

Run → read the failure → fix → repeat. Read `references/verification-loop.md` for how to read
Playwright failures efficiently and what the common categories mean.

Expect several rounds. In the reference implementation this took six, and three of them exposed wrong
assumptions rather than typos — including one race that only appeared when the run was slowed down.

**Checkpoint — ask now:** if a failure suggests the app genuinely disagrees with the recording (a
control moved, a rule changed), ask. Do not rewrite the assertion to match current behaviour; that
silently discards the thing the recording was evidence of.

As at step 5, **if nobody is available to answer, don't stall.** Leave that test unwritten, record the
disagreement in the handoff with what the recording claimed and what you measured, and carry on with the
rest. Blocking the whole run on one unanswered question wastes the work you could still deliver.

Before calling something drift, rule out the boring explanations: a column scrolled out of the viewport
is not a missing column (the header row settles it), and a control you can't reach may be behind a menu
you haven't opened. Report drift with the evidence and what you eliminated.

### 10. Record one walkthrough video

One continuous recording of the whole verified flow, in the same order as the original, so a reviewer
can compare side by side. Read `references/recording-verification-video.md` — it covers why this is a
separate artifact from the CI suite, how to pace it, and the resolution trap that makes videos
unreadable.

### 11. Hand off with a traceability matrix

List every claim and mark it automated / partial / blocked / not built, with a count. "8 of 21 narrated
claims are automated" is an honest, useful handoff. "Tests written ✅" is not.

**The denominator is the claims spoken in the transcript.** That is the right unit because the
reviewer recognises their own words and can argue with the count. (Silent footage needs a different
rule; `references/silent-recordings.md` has one.)

Split the count by provenance — derived from the recording versus confirmed by a human — because
those carry very different confidence and a reader deserves to know which is which.

Include the assertions your tests make that the narration never mentioned — those are discoveries
from verification, and if any is wrong behaviour, the test is wrong.

### 11b. Park open questions where they will be found again

A question asked in a chat window is answered once and then lost. These questions are scoped to
specific tests and they recur — every regression run rediscovers the same disagreement — so they need
to live with the test, not in a conversation.

Commit `open-questions.md` to the test repo. One entry per unanswered question:

```markdown
## Q3 — economic Apply to Value absent on new rows
Claim:      29, 30 (see spec.md)
Blocks:     test_apply_to_value_adds_to_applied_list  [quarantined]
Observed:   A newly created economic indicator has no apply_to_value column and never
            appears in the Map to Ledger option list, including after a full reload.
Ruled out:  stale SPA cache; the Manage menu; the Applied Economic Indicators card.
Asked:      2026-09-17
Answer:     <blank until someone fills it in>
```

Then mark the test itself, so the reason travels with the code rather than living only in a document:

```python
@pytest.mark.quarantine("open-questions.md#q3")
```

**The point is the loop, not the paperwork.** Three things should surface these questions at the moment
someone can actually answer them:

- **The regression run** reports open questions alongside drift, so a scheduled job's output reads
  "2 drift changes, 3 tests quarantined pending answers" rather than silently carrying the gap.
- **The PR description** carries them, so a reviewer sees them while looking at the code.
- **The next run's drift check** finds the same disagreement and can point at the existing question
  instead of raising it again as though it were new.

An answered question is a one-line edit to that file plus removing a marker. An unanswered one stays
visible. Either way nobody re-derives it from scratch, which is what happens when the only record was
a conversation.

### 12. Land it as a pull request

Changes go to the test repo on a branch, never straight to its default branch.

**Only open the PR once the suite is green.** The one rule applies here: a PR containing an unverified
test is asking a reviewer to approve something nobody has run. If some tests are blocked, leave them
out and say so — a PR of four working tests plus a stated gap is reviewable; one of seven where three
are guesses is not.

Commit:

- the tests, page objects and fixtures
- `project-map.json` — the baseline the next run diffs against
- the plain-language spec, so a non-engineer can see what is covered
- any test fixture files the flow consumes, such as an uploaded spreadsheet

Do **not** commit: the walkthrough video (binary, megabytes, and it belongs with the PR rather than in
history), traces, screenshots, `node_modules`, a venv, or captured auth state. Check the repo's
`.gitignore` covers these and add them if not.

The PR description is the handoff: the traceability matrix, the open questions, the instrumentation
backlog, and a link to or note about where the walkthrough video is. That way the review and the
evidence live in the same place.

Say plainly in the description that the walkthrough video is the artifact for the tester to check, and
that their sign-off is what promotes the open questions into real assertions.

## What good output looks like

- Every test passes, and was seen to pass
- One walkthrough video at full viewport resolution
- A traceability matrix with an explicit covered/total count
- A list of open questions for the human, not silently-guessed answers
- An instrumentation backlog: elements that need `aria-label` or `data-testid`
- The target environment in the state you found it

## What to avoid

**Inventing assertions.** If the recording doesn't say what correct means, ask.

**Asserting what you merely observed.** A subtler version of the same mistake, and it strikes during
step 7 precisely because you're looking at the real app: you notice something true — exactly one option
is selected, there are three rows, a field shows a particular value — and assert it. But "true on the
day I looked" is not a requirement. Nobody asked for it, so nobody will recognise it when it fails, and
on shared data it will fail without any defect existing. Before writing an assertion, ask which claim
it serves. If it maps to nothing in the spec, it belongs in the open-questions list, not the test.

Things you *discover* during verification are still valuable — auto-filled fields, absent toasts,
disabled controls. Put them in the handoff as "assertions the narration never made, please confirm"
and only promote them to tests once a human has.

**Trusting a first draft.** Locators derived from source or frames are hypotheses until executed.

**Positional selectors.** `nth(2)` passes today and breaks when a row is added.

**Silent fallbacks.** If you add a workaround because the obvious approach failed, either prove the
obvious approach is genuinely broken or remove the workaround. A fallback that masks a regression is
worse than a failing test.

**Claiming end-to-end.** If the tests deep-link past the setup steps, they're integration tests of a
page. Say so. Overclaiming coverage is how people stop trusting a suite.

**Producing an artifact that isn't the thing.** A deliverable named `walkthrough.mp4` must be a
recording of the tests running. Do not satisfy that item by copying the input recording to the output
path — observed in a real run, and it reads as "walkthrough produced" to anyone scanning the file
list, however carefully the accompanying prose hedges. If you could not produce an artifact, don't
create a file where it would have gone. A missing deliverable with a stated reason is honest; a
plausible file in its place is not.

The same applies to any blocked step. An empty project map, a suite of skipped tests, a scaffold
script — all fine, provided they are named and reported as blocked rather than filed as done.

## Where this has been validated, and what to do when your stack differs

Be honest with yourself about the evidence base. The **method** — video for sequence, source for
vocabulary, running app for locators, plus the verify loop — is framework-independent, and the media and
transcript scripts work on any screen recording.

The **gotchas** are only as general as the libraries they name. They were measured on a React app using
AG Grid, a Base UI-family component library, and Sonner toasts, against an environment that needed **no
authentication**. Those libraries are widespread, so most entries transfer — but several important paths
are unexercised: SSO/`storageState` login flows, role-gated UI, apps whose routes carry no state (so
nothing is deep-linkable), iframes, multi-tab flows, native OS file dialogs, canvas-only interfaces, and
TypeScript projects.

So treat `references/gotchas-web-frameworks.md` as a **starting checklist, not a specification**. When
your app uses a different grid, dialog or toast library, the *categories* still apply — portalling,
virtualization, generated ids, mount-on-demand containers, strict-mode duplicates — but the specific
selectors and behaviours will not. Measure, don't assume; that's why every entry says how it was measured.

**Then write down what you find.** If you spend an hour discovering that some library virtualizes
differently, or that a dialog behaves unexpectedly, append it to the gotchas file with the measurement
that proves it. Each entry saves the next person that hour, and this reference only becomes genuinely
general by accumulating real measurements from real apps — not by anyone guessing in advance.

## Reference files

| File | Read when |
|---|---|
| `references/clarification-loop.md` | Before step 1, and at each checkpoint |
| `references/gotchas-web-frameworks.md` | Before writing any locator |
| `references/verification-loop.md` | During step 9, when failures aren't obvious |
| `references/recording-verification-video.md` | At step 10 |
| `references/silent-recordings.md` | Only if the recording has no narration and no re-record is possible |
| `references/running-with-codex.md` | Driving this from Codex rather than Claude Code |

`assets/` holds templates: `conftest.template.py`, `page_object.template.py`,
`walkthrough.template.py`. Python/pytest, but the TypeScript shapes are direct translations.

`scripts/`:

| Script | Purpose |
|---|---|
| `probe_media.py` | duration, resolution, bitrate, and whether the audio has speech |
| `extract_frames.py` | auto-tuned scene detection + uniform coverage + gap report |
| `grab_frame.py` | precise frames at native resolution, with crop and zoom — for reading detail |
| `read_transcript.py` | `.vtt` / `.srt` / `.docx` / `.txt` → tagged utterances |
| `build_project_map.py` | capture a structural baseline of the app, to commit alongside the tests |
| `check_drift.py` | diff that baseline against the app now; names the tests it makes stale |

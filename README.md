# video-to-playwright

Turn a screen recording of someone manually testing a web app into a **verified, passing Playwright
suite**, plus one walkthrough video so the person who made the recording can confirm the tests match
what they demonstrated — without reading code.

It works because a recording tells you **what a human did and why**, but never **how to address
anything in code**. Those come from different places, and the whole method is keeping them separate:
frames give the sequence, narration gives the expected results, the app source gives the locators.

---

## 1. What you must provide

Five inputs. Everything else the agent works out for itself.

| | What | Why it can't be guessed |
|---|---|---|
| 1 | **Recording** — `.mp4` / `.webm` / `.mov` | — |
| 2 | **Transcript** — `.vtt` preferred, `.docx` accepted | Narration is the *only* source of expected results. A silent recording cannot produce assertions |
| 3 | **Test repo** (clone URL or path) | Where tests, the project map and the PR land. **Written to** |
| 4 | **App repo** (clone URL or path) | Read for the locator vocabulary. **Never modified** |
| 5 | **Dev URL** | Every locator is verified against it. Skip this if the address bar is readable in the footage |

Plus one question worth answering up front, because it shapes the entire suite:

> **May the tests create and delete data in that environment?** If yes they seed their own fixtures.
> If no they can only assert on whatever happens to be there.

**Ask for the `.vtt` if you only have a `.docx`.** Teams exports both. A `.vtt` brackets each line
with start and end times; a `.docx` gives one offset per speaker turn. Without per-line times,
narration can only be matched to frames by content — slower and less certain.

## 2. Run it

Both paths do the same thing. The only difference is that Claude Code finds the skill by itself and
Codex has to be told.

**Claude Code**

```bash
git clone https://github.com/Praneethvvs/video-to-playwright.git ~/.claude/skills/video-to-playwright
```

Then just ask, naming the five inputs:

> Turn `recordings/checkout.mp4` + `recordings/checkout.vtt` into Playwright tests.
> Test repo `~/work/checkout-e2e`, app repo `~/work/checkout-ui`, dev URL `https://checkout.dev.example.com`.
> Tests may create and delete their own data.

**Codex**

```bash
git clone https://github.com/Praneethvvs/video-to-playwright.git
codex
```

Then the same request, prefixed with the instruction to read the workflow — Codex does not
auto-discover skill files, and without this you get a generic attempt instead:

> Read `video-to-playwright/SKILL.md` and follow it. Turn `recordings/checkout.mp4` + …

For unattended runs see [Running unattended](#running-unattended) below — there are two traps that
each cost a full run.

## 3. What happens, in order

| | Step | You are involved |
|---|---|---|
| 1 | Reads the transcript, probes the video, pulls frames | — |
| 2 | Reads both repos: language, runner, layout, existing page objects | — |
| 3 | Captures or checks `project-map.json`, a structural baseline of the app | — |
| 4 | Drafts a plain-language spec in `specs/`, with gaps left as gaps | — |
| 5 | **Asks you a batch of questions** | **yes — ~30 min** |
| 6 | Verifies every locator against the running app | — |
| 7 | Writes the tests and runs them until green | — |
| 8 | Records the walkthrough video | — |
| 9 | **Asks anything still unresolved**, then opens a PR | **yes** |

### When it asks questions

Questions arrive **batched at checkpoints**, not drip-fed, and only *after* it has watched the
footage — so they come with its best guess attached and are usually one word to answer. Expect
roughly three per checkpoint in the flow:

- **What exactly were you checking here?** "A banner appeared" / "it said this precise text" / "the
  row reached this status" are three different tests.
- **Where did this data come from?** The recording shows the result of setup it never captured.
- **What would you have called a failure?**

If **nobody is available**, say so up front. It will deliver what it can and list the rest as open
questions rather than stalling — or guessing, which it will not do.

**It will not invent an assertion to fill a gap.** If the narration never says what "correct" means,
that becomes an open question, and if nobody answers it, an honest hole in the traceability matrix.
A visible hole can be filled; fabricated coverage is invisible until it costs you a release.

### Where the tests are written

Into the **test repo**, matching whatever conventions it already has — its layout, its naming, its
existing page objects. A suite that looks foreign to the people maintaining it gets rewritten.

If that repo is empty, this is the layout used, and it tells you before writing anything:

```
e2e/
  tests/          test_<feature>.py      one file per flow in the recording
  pages/          page objects           one class per screen
  helpers/        framework quirks       grids, toasts, dialogs
  conftest.py     fixtures
  .artifacts/     traces and screenshots (gitignored)
specs/            the plain-language spec
project-map.json  the structural baseline for drift detection
```

The **app repo is never modified**. Changes to the test repo land as a pull request.

## 4. What you get

- A passing suite, structured so a broken selector is a one-line fix
- **One** continuous walkthrough video at viewport resolution, in the recording's order
- A traceability matrix: every narrated claim marked automated / partial / blocked / not built
- A list of open questions — not silently-guessed answers
- An instrumentation backlog: elements needing `aria-label` or `data-testid`

**Realistic scale.** On a 10-minute narrated recording of a single page: 27 of 37 narrated claims
automated, 20 passing tests, 8 open questions, ~90 minutes of machine time, and it still needs a
human review pass. "Point at a video, get a suite" is not the target. "Point at a video, get a
reviewed draft plus a verification video" is.

---

## The one rule

**Never ship a test containing a locator or assertion that has not been executed against the running
application.** A plausible-looking test that never ran is worse than no test: it reads as coverage,
passes review, and fails silently later.

There is a subtler version that strikes while you are looking at the real app: you notice something
true — exactly one option selected, three rows present — and assert it. *True when I looked* is not a
requirement. Nobody asked for it, nobody recognises it when it breaks, and on shared data it will
break with no defect present.

## Running unattended

```bash
codex exec -C <workdir> -s danger-full-access --skip-git-repo-check \
  -o last-message.txt \
  "Read TASK.md in this directory and carry it out exactly."
```

| Flag | Why |
|---|---|
| `-C <dir>` | Working directory; everything resolves relative to it |
| `-s <policy>` | Sandbox policy — **the obvious choice is wrong, see below** |
| `--skip-git-repo-check` | Required when the working directory is not a git repo |
| `-o <file>` | Writes the final message to a file, so the run leaves a readable outcome |

**Trap 1: `workspace-write` is not enough, and failing looks like passing.** The workflow's whole
point is verifying locators against the running app, which needs to launch a browser and reach the
network. Under `workspace-write` that fails with `WinError 5: Access is denied` on Windows — and a
blocked run produces all-**skipped** tests, a map with every route **unreachable**, and **exit code
0**. The output is honest about being blocked; the exit code invites reading it as a pass.

> **Before believing any run passed, check two things:** the project map has reachable routes, and
> the tests report passes rather than skips.

**Trap 2: put the task in a file, never in the argument.** A multi-line prompt passed as a shell
argument gets split on whitespace and its fragments parsed as flags — from PowerShell that produced
`error: unexpected argument 'a' found`. Write a `TASK.md` naming the five inputs and pass a one-line
prompt pointing at it.

**Finding the binary on Windows.** `codex` is often not on `PATH`:
`%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe` — the `<hash>` changes between versions, so glob
for it. Run `codex login status` before a long job.

[`references/running-with-codex.md`](references/running-with-codex.md) has the rest.

## Requirements

- An agent harness that can read files and run shell commands — Claude Code, Codex, or another
- **Python 3.10+** with ffmpeg. Not on `PATH`? `pip install imageio-ffmpeg` needs no admin rights
- **Playwright** — `pytest-playwright` or `@playwright/test`
- A **running instance** of the app. Not optional; verification against it is the point

## What's in here

```
SKILL.md                          the workflow: 12 steps, plus 1b, 1c and 11b
AGENTS.md                         always-on rules, picked up automatically by Codex
references/
  gotchas-web-frameworks.md       locator traps, with the measurements that prove them
  clarification-loop.md           what to ask, when, and what never to ask
  verification-loop.md            reading failures, and knowing when to stop
  recording-verification-video.md producing the walkthrough
  silent-recordings.md            no narration, and the recording cannot be redone
  running-with-codex.md           codex exec detail
scripts/
  probe_media.py                  duration, resolution, whether the audio has speech
  extract_frames.py               scene detection + uniform coverage + gap report
  grab_frame.py                   precise frames at native resolution, with crop and zoom
  read_transcript.py              .vtt/.srt/.docx/.txt/.md -> attributed, timestamped utterances
  build_project_map.py            capture the structural baseline of the running app
  check_drift.py                  compare the app against that baseline
assets/                           conftest, page-object and walkthrough templates
tests/                            the transcript parser's own tests
docs/usage.html                   the same guide, formatted for sharing
```

The scripts are plain Python and need only ffmpeg, so they are useful on their own — pulling frames
or reading a Teams transcript needs no agent at all.

## Honest scope

The **method** is framework-independent. The **gotchas** are only as general as the libraries they
name: they were measured on a React app using AG Grid, a Base UI-family component library and Sonner
toasts, against an environment requiring **no authentication**.

Genuinely unexercised: SSO / `storageState` login flows and role-gated UI; apps with no deep-linkable
routes; iframes, multi-tab flows, native file dialogs, canvas-only interfaces; TypeScript projects
(the templates are Python and the TS translations are claimed, not tested); CI integration.

Treat [`references/gotchas-web-frameworks.md`](references/gotchas-web-frameworks.md) as a **starting
checklist, not a specification**. The *categories* transfer — portalling, virtualization, generated
ids, mount-on-demand containers, strict-mode duplicates — but the specifics will not. Measure.

**Then write down what you find.** This reference becomes general by accumulating real measurements
from real apps, not by anyone guessing in advance. A PR adding a measured gotcha for another stack is
the most valuable contribution here.

## Licence

MIT — see [LICENSE](LICENSE).

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

### Claude Code

```bash
git clone https://github.com/Praneethvvs/video-to-playwright.git ~/.claude/skills/video-to-playwright
```

It discovers the skill itself. Just ask, naming the five inputs:

> Turn `recordings/checkout.mp4` + `recordings/checkout.vtt` into Playwright tests.
> Test repo `~/work/checkout-e2e`, app repo `~/work/checkout-ui`, dev URL
> `https://checkout.dev.example.com`. Tests may create and delete their own data.

### Codex

A full run takes tens of minutes, so `codex exec` is the way to run it. The request goes in a file
rather than on the command line — see [why](#why-the-task-goes-in-a-file).

**Step 1.** Clone the workflow into the directory you want to work from:

```bash
cd ~/work/checkout-run
git clone https://github.com/Praneethvvs/video-to-playwright.git
```

**Step 2.** Copy the task template into that same directory and fill it in:

```bash
cp video-to-playwright/assets/TASK.template.md TASK.md
```

Open `TASK.md` and replace the five inputs with your own paths. It is commented throughout, and the
placeholders are the questions the agent would otherwise have to ask or guess at. Nothing else needs
editing.

**Step 3.** Run it:

```bash
codex exec -C . -s danger-full-access --skip-git-repo-check \
  -o last-message.txt \
  "Read TASK.md in this directory and carry it out exactly."
```

| Flag | Why |
|---|---|
| `-C <dir>` | Working directory; `TASK.md` and every path in it resolve relative to this |
| `-s <policy>` | Sandbox policy — **the obvious choice is wrong, see below** |
| `--skip-git-repo-check` | Required when the working directory is not itself a git repo |
| `-o <file>` | Writes the final message to a file, so an unattended run leaves a readable outcome |

**Step 4.** Read `last-message.txt` and check two numbers before believing it passed — see
[Checking a run actually passed](#checking-a-run-actually-passed).

Prefer to drive it by hand? Run `codex` interactively and paste the contents of your `TASK.md` as
the first message. Same result, but you have to sit with it.

#### The sandbox must allow a browser and the network

`workspace-write` sounds sufficient — the workflow writes frames and test files. It is not. The
whole point is verifying locators against the *running* application, which means launching a browser
and reaching it over the network. Under `workspace-write` that fails with `WinError 5: Access is
denied` on Windows.

```bash
codex exec -s danger-full-access ...                                                   # sandbox elsewhere: a container, a VM
codex exec -s workspace-write -c 'sandbox_permissions=["disk-full-read-access"]' ...   # or grant what is needed
```

#### Checking a run actually passed

A blocked run produces all-**skipped** tests, a project map with every route **unreachable**, and
**exit code 0**. The output is honest about being blocked; the exit code invites reading it as a
pass. `TASK.md` asks the agent to state both numbers, so:

```bash
grep -iE "reachable|skipped|passed" last-message.txt
```

Routes reachable, and passes rather than skips. If you see skips and unreachable routes, the sandbox
blocked it — nothing was actually verified.

#### Why the task goes in a file

A multi-line prompt passed as a shell argument gets split on whitespace and its fragments parsed as
flags. From PowerShell, a here-string prompt containing blank lines and hyphens produced:

```
error: unexpected argument 'a' found
```

That is an argument-quoting problem rather than a Codex bug, and it wastes a full run before you
notice. A task file is better practice anyway: it records exactly what was asked, which matters when
comparing runs or re-running after a change.

#### Finding the binary on Windows

`codex` is frequently not on `PATH` even when installed:
`%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe` — the `<hash>` changes between versions, so glob
for it rather than hardcoding. Run `codex login status` before starting a long job.

[`references/running-with-codex.md`](references/running-with-codex.md) has the rest.

## 3. The steps

1. Probe the video and read the transcript
2. Extract frames
3. Read both repos
4. Capture or check the project map
5. Draft the spec
6. **Ask you a batch of questions** — about 30 minutes
7. Build the locator vocabulary from the app source
8. Verify every locator against the running app
9. Write the tests
10. Run them until green
11. Record the walkthrough video
12. **Ask anything still unresolved**, then open a pull request

Steps 6 and 12 are the only ones that need you.

### What each step does

**1. Probe the video and read the transcript.** Checks the audio actually contains speech — a silent
recording cannot produce assertions, and it says so rather than proceeding. Parses the transcript
into timestamped, attributed utterances.

**2. Extract frames.** Scene detection tuned to the recording, plus uniform coverage so nothing is
missed between scene changes, plus a report of gaps where it is unsure.

**3. Read both repos.** The test repo for its language, runner, layout and existing page objects;
the app repo for how elements are actually addressed in code. The app repo is never written to.

**4. Capture or check the project map.** A structural baseline of the running app — routes, roles,
names. If one exists it checks for drift first, because drift found up front is a fact you can
report, while the same drift found halfway through writing a suite looks like your locators are
wrong and sends you hunting a bug that does not exist.

**5. Draft the spec.** A plain-language description in `specs/`: preconditions, numbered steps, and
assertions quoted from the narration. Gaps are left as gaps rather than filled in.

**6. Ask you a batch of questions.** See [When it asks questions](#when-it-asks-questions).

**7. Build the locator vocabulary.** From the application source, not from the video — pixels carry
no roles or test-ids.

**8. Verify every locator against the running app.** The rule the whole method exists for. A locator
that has not been executed does not go in a test.

**9. Write the tests.** Page objects for intent, a helpers module for framework quirks, fixtures for
setup, so a broken selector is a one-line fix.

**10. Run them until green.** Real passes. A skipped test is not a passing test.

**11. Record the walkthrough video.** One continuous take at viewport resolution, in the recording's
order, so the person who made the original can confirm it without reading code.

**12. Hand off.** A traceability matrix, the open-questions list, and a pull request against the test
repo.

### When it asks questions

Questions arrive **batched at checkpoints**, not drip-fed, and only *after* it has watched the
footage — so they come with its best guess attached and are usually one word to answer. Expect
roughly three per checkpoint in the flow:

- **What exactly were you checking here?** "A banner appeared" / "it said this precise text" / "the
  row reached this status" are three different tests.
- **Where did this data come from?** The recording shows the result of setup it never captured.
- **What would you have called a failure?**

If **nobody is available**, say so — `TASK.md` has a field for it. It will deliver everything that is
not blocked and list the rest as open questions rather than stalling.

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
assets/
  TASK.template.md                copy to TASK.md and fill in, for unattended Codex runs
  conftest.template.py            fixtures, with a comment per setting explaining what it prevents
  page_object.template.py         page-object shape
  walkthrough.template.py         the walkthrough recorder
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

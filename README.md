# video-to-playwright

A [Claude Code](https://claude.com/claude-code) skill that turns a screen recording of someone manually
testing a web app — plus the transcript, if there is one — into a **verified, passing Playwright suite**,
and produces a single walkthrough video so the person who made the original recording can confirm the
tests actually match what they demonstrated.

It is built around one idea: a recording tells you **what a human did and why**, but it cannot tell you
**how to address anything in code**. Those come from different places, and keeping them separate is the
method.

| Input | Supplies | Cannot supply |
|---|---|---|
| Video frames | the sequence, the UI state at each step | durable locators — pixels carry no roles or test-ids |
| Narration / transcript | intent, expected outcomes, business rules, prerequisites | anything the speaker didn't say aloud |
| App source + running app | how to address every element | which flows anyone cares about |

## The one rule

**Never ship a test containing a locator or assertion you have not confirmed against the running
application.** A plausible-looking test that was never executed is worse than no test: it reads as
coverage, passes review, and fails silently later.

The corollary matters just as much — **never invent an assertion to fill a gap.** If the recording
doesn't say what "correct" means, that becomes a question for a human, and if nobody answers it, it
becomes an honest hole in the traceability matrix. An honest hole is visible and someone can fill it.
Fabricated coverage is invisible until it costs you a release.

**Full usage guide:** [docs/usage.html](docs/usage.html) — open it locally, or read the sections below.

## Install

Nothing here is specific to one agent. `SKILL.md` is an instruction set in Markdown and the scripts are
plain Python, so any harness that can read files and run shell commands will do.

### Claude Code

Skills are discovered from `~/.claude/skills/`, and trigger from the request itself:

```bash
git clone https://github.com/Praneethvvs/video-to-playwright.git ~/.claude/skills/video-to-playwright
```

> Here's a recording of our release checks: `~/recordings/checkout-flow.mp4`, and the transcript.
> I need Playwright tests for this.

### Codex

Codex **does not auto-discover skill files**, so the "read it and follow it" instruction is doing real
work. Without it you get a generic attempt rather than the workflow.

```bash
git clone https://github.com/Praneethvvs/video-to-playwright.git
codex
```

> Read `video-to-playwright/SKILL.md` and follow it. Here's the recording:
> `recordings/checkout-flow.mp4`, and the transcript: `recordings/checkout-flow.vtt`.

`AGENTS.md` carries the always-on rules for Codex sessions, so cloning this *into* a project picks them
up automatically. The YAML frontmatter at the top of `SKILL.md` is Claude Code's discovery metadata and
is inert elsewhere.

#### Unattended, with `codex exec`

A full run takes tens of minutes, so this is usually what you want:

```bash
codex exec -C <workdir> -s danger-full-access --skip-git-repo-check \
  -o last-message.txt \
  "Read TASK.md in this directory and carry it out exactly."
```

| Flag | Why |
|---|---|
| `-C <dir>` | Working directory; everything resolves relative to it |
| `-s <policy>` | Sandbox policy — **see the trap below, the obvious choice is wrong** |
| `--skip-git-repo-check` | Required when the working directory is not a git repo |
| `-o <file>` | Writes the final message to a file, so an unattended run leaves a readable outcome |
| `--json` | Streams structured events, to watch progress programmatically |

#### Two traps that each cost a full run

**1. `workspace-write` is not enough, and failing looks like passing.**

It sounds sufficient — the workflow writes frames and test files. But the whole point is verifying
locators against the *running application*, which means launching a browser and reaching the app over
the network. Under `workspace-write` that fails with `WinError 5: Access is denied` on Windows.

What makes this expensive is how it presents. A blocked run produces a suite of tests that are all
**skipped**, a project map with every route **unreachable**, and **exit code 0**. The output is honest
about being blocked; the exit code invites you to read it as a pass.

```bash
codex exec -s danger-full-access ...                                        # sandbox elsewhere: a container, a VM
codex exec -s workspace-write -c 'sandbox_permissions=["disk-full-read-access"]' ...   # or grant what is needed
```

> **Before believing any run succeeded, check two things:** the project map has reachable routes, and
> the test run reports passes rather than skips.

**2. Put the task in a file, never in the argument.**

A multi-line prompt passed as a shell argument gets split on whitespace and its fragments parsed as
flags. From PowerShell, a here-string prompt containing blank lines and hyphens produced:

```
error: unexpected argument 'a' found
```

That is an argument-quoting problem rather than a Codex bug, and it wastes a run before you notice.
Write a `TASK.md` and pass a one-line prompt pointing at it:

```markdown
# Task

Read `video-to-playwright/SKILL.md` in this directory and follow it.

**The request:** "Here's a recording of our release checks: `recordings/checkout.mp4`, and the
transcript `recordings/checkout.vtt`. I need a Playwright suite, and a video I can show the tester."

**Environment:** `python` here has `imageio-ffmpeg` installed. <Say whether a human is available to
answer questions, and whether the agent may create and delete data in the target environment.>

## Deliverables
1. The test suite  2. A passing test run  3. The walkthrough video  4. The traceability matrix
```

A task file is better practice anyway: it records exactly what was asked, which matters when comparing
runs or re-running after a change.

#### Finding the binary on Windows

`codex` is frequently not on `PATH` even when installed:

```
%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe
```

The `<hash>` changes between versions, so glob for it rather than hardcoding. A `~/.codex/` holding
`config.toml` and `plugins/` is a reliable sign it is installed even when the launcher isn't findable.
Run `codex login status` before starting a long job.

#### Two things to state that the agent cannot work out

- **Whether a human is available to answer questions.** The workflow batches questions to a person at
  checkpoints. If nobody is there it should deliver what it can and list the rest rather than stall.
- **Whether it may create and delete data in the target environment**, and what naming convention to
  use so anything left behind is identifiable.

**[`references/running-with-codex.md`](references/running-with-codex.md)** has the rest: what a real
run looked like end to end, and which behaviours to watch for when evaluating a different agent.

### Any other agent

The mechanics are plain file reads and shell commands, so nothing here is Claude- or Codex-specific.
Point the harness at `SKILL.md` the same way, and check the same two things before believing a run
passed.

### Without any model at all

Roughly two-thirds of this is useful with no agent involved: the scripts pull frames and read
transcripts on their own, `references/gotchas-web-frameworks.md` is documentation a developer can read
before writing locators by hand, and `playwright codegen` is free and records a flow into runnable code.
Slower than the full workflow, but no licence required.

## Requirements

- **Claude Code** (or any agent harness that can read the skill and run shell commands)
- **Python 3.10+** with an ffmpeg binary. If ffmpeg isn't on `PATH`, the bundled wheel needs no admin
  rights and works inside a project venv: `pip install imageio-ffmpeg`
- **Playwright** — Python (`pytest-playwright`) or TypeScript (`@playwright/test`)
- Access to a **running instance** of the app under test. This is not optional; verification against the
  live app is the whole point.

## What's in here

```
SKILL.md                                  the workflow: 12 steps, plus 1b, 1c and 11b
AGENTS.md                                 always-on rules, picked up automatically by Codex
references/
  gotchas-web-frameworks.md               locator traps, with the measurements that prove them
  clarification-loop.md                   what to ask, when, and what never to ask
  verification-loop.md                    reading failures, and knowing when to stop
  recording-verification-video.md         producing the walkthrough
  silent-recordings.md                    no narration, and the recording cannot be redone
  running-with-codex.md                   codex exec flags, and the quoting trap that breaks prompts
scripts/
  probe_media.py                          duration, resolution, and whether the audio has speech
  extract_frames.py                       auto-tuned scene detection + uniform coverage + gap report
  grab_frame.py                           precise frames at native resolution, with crop and zoom
  read_transcript.py                      .vtt / .srt / .docx / .txt / .md -> attributed utterances
  build_project_map.py                    capture the structural baseline of the running app
  check_drift.py                          compare the app against that baseline before trusting a pass
assets/                                   conftest, page-object and walkthrough templates
tests/                                    the transcript parser's own tests
docs/usage.html                           the same guide, formatted for sharing
```

The scripts are plain Python with no dependencies beyond ffmpeg, so they're useful on their own if you
just want to pull frames or read a Teams transcript.

## The gotchas file is the most reusable part

Locator traps that silently break the obvious approach, each with the measurement that demonstrates it:

- Off-screen grid columns are usually still in the DOM — and how to tell "scrolled away" from "genuinely
  absent", which is the difference between a locator bug and app drift
- Pinned grid columns split one row across three DOM elements sharing a `row-id`
- DOM order is not display order, so never address rows by index
- Dropdown options are portalled out of the dialog that opened them
- `[role=dialog]` nodes persist after closing — assert hidden, not absent
- Toast containers may be mounted on demand, which breaks negative assertions confusingly
- Generated ids (`base-ui-«r36»`, `radix-:r1:`) regenerate every render
- A missing `aria-label` proves nothing; for most roles the name comes from text content
- `expect.toPass()` is JavaScript-only, and what to use in Python instead

## Honest scope

The **method** is framework-independent, and the media/transcript scripts work on any recording.

The **gotchas** are only as general as the libraries they name. They were measured on a React app using
AG Grid, a Base UI-family component library and Sonner toasts, against an environment that required **no
authentication**. Those libraries are widespread so most entries transfer, but several paths are
genuinely unexercised:

- SSO / `storageState` login flows, and role-gated UI
- apps whose routes carry no state, so nothing is deep-linkable
- iframes, multi-tab flows, native OS file dialogs, canvas-only interfaces
- TypeScript projects (the templates are Python; the TS shapes are claimed to be direct translations,
  but that claim is untested)
- CI integration

Treat `references/gotchas-web-frameworks.md` as a **starting checklist, not a specification**. When your
stack differs, the *categories* still apply — portalling, virtualization, generated ids, mount-on-demand
containers, strict-mode duplicates — but the specifics will not. Measure, don't assume.

**And then write down what you find.** If you spend an hour discovering how some library virtualizes,
append it with the measurement that proves it. This reference only becomes genuinely general by
accumulating real measurements from real apps, not by anyone guessing in advance. Pull requests adding
measured gotchas for other stacks are the most valuable contribution you can make.

## What it produces

- A passing test suite, structured so a broken selector is a one-line fix
- **One** continuous walkthrough video at viewport resolution, in the recording's order, so a
  non-engineer can verify it
- A traceability matrix: every claim from the recording marked automated / partial / blocked / not built,
  with an explicit count and the provenance of each assertion
- A list of open questions for a human — not silently-guessed answers
- An instrumentation backlog: elements needing `aria-label` or `data-testid`, which usually fixes real
  accessibility defects too

## Realistic expectations

On a 10-minute narrated recording of a single page, in testing: **27 of 37 narrated claims automated**,
20 passing tests, 8 open questions, and a walkthrough video — in roughly 90 minutes of machine time,
still needing a human review pass.

"Point at a video, get a suite" is not the target. "Point at a video, get a reviewed draft plus a
verification video" is, and that is still a large win over writing them by hand.

Expect the first draft of any locator to be partly wrong. That isn't a prompting failure — locators
derived from frames or source are hypotheses, and the only way to test a hypothesis about a running app
is to run it. The verify loop is the product.

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Microsoft or the Playwright project. Examples in the reference files
use a fictional sample application.

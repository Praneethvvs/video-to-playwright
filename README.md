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
git clone https://github.com/<you>/video-to-playwright.git ~/.claude/skills/video-to-playwright
```

> Here's a recording of our release checks: `~/recordings/checkout-flow.mp4`, and the transcript.
> I need Playwright tests for this.

### Codex, or any other agent

Clone it anywhere, then **point the agent at the workflow explicitly** — Codex does not auto-load skill
files the way Claude Code does, so it needs the instruction:

```bash
git clone https://github.com/<you>/video-to-playwright.git
```

> Read `video-to-playwright/SKILL.md` and follow it. Here's the recording:
> `recordings/checkout-flow.mp4`, and the transcript: `recordings/checkout-flow.vtt`.

`AGENTS.md` carries the always-on rules for Codex sessions, so cloning this *into* a project picks those
up automatically. The YAML frontmatter at the top of `SKILL.md` is Claude Code's discovery metadata and
is inert elsewhere.

**[`references/running-with-codex.md`](references/running-with-codex.md)** has the verified detail:
the `codex exec` flags for unattended runs, where the binary hides on Windows, and the
argument-quoting trap that silently breaks multi-line prompts. This path has been run end to end, not
assumed.

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
SKILL.md                                  the 11-step workflow
references/
  gotchas-web-frameworks.md               locator traps, with the measurements that prove them
  clarification-loop.md                   what to ask, when, and what never to ask
  verification-loop.md                    reading failures, and knowing when to stop
  recording-verification-video.md         producing the walkthrough
scripts/
  probe_media.py                          duration, resolution, and whether the audio has speech
  extract_frames.py                       auto-tuned scene detection + uniform coverage + gap report
  grab_frame.py                           precise frames at native resolution, with crop and zoom
  read_transcript.py                      .vtt / .srt / .docx / .txt -> tagged utterances
assets/                                   conftest, page-object and walkthrough templates
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

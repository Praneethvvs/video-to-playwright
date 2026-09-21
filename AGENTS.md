# AGENTS

Always-on guidance for agent sessions in this repository, including Codex.

## What this repo is

A workflow for turning a screen recording of someone manually testing a web app into a verified,
passing Playwright suite, plus a walkthrough video a non-engineer can sign off.

`SKILL.md` holds the workflow, numbered 1 through 12 with sub-steps at 1b, 1c, 1d and 11b. Read it before starting that kind of task — it is the
instruction set, not background reading.

## Invocation

Claude Code auto-discovers this from `~/.claude/skills/` and triggers on a matching request. **Codex
does not auto-load skill files**, so point it at the workflow explicitly:

> Read SKILL.md in this repo and follow it. Here's the recording: `<path>`, and the transcript:
> `<path>`.

If you have cloned this alongside a project rather than into it, give the path:
`Read ~/skills/video-to-playwright/SKILL.md and follow it.`

For unattended runs, the exact `codex exec` invocation and the argument-quoting trap that breaks
multi-line prompts are in `references/running-with-codex.md`.

The YAML frontmatter at the top of `SKILL.md` is Claude Code's discovery metadata. It is inert
everywhere else and can be ignored.

## The rules that matter most

These hold regardless of which step you are on:

- **Never ship a test containing a locator or assertion that has not been executed against the running
  application.** A test that was never run reads as coverage, passes review, and protects nothing. This
  governs test code, not the spec — the spec is *supposed* to carry unverified candidates, clearly
  marked as such.
- **Never invent an assertion to fill a gap.** If the recording does not say what "correct" means, that
  is a question for a human. If nobody answers, the test is not written and the gap is stated openly.
- **Never assert what you merely observed.** During verification you will notice things that are true —
  exactly one option selected, three rows present. True-when-you-looked is not a requirement. If an
  assertion maps to nothing anyone asked for, it belongs in the open-questions list.
- **Read `references/gotchas-web-frameworks.md` before writing any locator.** It documents traps that
  each cost hours to find, with the measurements that prove them.
- **Leave the target environment as you found it.** Record the baseline first, and verify it afterwards.

## Reference files

| File | Read when |
|---|---|
| `SKILL.md` | Starting the task; it is the workflow |
| `references/gotchas-web-frameworks.md` | Before writing any locator |
| `references/clarification-loop.md` | Deciding what to ask a human, and when |
| `references/verification-loop.md` | Failures aren't obvious, or deciding whether you're done |
| `references/recording-verification-video.md` | Producing the walkthrough |
| `references/silent-recordings.md` | The recording has no narration and cannot be redone |
| `references/running-with-codex.md` | Driving this from Codex rather than Claude Code |
| `testboard/README.md` | The optional control panel offered at step 1d |

## Tooling notes

The media scripts in `scripts/` are plain Python and need an ffmpeg binary. If one is not on `PATH`,
`pip install imageio-ffmpeg` provides it inside a venv with no admin rights.

They are useful on their own, with no model involved, if you only want to pull frames from a video or
read a Teams transcript.

## Execution style

- Verify rather than assume. Every claim in the reference files says how it was measured; hold your own
  output to the same standard.
- When something is blocked, deliver everything that isn't and state the blocker with evidence.
- Do not claim a command or test passed unless it was actually run in this session.

# Running this with Codex

Verified end to end on Windows with `codex-cli 0.154.0-alpha.6.2`, driving the workflow through the
spec stage on a real recording. The mechanics are plain file reads and shell commands, so nothing here
is Claude-specific — but Codex needs pointing at the workflow, and there is one argument-passing trap
worth knowing before you hit it.

## Interactive

Simplest path. From a directory containing the workflow:

```
codex
```

Then in the session:

> Read `video-to-playwright/SKILL.md` and follow it. Here's the recording: `recordings/checkout.mp4`,
> and the transcript: `recordings/checkout.vtt`.

Codex does not auto-discover skill files, so the "read X and follow it" instruction is doing real
work. Without it you get a generic attempt rather than the workflow.

## Non-interactive

`codex exec` runs to completion without prompting, which is what you want for a job that takes tens of
minutes:

```bash
codex exec -C <workdir> -s workspace-write --skip-git-repo-check \
  -o last-message.txt \
  "Read TASK.md in this directory and carry it out exactly."
```

| Flag | Why |
|---|---|
| `-C <dir>` | Working directory. Everything is resolved relative to this. |
| `-s <policy>` | Sandbox policy. **`workspace-write` is not enough — see below.** |
| `--skip-git-repo-check` | Required if the working directory is not a git repo. |
| `-o <file>` | Writes the final message to a file, so you can read the outcome after an unattended run. |
| `--json` | Streams structured events, if you want to watch progress programmatically. |

## The sandbox must allow a browser and the network

`workspace-write` sounds sufficient — the workflow writes frames and test files — but it is not. This
workflow's whole value is verifying locators against the running application, and that needs to launch
a browser process and reach the app over the network. Under `workspace-write` the launch fails with
`WinError 5: Access is denied` on Windows.

Choose one of:

```bash
# sandboxed elsewhere (a container, a dedicated VM)
codex exec -s danger-full-access ...

# or grant what is actually needed
codex exec -s workspace-write -c 'sandbox_permissions=["disk-full-read-access"]' ...
```

The failure is worth recognising because it does **not** look like a permissions error from the
outside. A well-behaved run under the wrong sandbox produces a suite of tests that are all *skipped*,
a project map with every route marked unreachable, and an exit code of 0. Nothing is wrong with the
output — it is honest about being blocked — but "exit 0" invites you to read it as a pass.

**So check two things before believing a run succeeded:** that the project map has reachable routes,
and that the test run reports passes rather than skips.

## The trap: put the task in a file, not the argument

**A multi-line prompt passed as a shell argument gets mangled.** From PowerShell, a here-string prompt
containing blank lines and hyphens produced:

```
error: unexpected argument 'a' found
```

The prompt was being split on whitespace and fragments were parsed as flags. This is an argument-quoting
problem, not a Codex bug, and it wastes a full run before you notice.

Write the task to a Markdown file and pass a one-line prompt pointing at it:

```markdown
# Task

Read `video-to-playwright/SKILL.md` in this directory and follow it.

**The request:** "Here's a recording of our release checks: `recordings/checkout.mp4`, and the
transcript `recordings/checkout.vtt`. I need a Playwright suite for this, and a video I can show the
tester."

**Environment notes:** `python` here has `imageio-ffmpeg` installed, so the skill's scripts will find
an ffmpeg binary. <Say whether a human is available to answer questions.>

## Deliverables
1. The test suite.
2. A passing test run.
3. The walkthrough video.
4. The traceability matrix.
```

Then `codex exec ... "Read TASK.md in this directory and carry it out exactly."`

This is better practice anyway: the task file is a record of exactly what was asked, which matters when
comparing runs or re-running after a change.

## Finding the binary on Windows

`codex` is often not on `PATH` even when installed. Look here:

```
%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe
```

The `<hash>` segment changes between versions, so glob for it rather than hardcoding. `~/.codex/`
holding `config.toml` and a `plugins/` directory is a reliable sign it is installed even when the
launcher isn't findable.

Check auth with `codex login status` before starting a long run.

## What to expect

On a ~3.5 minute recording, spec stage only, it ran about 25 minutes and produced a spec, a summary and
a critique. It read the frame-triage guidance and acted on it — sweeping URL-bar crops into their own
folder first, then full frames — which is the behaviour the workflow asks for.

Codex also respected the parts that matter most: it detected the silent audio track, produced candidate
assertions without promoting any to requirements, refused to claim an outcome the recording never
showed, and listed its open questions rather than guessing. Those are the behaviours to watch for if
you are evaluating a different agent — capability is rarely the problem, discipline is.

## Telling it the environment

Two things are worth stating in the task file because the agent cannot determine them:

- **Whether a human is available to answer questions.** The workflow has checkpoints that batch
  questions to a person; if nobody is there it should deliver what it can and list the rest rather
  than stalling.
- **Whether it may create and delete data in the target environment**, and any naming convention you
  want for records it creates so leftovers are identifiable.

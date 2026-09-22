# Task

Read `video-to-playwright/SKILL.md` in this directory and follow it.

Adjust that path if you cloned the workflow somewhere else. This file is the whole request: the
agent reads it and nothing else, so anything you leave as a placeholder below is something it will
have to ask about or guess at.

## Inputs

| | |
|---|---|
| Recording | `recordings/checkout.mp4` |
| Transcript | `recordings/checkout.vtt` |
| Test repo | `~/work/checkout-e2e` — write here; the PR lands here |
| App repo | `~/work/checkout-ui` — **read only**, never modify |
| Dev URL | `https://checkout.dev.example.com` |

Ask for the `.vtt` rather than the `.docx` if you have the choice. A `.vtt` brackets every line with
a start and an end time; a Teams `.docx` gives one offset per speaker turn, so narration can only be
matched to frames by content.

## Where to put the tests

Follow the test repo's existing conventions. It is empty, so use the default layout in SKILL.md
step 1b, and tell me the directory before writing anything.

<!-- Or, if you want a specific location:
     Put the tests in `tests/e2e/`, page objects in `tests/e2e/pages/`. -->

## Environment

- **May the tests create and delete data in the target environment?** `yes` / `no`
  <!-- yes -> tests seed their own fixtures. no -> they can only assert on what is already there.
       This shapes the entire suite, so answer it. -->
- **Naming convention for anything created:** prefix every record with `E2E-` so leftovers are
  identifiable and can be cleaned up by hand if a run dies mid-test.
- **Is anyone available to answer questions?** `no — running unattended`
  <!-- If no: deliver everything that is not blocked, and list the rest as open questions.
       Do NOT stall waiting, and do NOT guess an assertion to fill a gap. -->
- `python` here has `imageio-ffmpeg` installed, so the media scripts will find an ffmpeg binary.

## Deliverables

1. The test suite, in the directory named above
2. A passing test run — real passes, not skips
3. The walkthrough video
4. The traceability matrix: every narrated claim marked automated / partial / blocked / not built
5. The open-questions list

## Before you report success

State explicitly:

- the project map's routes and whether each was **reachable**
- the test run's pass / fail / **skip** counts

A sandbox that blocks the browser produces all-skipped tests, a map with every route unreachable,
and exit code 0. That is not a pass, and these two numbers are how anyone reading the log afterwards
can tell the difference.

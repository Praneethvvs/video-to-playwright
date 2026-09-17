# The clarification loop

Most of what a recording leaves out cannot be inferred — it can only be asked. The difference between
a suite people trust and one they quietly stop running is usually whether these questions got asked
or quietly guessed.

## The principle: ask late, with evidence

A question asked *before* watching the footage is generic and burns goodwill. The same question asked
*after*, with a frame reference and your best guess attached, takes ten seconds to answer.

So don't front-load an interview. Gather the few things that block starting, then ask at the
checkpoints below, batching everything you've accumulated since the last one.

## What to never ask

Anything you can determine yourself. Every question spends someone's attention, so spend it on things
only they know:

- Resolution, duration, whether there's an audio track → `scripts/probe_media.py`
- Whether the app needs a login → navigate to it and see
- What the routes are, what components exist → read the source
- Whether a locator resolves → execute it
- What a control is called → read the aria snapshot

Ask about **intent, expectations, provenance, and priorities**. Those live only in a human's head.

---

## Checkpoint 0 — before you start

Only two things genuinely block progress:

1. **The video path.**
2. **The transcript.** A narrated recording is the standard this workflow assumes — narration is the
   only source of expected results. If the probe says the track is silent, ask for a re-record with
   narration before doing anything else.

One more worth asking up front, because it changes whether the suite can be written in its useful
form at all:

3. **Is the target environment shared, and may tests create and delete data there?** You cannot
   determine this by looking, and the answer decides whether tests can seed their own fixtures or
   must read whatever happens to be present.

**Don't ask for the app URL up front.** The address bar is usually visible in the footage — read it
from a frame and confirm your reading at Checkpoint 1. Same for language and runner: read the target
repo, and default to whatever it already uses, because whoever maintains that code will maintain
these tests. Asking for either is a round trip you can spend on something only a person knows.

If a `.docx` transcript was supplied, ask for the `.vtt` as well. Teams exports both; the `.docx`
usually collapses to a single timestamp for the whole session while the `.vtt` has per-cue times.
Without those, narration can only be aligned to frames by content, which is slow and approximate.
This is the highest value-per-word question in the whole process.

---

## Checkpoint 1 — after drafting the spec

The big one. You've watched the footage and know precisely what's ambiguous.

For each place the recording shows an outcome without explaining it, ask three questions:

- **What exactly were you checking here?** ("a banner appeared" / "it said this precise text" / "the
  row reached this status" are three different tests with three different values)
- **Where did this data come from?** Which entity, in what state, created by whom — the recording
  shows the result of setup it never captured
- **What would you have called a failure?**

Also raise:
- **Suspicious transcription.** Domain terms get mangled. Quote the phrase and give your reading:
  *"the transcript says 'gross margin cost' — I read that as 'excess handling cost', confirm?"*
- **Any file the flow consumed.** A recording shows the file picker's result, never the file. Uploads
  can't be automated without the fixture.
- **Anything the recording started mid-flow.** List the prerequisite steps you inferred and ask
  whether they're complete.

Present these as a numbered list with your best guess against each. Answering "yes, yes, no — it's
excess handling cost" is a ten-second reply. An open-ended "can you clarify the flow?" is a meeting.

---

## Checkpoint 2 — when the app disagrees with the recording

This will happen, and it matters more than it looks. Apps drift; a recording is a snapshot. In the
reference implementation the app had already diverged from a three-day-old video.

When a locator can't be found or a described control doesn't exist, resist two tempting mistakes:

- **Don't rewrite the assertion to match current behaviour.** The recording is evidence of what the
  behaviour was supposed to be. Silently conforming to today's app throws that away and may enshrine
  a regression as expected.
- **Don't guess the mechanism.** If a rule is clearly real but the cause isn't clear, say exactly
  that, present the evidence, and ask.

Report it as: what the recording claimed, what you observe now, what you ruled out, and the specific
question that would resolve it. Then continue with everything else rather than blocking — deliver the
rest of the suite and flag the blocked items.

---

## Checkpoint 3 — handoff

Ask for sign-off on three things:

1. **Does the walkthrough video match what they demonstrated?**
2. **Are the assertions you discovered but they never mentioned correct behaviour?** These come out of
   verification — auto-filled fields, absent toasts, disabled buttons. If one is wrong, your test is
   wrong, and only they can tell you.
3. **Is the not-yet-automated list the right priority order?**

---

## How to ask well

**Batch.** One numbered list per checkpoint. Drip-feeding questions across an afternoon is the fastest
way to lose a reviewer's engagement.

**Lead with your guess.** "I think X because Y — confirm?" is far cheaper to answer than "what should
X be?", and it shows the work.

**Cite the evidence.** A frame timestamp, a transcript quote, a measured match count. It lets them
correct you precisely instead of re-explaining from scratch.

**Say what's blocked and what isn't.** People answer faster when they know the cost. "This blocks two
of the six tests; the other four are done" gets a reply. "Please clarify" doesn't.

**Never let an unanswered question become an invented assertion.** If nobody answers, the test doesn't
get written and the gap goes in the traceability matrix. An honest hole is worth more than fake
coverage — it's visible, and someone can fill it later.

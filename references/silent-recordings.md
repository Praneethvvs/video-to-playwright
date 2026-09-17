# Silent recordings — the degraded path

Read this only when the probe reports a silent track **and** a narrated re-record genuinely isn't
available. A narrated recording is the standard the main workflow assumes, because narration is the
only source of expected results. Ask for one first; it is usually minutes of someone's time.

If you are here, the job is still doable. It costs roughly twice as much and produces a weaker spec,
and the main risk is subtle: silent footage invites you to write down everything you can see, which
quietly converts observations into requirements nobody asked for.

## Three kinds of statement, kept apart

This is the distinction that stops a silent spec becoming fake coverage. Label every line in the
spec with which kind it is.

| Kind | Where it comes from | May become a test? |
|---|---|---|
| **Observed fact** | Two frames you can point at. "The row count read 3, then 4." | Not on its own |
| **Candidate requirement** | Your reading of why that mattered. "Creating an indicator adds a row." | Only after a human confirms |
| **Confirmed requirement** | A person said yes | Yes |

With narration, the middle tier largely collapses — the narrator tells you which observations were
the point. Without it, everything lands in the middle tier and has to be promoted by a human.

So the spec you produce here is a **question list with evidence attached**, not a test plan. That
framing is the deliverable, and saying so plainly is what keeps it honest.

## What not to do

**Don't promote an observation because it looks stable.** Three rows, one selected option, a
particular total — all true when you looked, none of them requirements. This is the same trap the
main workflow warns about, and silent footage makes it far more tempting because observation is all
you have.

**Don't infer intent from repetition.** A tester clicking through a screen twice may be checking
something or may have misclicked. The footage cannot tell you which.

**Don't treat incidental OS interface as app behaviour.** Screen recordings pick up file pickers,
clipboard panels, notification toasts from other applications, stray key presses. Classify these as
incidental and keep them out of the spec entirely.

## Counting for the traceability matrix

With narration you count claims the narrator made. Without it, you need a rule, and the rule matters
less than stating which rule you used — otherwise the number is not comparable to anything.

A workable convention: **one candidate per distinct observable state change you can evidence with a
before-and-after frame pair.** Six values appearing on one screen after one click is one change, not
six, unless you have reason to think the values are independently meaningful.

Then report the count with its provenance split out:

```
34 candidate requirements, all evidence-derived
 0 human-confirmed
 0 verified against the application
```

That reads as honest work in progress. "34 assertions" reads as coverage, and is the thing to avoid.

## Recovering some of what narration would have given

Cheapest first:

1. **Ask for a single typed line per flow.** *"What I'm checking here is that the total matches the
   line items."* Ten seconds of the tester's effort, and it recovers most of the expectation.
2. **Ask for the URL bar to be visible** in any future recording — that alone gives real entity IDs
   and route structure.
3. **Walk the draft spec with the tester against the frames**, side by side. People correct far more
   reliably than they recall, so showing them a wrong draft beats asking them to describe the flow
   from memory.

Any of those moves work out of this reference and back into the main path.

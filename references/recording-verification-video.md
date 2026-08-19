# Recording the verification video

The point of this artifact is narrow and important: let the person who made the original recording
confirm, without reading code, that the tests do what they demonstrated. Everything below serves that.

## One video, not one per test

Playwright records one video per browser context, so a normal suite run produces a scattered clip per
test — wrong order, repeated page loads, no narrative. A reviewer can't compare that to their original
recording.

Instead write a **single walkthrough** that performs the whole verified flow in the same order as the
original, and record that. It's a separate artifact from the CI suite and should live outside the
test paths so a normal run never collects it and CI never pays for it.

It is explicitly allowed to do things a real test shouldn't:

- **Deliberate pauses** so a human can read the screen. In a gate test these are an anti-pattern; here
  they're the entire point.
- **Scrolling elements into view** before asserting, so the video actually *shows* what is being
  checked. A test that passes on an off-screen element is correct but proves nothing to a viewer.
- **Printing timestamped chapter markers** as it runs, which become the chapter list in the handoff
  doc — real timings, not guesses.

Keep it honest, though: it must use the same page objects and the same assertions as the real suite.
If the walkthrough passes while the suite fails, the video is worthless.

## Pace it for a human, not a machine

Start around 1000 ms per action with ~2500 ms holds at each narrative beat, and expect to be told
it's still too fast. Reviewers read the screen, look away, look back. A 90-second video that can be
followed beats a 20-second one that can't.

## The resolution trap

**Don't trust the default recording size — measure the output.** Recorded video is not necessarily
captured at your viewport size. Measured case: a 1600×1000 viewport produced an **800×500** file, which
is the documented "scale to fit 800×800" behaviour, and it rendered a dense data grid unreadable — the
same legibility failure that makes compressed Teams recordings hard to work from. Another run on a later
version reported no such downscaling, so this varies by version and configuration.

Either way the remedy is the same and costs nothing: pin the size, then check the file.

Pin the recording size to the viewport:

```python
{"record_video_size": {"width": 1600, "height": 1000}}
```

Scope that to the walkthrough only, so CI failure artifacts stay small.

Then **verify it worked** — both the dimensions and the legibility, because one can be right while the
other isn't:

```bash
ffmpeg -i walkthrough.mp4                                  # confirm WxH matches the viewport
ffmpeg -ss 44 -i walkthrough.mp4 -frames:v 1 frame.png     # then look at a still
```

If you can't read the grid text in that still, neither can the reviewer, and the video has failed at its
only job.

A well-chosen frame often proves several assertions at once, which makes it a good thing to include in
the handoff.

## Convert for the reviewer

Playwright writes `.webm`. Convert to `.mp4` — it opens on any machine without fuss:

```bash
ffmpeg -y -i video.webm -c:v libx264 -pix_fmt yuv420p -crf 22 -movflags +faststart walkthrough.mp4
```

`+faststart` lets it stream rather than requiring a full download first.

## Ship it with a traceability matrix

The video shows what the tests *do*. It can't show what they *don't*. Pair it with a document that
lists every claim from the transcript against its status — automated, partial, blocked, not built —
with an explicit count, plus:

- **Chapter timestamps**, so a reviewer can jump to the moment they care about
- **Assertions the tests make that the narration never mentioned** — discoveries from verification.
  If one is wrong behaviour, the test is wrong, and only the reviewer can say
- **Any deliberate difference from the original** (a unique test-data name instead of the one in the
  recording, for instance) flagged up front, so nobody wastes time on a mismatch that was intentional
- **A sign-off checklist**

"8 of 21 narrated claims are automated" is a useful handoff. "Tests written ✅" is not.

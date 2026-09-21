# testboard

A control panel for one Playwright test repository. It shows what tests exist, when each last
passed, how old the project map is and whether anything has drifted — and it runs any single test
on demand, streaming the output to the browser.

The last part is why it is an application rather than a generated page. A page can show state; it
cannot run anything.

## Installing it into a test repo

```bash
python scripts/install_testboard.py --repo path/to/test-repo
cd path/to/test-repo
.testboard/venv/Scripts/python.exe -m testboard      # or .testboard/venv/bin/python on Linux
```

Then open <http://127.0.0.1:8770>.

The repository commits two small files:

| | |
|---|---|
| `testboard.yaml` | every fact specific to that repo — interpreter, test paths, marker names, which environment variable holds the base URL |
| `testboard.lock` | which version is installed |

**The application's code is not committed.** It is installed into a gitignored `.testboard/venv`.
That is deliberate: six repos each carrying a copy of an application is six copies that diverge,
and this project already has the evidence — the vendored `scripts/*.py` in these repos are
byte-identical only for as long as somebody keeps checking. Upgrading is re-running the installer,
not reviewing a diff.

It runs in **its own virtualenv** and invokes the repository's interpreter as a subprocess. Sharing
one environment would mean adding a web framework to a test repo's dependency set and then keeping
the two sets compatible forever.

## What it will not do

**It will not pretend to know something it does not.** A drift check that could not reach the
application reports zero breaking changes, and so does a clean one. Only one of those is good news,
so every tile has an explicit *unknown* state and the drift page says, in words, "this is not a
clean result — it is an absence of one". The same rule covers a test that has never been run here,
a pruned artifact and a map with no timestamp.

**It will not run two things at once.** The suite mutates shared data in an environment other people
are using, so there is one queue and one worker. Drift checks and map captures go through the same
queue, because they drive the same browser against the same host.

**It will not write to your repository.** Recapturing the map produces a file in the run directory
for a human to review and commit. A service that edits tracked files, watched by CI that rebuilds
on tracked files, is a loop.

**It will not overwrite `project-map.json` or anything under `e2e/`.** The installer writes
`testboard.yaml` only if it is absent, and never touches it again.

## Running a destructive test

Tests carrying the repo's destructive marker (`writes`, here) change data in a shared environment.
The button says so before you press it, the confirmation names the environment, and while one is in
flight a banner appears on every page with who started it. The server refuses the request outright
unless it is told the person was asked, so a direct POST that skips the dialog is still rejected.

## Keeping it alive

The things that kill an unattended service are handled rather than left to be noticed:

| Failure | What happens |
|---|---|
| Traces fill the disk | Hourly sweep: last N runs per test, a global cap, and the newest run of every test always kept. Below 500 MB free it refuses to start runs rather than writing a truncated trace |
| The service dies mid-run | On restart each `running` row is reconciled. The recorded PID is only acted on if the process creation time also matches, so a recycled PID is never killed by mistake |
| Orphaned browsers | Killing pytest alone orphans Chromium, so the whole process group goes — `killpg` on Linux, `taskkill /T /F` on Windows — plus a periodic sweep for anything that outlived its parent |
| Ghost SSE clients | Every stream heartbeats every 15 seconds. Without it, subscribers a proxy quietly dropped accumulate forever |
| Its own log growing | Rotated. Moving the disk-fill bug into the application's log would not be a fix |
| A slow browser tab | A subscriber whose queue fills is dropped and told to resync. The run is never held up by someone's wifi |

## Deploying it

`Dockerfile` and `k8s/testboard.yaml` are written and **have never been run**. Read the `DECIDE`
comments first. No Ingress is included on purpose: anything that can reach this can mutate the
shared environment, and authentication belongs at the ingress, not in here.

Until that is settled: `kubectl port-forward svc/testboard 8770:80`.

## Limits worth knowing

- **Python and pytest only.** The runner and the inventory collector are the only language-specific
  parts and sit behind a small interface, so a TypeScript adapter is additive — but it does not
  exist, and the configuration loader refuses a non-Python repo rather than half-working.
- **The link between a test and the map is inferred**, by case-insensitive substring search over the
  test tree. It is shown as inference, and it misses a locator assembled at runtime.
- **No cross-repo view.** If an aggregate over several repos is ever wanted it belongs in one
  central place, not in six copies of this.
- **No authentication.** It binds to `127.0.0.1` and warns if you tell it not to.

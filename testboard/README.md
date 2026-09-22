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

## Recordings, and generating tests from them

Drop a screen recording and its transcript onto the Recordings page, or point testboard at files
already sitting in the repo and adopt them. The transcript is parsed on upload — `.vtt`, `.srt`,
`.docx` and `.txt`, with no third-party dependency — and the result says plainly whether it has
per-cue timestamps, because a Teams `.docx` usually does not and that changes how well narration
can be aligned to the video.

**Where each piece is kept, and why they differ.** The transcript text goes into SQLite: it is
small, it is what a generation run actually reads, and having it there means a run needs nothing
from the filesystem. The video goes on disk with its digest recorded. Putting a 200 MB file in
SQLite would bloat every backup and VACUUM of a database whose whole value is being small enough
to keep forever, and nothing ever queries its bytes.

Press **Generate tests** and an agent reads the transcript and writes tests, through the same
queue, with the same live log and the same Cancel button as everything else.

### Why the SDK and not `codex exec`

Shelling out to the CLI works on a laptop and is the wrong shape for a cluster, for four reasons
this project hit directly:

| | |
|---|---|
| **Quoting** | A multi-line prompt as a shell argument gets split on whitespace: `error: unexpected argument 'a' found`. The documented workaround is writing the task to a Markdown file — a workaround for a problem that does not exist when the prompt is an object |
| **Authentication** | The CLI wants an interactive login or a seeded `~/.codex/auth.json`. `login_api_key()` reads a key from a Secret at startup and writes nothing |
| **Sandboxing** | `-s workspace-write` blocks the browser on Windows with `WinError 5`, and a blocked run gives all-skipped tests and **exit code 0** — failure that looks like success. Here it is a typed argument |
| **Cancel and streaming** | A subprocess gives a pipe to scrape. `turn.stream()` gives typed events and `turn.interrupt()` gives a cancel the queue calls directly |

`openai-codex` bundles its own pinned CLI binary, so there is nothing to install separately and
nothing to keep in version step.

**The skill travels with the request**, as a `SkillInput`. That was the open question — how a
remote agent gets the video-to-playwright workflow without it being pre-installed in a home
directory somewhere — and it has a first-class answer. In the image it lives at
`TESTBOARD_SKILL_PATH`.

Nothing else changes between a laptop and a pod:

| | laptop | pod |
|---|---|---|
| credential | your existing `codex login` | `CODEX_API_KEY` from a Secret |
| workspace | the checked-out repo | the repo the init container cloned |
| the skill | found at `~/.claude/skills/` | baked into the image |
| corporate TLS | the OS trust store, exported automatically | `SSL_CERT_FILE`, mounted |

That last row is not incidental. The Codex runtime is Rust and verifies TLS against its own
compiled-in roots, so behind an intercepting proxy every model call fails with
`invalid peer certificate: UnknownIssuer`, which reads like a network fault and is not one. On
Windows testboard exports the system trust store to a bundle and points the agent at it; in a
container there is no OS store to export, so the bundle is mounted.

## Nothing enters the suite without a person saying so

A test is in the suite because somebody looked at it and accepted it, not because it exists. Both
ways a test can arrive are treated identically:

| | |
|---|---|
| An agent wrote it here | it appears as **pending** after the conversion run |
| Somebody merged a pull request | it appears as **pending** at the next collection |

Pending tests are excluded from "Run all approved" — testboard passes `--deselect` for each — and
approving one records who did it and when. You can still run a pending test deliberately; that is
how you check it before accepting it.

This is keyed on the nodeid rather than a marker or a directory, which is what makes it work for a
test that arrived through a merge, a thing testboard had no part in creating.

The first collection in a new installation is different, and deliberately so: everything already
in the repository is the suite as it stands, and is approved outright. Anything appearing after
that is something nobody has looked at yet.

## Where the agent's ambiguity goes

A recording is often ambiguous — it shows a click but not why, or an outcome the narrator never
named. The agent says so in its closing report, which the recording's page shows.

**testboard does not try to hold that conversation.** It has no way to verify an answer against the
application, and a half-remembered reply typed into a box becomes an assertion nobody can trace
back to anything. The page says to continue in your own Codex session, open a pull request, and
let the merged tests arrive here as pending. That keeps every answer attached to a commit and a
review, which is where an answer of that kind belongs.

## Who can use it

There is no login, and there is not going to be one. What there is instead is a policy decided
by where it listens, so it cannot be left in an unsafe state by forgetting a setting:

| | |
|---|---|
| loopback, no token | everyone is `local`. Only somebody already on the machine can reach it |
| any address, token set | every request must present it |
| token **and** an authenticating proxy | the token authorises the proxy; the proxy's `X-Forwarded-User` becomes the recorded name |

**Listening on a non-loopback address without a token is refused at startup.** Not warned about —
refused, with the command to generate one. A warning printed into a log nobody reads is how an
unauthenticated endpoint that can mutate a shared environment ends up on a cluster network.

```bash
TESTBOARD_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
python -m testboard --host 0.0.0.0
```

A browser can be given the token once as `?token=…` and it is remembered in a `SameSite=Strict`
cookie for twelve hours. `/healthz` and `/static/` stay open so a Kubernetes probe works without
the secret and the stylesheet loads on the page asking for it.

The token authenticates; it does not identify. Every holder is recorded identically as
`token@<address>`, which is honest and not much use in an audit trail — so if "who approved this
test" needs to mean something, front it with a proxy that sets a user header.

## Local development, cluster execution

Writing tests wants a fast local loop: edit, click re-run, read the output in seconds. Running them
against an environment only reachable from inside the cluster wants a pod. Both are the same
application and the same database file.

| | local | in the cluster |
|---|---|---|
| purpose | writing and reviewing tests | running them against dev |
| state | `.testboard/` in your checkout | the same path, on a PersistentVolumeClaim |
| credential | your `codex login` | `CODEX_API_KEY` from a Secret |

### Runs that happened elsewhere

A pipeline posts its JUnit report when it finishes, so the dashboard reflects what ran in the
cluster and not only what somebody started in its own interface:

```bash
curl -H "Authorization: Bearer $TESTBOARD_TOKEN" \
     -F "file=@test-results.xml" \
     -F "label=$(Build.DefinitionName) #$(Build.BuildId)" \
     -F "git_sha=$(Build.SourceVersion)" \
     "$TESTBOARD_URL/api/runs/junit"
```

`.pipelines/ci.yaml` does this already, guarded so that a dashboard being down never fails a
green suite.

Imported runs are recorded as `kind='ci'` and stay visibly distinct: no log to stream, no
artifacts to prune, no process that was ever ours, and the run page says so rather than showing
an empty log panel. Two things they deliberately do **not** do:

- **They never approve a test.** A pipeline is not a person, and the approval record exists to
  say which person decided.
- **They never add to the inventory.** A nodeid in the report that this checkout has never
  collected is listed as unmatched and otherwise ignored — the pipeline may be on a branch whose
  tests do not exist here, and inventing rows from a remote report would put tests on the
  dashboard that nobody can find or run.

### Waiting for a run you started

`GET /api/runs/<id>` returns the run as JSON, including a `finished` flag:

```bash
until curl -sf -H "Authorization: Bearer $TESTBOARD_TOKEN" \
        "$TESTBOARD_URL/api/runs/$RUN_ID" | jq -e '.finished' >/dev/null; do sleep 10; done
```

`finished` is the server's answer rather than the caller's, on purpose. A poller that hardcodes
`passed|failed` waits forever on a run that crashed or timed out — which is exactly the situation
somebody is polling to find out about.

### What a conversion changed

Approval covers a test *arriving*. It says nothing about one being rewritten or deleted, and an
agent asked to write tests from a recording will sometimes decide an existing assertion is not
supported by the narration and remove it. On the first real conversion here that took out a whole
test and three files' worth of assertions, and every screen showed a clean green run.

So a generation run records what it changed in the working tree and names every test that stopped
existing, on the run page and in the log. It does not block the deletion: sometimes the agent is
right, and a tool that forbids it just gets worked around. The failure being fixed is that nobody
could see it.

Reading the tree is best effort, and "could not tell" is reported as itself — never as "nothing
changed". Edits already present when Convert was pressed are subtracted, so somebody mid-change
is not blamed on the agent.

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

## Its own tests

```bash
pip install -e ".[dev]" && pytest
```

113 tests, about three seconds, no browser and no network. They cover the properties that are expensive
to be wrong about rather than aiming at coverage:

| | |
|---|---|
| `test_approval.py` | a test only enters the suite because a person said so — including the empty-repo-then-generate sequence that used to bypass it entirely |
| `test_auth.py` | a public bind without a token is refused; a prefix of the token does not pass; an implausible proxy header is never recorded as a name |
| `test_transcripts.py` | every real file shape that has broken something: mm:ss cues, Word revision ids, `<w:br/>` between runs, a `.vtt` that parses to nothing |
| `test_ingest.py` | an import never approves a test, never adds an inventory row, and never attributes a result to an ambiguous name |
| `test_app_security.py` | the middleware stack as wired: every mutating route needs the token, a cross-site POST is refused, and the Referer cannot steer a redirect off-site |
| `test_agent_changes.py` | a clean working tree reads as clean and an unreadable one reads as unknown; a deleted test is named; pre-existing edits are not blamed on the agent |

They found a live defect on their first run: `hmac.compare_digest` accepts ASCII only when given
`str`, so a token containing any non-ASCII character raised `TypeError` inside the auth middleware
and returned 500 where it should have returned 401.

## Limits worth knowing

- **Python and pytest only.** The runner and the inventory collector are the only language-specific
  parts and sit behind a small interface, so a TypeScript adapter is additive — but it does not
  exist, and the configuration loader refuses a non-Python repo rather than half-working.
- **The link between a test and the map is inferred**, by case-insensitive substring search over the
  test tree. It is shown as inference, and it misses a locator assembled at runtime.
- **No cross-repo view.** If an aggregate over several repos is ever wanted it belongs in one
  central place, not in six copies of this.
- **No login of its own.** There are two modes and no third: loopback with nobody in front, or a
  shared token. A per-user identity only exists when an authenticating proxy supplies one, and that
  header is read in token mode only. Anyone holding the token can do anything this can do.

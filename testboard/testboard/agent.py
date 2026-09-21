"""Turning a recording into tests, by driving Codex in-process.

## Why the SDK rather than shelling out to the CLI

The obvious implementation is `subprocess("codex exec ...")`. It works on a laptop and it is the
wrong shape for a cluster, for four concrete reasons this project has already hit:

* **Quoting.** A multi-line prompt passed as a shell argument gets split on whitespace and Codex
  exits with `error: unexpected argument 'a' found`. The documented workaround is to write the
  task into a Markdown file and pass a one-line prompt — a workaround for a problem that simply
  does not exist when the prompt is a Python object.
* **Authentication.** The CLI expects either an interactive login or a seeded `~/.codex/auth.json`.
  In a pod that means baking a credential file into an image or mounting one, both worse than
  `login_api_key()` reading a key straight from a Kubernetes Secret at startup.
* **Sandboxing.** `-s workspace-write` blocks the browser on Windows with `WinError 5`, and a
  blocked run produces all-skipped tests and **exit code 0** — a failure that looks exactly like
  success. The sandbox is a typed argument here, set once, in code.
* **Cancellation and streaming.** A subprocess gives a pipe to scrape. `turn.stream()` gives typed
  events, and `turn.interrupt()` gives a cancel that the existing queue can call directly.

`openai-codex` bundles its own pinned CLI binary as a dependency (`openai-codex-cli-bin`), so there
is nothing to install separately and nothing to keep in version step.

## Running it in the cluster

Nothing changes. The same code path runs on a laptop and in a pod; the differences are all
configuration:

| | laptop | pod |
|---|---|---|
| credential | `CODEX_API_KEY` in the shell | the same variable, from a Secret |
| workspace | the checked-out repo | the repo cloned by the init container |
| the skill | a path on disk | the same, baked into the image or mounted |

The skill travels as a `SkillInput`, which is how the agent gets the video-to-playwright workflow
without anything being pre-installed in its home directory. That was the open question, and it has
a first-class answer.

This module is optional. testboard runs fine without `openai-codex` installed; generation is then
reported as unavailable, with the reason, rather than failing at the moment somebody presses the
button.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("testboard.agent")

API_KEY_VARS = ("CODEX_API_KEY", "OPENAI_API_KEY")


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""
    detail: str = ""


def auth_file() -> Path:
    """Where the Codex CLI keeps a ChatGPT login."""
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "auth.json"


def _api_key() -> str | None:
    for var in API_KEY_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def availability() -> Availability:
    """Can a generation run start at all? Answered before the button is offered, not after.

    Two credentials are accepted, because the two places this runs have different ones. On a
    laptop somebody has already run `codex login`, and the token sitting in ~/.codex/auth.json is
    the one they want used — asking them for an API key they do not have would be absurd. In a
    pod there is no interactive login and no home directory worth persisting, so a key arrives
    from a Secret. Either is fine; neither being present is worth saying plainly up front rather
    than at the moment somebody presses the button.
    """
    try:
        import openai_codex  # noqa: F401
    except ImportError:
        return Availability(
            False, "sdk-missing",
            "the Codex SDK is not installed. Add it with:  pip install openai-codex",
        )
    if _api_key():
        return Availability(True, "api-key", f"using {_which_key()} from the environment")
    if auth_file().exists():
        return Availability(True, "cli-login", f"using the Codex login in {auth_file()}")
    return Availability(
        False, "no-credential",
        f"no credential. Either run `codex login`, or set one of {', '.join(API_KEY_VARS)} — "
        f"in the cluster, from a Secret.",
    )


def _which_key() -> str:
    for var in API_KEY_VARS:
        if os.environ.get(var):
            return var
    return "an API key"


def ca_bundle(state_dir: Path) -> Path | None:
    """Export the operating system's trust store to a PEM the Codex binary can read.

    The Codex runtime is a Rust program and verifies TLS against its own compiled-in roots. Behind
    corporate interception every response is re-signed by a proxy whose root lives in the Windows
    certificate store and nowhere the binary looks, so the connection fails with

        stream disconnected before completion: invalid peer certificate: UnknownIssuer

    which reads like a network fault and is nothing of the kind. `SSL_CERT_FILE` is the standard
    escape hatch, so the store is dumped once to a file under the state directory and pointed at.

    A no-op where it should be: if `SSL_CERT_FILE` is already set, that wins, and on Linux — where
    the system bundle is already correct and `enum_certificates` does not exist — this returns
    None and nothing is overridden.
    """
    import ssl

    if not hasattr(ssl, "enum_certificates"):
        # Linux and macOS: the system bundle is already what a Rust binary reads, and an existing
        # SSL_CERT_FILE is somebody's deliberate choice. Nothing to do.
        return Path(os.environ["SSL_CERT_FILE"]) if os.environ.get("SSL_CERT_FILE") else None

    bundle = state_dir / "ca-bundle.pem"
    try:
        pems: list[str] = []

        # An existing SSL_CERT_FILE is merged in rather than deferred to. On this machine it
        # holds the interception root and nothing else, and handing a Rust binary a one-root
        # bundle replaces the public CAs instead of adding to them — which fixes the proxy and
        # breaks everything the proxy is not in front of.
        existing = os.environ.get("SSL_CERT_FILE")
        if existing and Path(existing).is_file() and Path(existing) != bundle:
            try:
                pems.append(Path(existing).read_text(encoding="ascii", errors="ignore"))
            except OSError:
                pass

        for store in ("ROOT", "CA"):
            for der, _encoding, trust in ssl.enum_certificates(store):
                # `True` means "trusted for everything"; a set means trusted for specific purposes,
                # and server authentication is the one that matters here.
                if trust is True or (isinstance(trust, (set, frozenset))
                                     and "1.3.6.1.5.5.7.3.1" in trust):
                    pems.append(ssl.DER_cert_to_PEM_cert(der))
        if not pems:
            return None
        text = "".join(pems)
        if not bundle.exists() or bundle.read_text(encoding="ascii") != text:
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_text(text, encoding="ascii")
        return bundle
    except (OSError, ssl.SSLError, ValueError) as exc:
        log.warning("could not export the system trust store: %s", exc)
        return None


PROMPT = """\
Read the skill and follow it to turn this recording into Playwright tests for this repository.

Recording:  {video}
Transcript: {transcript}

The transcript text is below, between the markers.

Rules that override anything you infer:

- Never commit a locator or an assertion you have not confirmed against the running application at
  {base_url}. A test that was never executed reads as coverage and fails silently later.
- Never assert something that merely happened to be true on the day. If an assertion does not trace
  back to something the narrator said mattered, do not write it. Exact record names, row counts and
  "one option is selected" have all broken here with no defect present.
- Match this repository's existing conventions: read {test_dir} first and follow what is there.
- Where the recording is ambiguous, leave the hole open and say so in your final message. Do not
  invent an expected value.

Finish by listing, in your final message: every file you created or changed, every narrated claim
you automated, and every claim you could not and why.

--- TRANSCRIPT ---
{transcript_text}
--- END TRANSCRIPT ---
"""


@dataclass
class GenerationResult:
    ok: bool
    status: str
    final_response: str
    error: str = ""
    duration_ms: int | None = None
    tokens: int | None = None


class CodexAgent:
    """One generation run. Owns a Codex thread and can be interrupted mid-flight."""

    def __init__(self, *, repo_root: Path, skill_path: Path | None, model: str | None,
                 sandbox: str, env: dict[str, str], state_dir: Path | None = None):
        self.repo_root = repo_root
        self.skill_path = skill_path
        self.model = model
        self.sandbox = sandbox
        self.env = dict(env)
        self._turn = None
        self._codex = None

        if state_dir is not None:
            bundle = ca_bundle(state_dir)
            if bundle:
                # Overridden, not defaulted: whatever was inherited is already merged into this
                # file, and the merged bundle is strictly the better of the two.
                self.env["SSL_CERT_FILE"] = str(bundle)
                self.env["NODE_EXTRA_CA_CERTS"] = str(bundle)   # node tooling reads its own
                self.env["REQUESTS_CA_BUNDLE"] = str(bundle)

    async def interrupt(self) -> None:
        if self._turn is not None:
            try:
                await self._turn.interrupt()
            except Exception:                       # noqa: BLE001
                log.exception("interrupting the Codex turn failed")

    async def run(self, *, prompt: str, emit: Callable[[str], None]) -> GenerationResult:
        from openai_codex import (AsyncCodex, CodexConfig, Sandbox, SkillInput, TextInput)

        config = CodexConfig(cwd=str(self.repo_root), env=self.env)
        codex = AsyncCodex(config)
        self._codex = codex

        try:
            # Only when a key is supplied. With a ChatGPT login already on the machine, calling
            # login_api_key(None) would replace a working credential with nothing.
            key = _api_key()
            if key:
                await codex.login_api_key(key)
                emit(f"authenticated with {_which_key()}")
            else:
                emit(f"using the Codex login in {auth_file()}")

            sandbox = {
                "read_only": Sandbox.read_only,
                "workspace_write": Sandbox.workspace_write,
                "full_access": Sandbox.full_access,
            }.get(self.sandbox, Sandbox.workspace_write)

            thread = await codex.thread_start(
                cwd=str(self.repo_root),
                sandbox=sandbox,
                model=self.model or None,
                # Nothing is persisted into a home directory that a pod will throw away anyway;
                # the record of what happened is this run's own log and the diff it leaves behind.
                ephemeral=True,
            )
            emit(f"thread {thread.id} started  (sandbox={sandbox}, model={self.model or 'default'})")

            items: list = []
            if self.skill_path and self.skill_path.exists():
                # The whole point: the workflow travels with the request. Nothing has to be
                # pre-installed in the agent's home directory, which is what made this hard to
                # run anywhere but a laptop.
                items.append(SkillInput(name="video-to-playwright", path=str(self.skill_path)))
                emit(f"skill attached: {self.skill_path}")
            else:
                emit("no skill attached — the agent is working from the prompt alone")
            items.append(TextInput(text=prompt))

            handle = await thread.turn(items)
            self._turn = handle

            # The outcome is taken from the stream's own completion event, not from a follow-up
            # run(). Consuming the stream closes the subscription, so calling run() afterwards
            # raises TransportClosedError — which recorded a perfectly good run, one that had
            # already written its tests, as a failure.
            completed = None
            async for note in handle.stream():
                if type(getattr(note, "payload", None)).__name__ == "TurnCompletedNotification":
                    completed = note.payload.turn
                for line in _render(note):
                    emit(line)

            if completed is None:
                # The stream ended without a completion event. Whatever happened, it is not a
                # success, and saying so beats guessing from the files left behind.
                return GenerationResult(
                    False, "incomplete", "",
                    "the agent's stream ended before the turn completed; check the log and the "
                    "working tree before trusting anything it wrote",
                )

            status = getattr(completed.status, "value", str(completed.status))
            final = _final_message(completed)
            return GenerationResult(
                ok=status.endswith("completed") and not completed.error,
                status=status,
                final_response=final,
                error=str(completed.error) if completed.error else "",
                duration_ms=completed.duration_ms,
            )
        finally:
            try:
                await codex.close()
            except Exception:                       # noqa: BLE001
                pass


def _unwrap(item):
    """ThreadItem is a RootModel around a union of ~19 item types; the real one is `.root`.

    Missing this is why the log labelled every event "ThreadItem" and why the agent's closing
    summary came back empty — the wrapper has exactly one attribute, and none of the ones worth
    reading.
    """
    return getattr(item, "root", item)


def _final_message(turn) -> str:
    """The agent's closing summary — the last agent message in the turn."""
    for wrapped in reversed(getattr(turn, "items", []) or []):
        item = _unwrap(wrapped)
        if "AgentMessage" in type(item).__name__:
            text = _text_of(item)
            if text:
                return text[:8000]
    return ""


def _text_of(item) -> str | None:
    """Pull the readable text out of a thread item, whatever shape it arrived in."""
    for attr in ("text", "content", "message", "summary", "aggregated_output", "output"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            parts = [getattr(v, "text", None) or (v if isinstance(v, str) else None)
                     for v in value]
            joined = " ".join(p for p in parts if p)
            if joined.strip():
                return joined
    return None


def _render(note) -> list[str]:
    """Turn a Codex notification into log lines.

    Permissive on purpose. The event union is large, it will grow, and the shapes are not worth
    pinning down precisely — so anything not explicitly recognised is still announced by its type
    rather than dropped. A silent log during a twenty-minute agent run is indistinguishable from a
    hung one, and that ambiguity costs far more than an occasional uninformative line.
    """
    # Every event arrives as Notification(method=..., payload=<the specific model>). Switching on
    # type(note) therefore matches nothing — it is always "Notification" — which is precisely how
    # the first version of this produced a log full of blank bullets.
    payload = getattr(note, "payload", None) or note
    name = type(payload).__name__.replace("Notification", "")

    def get(*keys):
        for key in keys:
            value = getattr(payload, key, None)
            if value:
                return value
        return None

    # Deltas arrive per token. The completed item carries the same text once.
    if name.endswith("Delta") and not name.startswith("CommandExecutionOutput"):
        return []

    if name == "CommandExecutionOutputDelta":
        chunk = get("delta", "chunk", "text", "output")
        if not chunk:
            return []
        return [ln for ln in str(chunk).rstrip().splitlines() if ln.strip()][-20:]

    if name in ("ItemStarted", "ItemCompleted"):
        item = _unwrap(get("item") or payload)
        kind = type(item).__name__.replace("ThreadItem", "")

        command = getattr(item, "command", None)
        if command:
            return [f"$ {command}"] if name == "ItemStarted" else []
        changes = getattr(item, "changes", None) or getattr(item, "path", None)
        if changes:
            return [f"edit {changes}"] if name == "ItemCompleted" else []
        if kind == "Reasoning":
            text = _text_of(item)
            return [f"  {line}" for line in str(text or "")[:600].splitlines() if line.strip()] \
                if name == "ItemCompleted" else []
        text = _text_of(item)
        if text and name == "ItemCompleted":
            return [line for line in str(text)[:4000].splitlines() if line.strip()]
        return [f"→ {kind}"] if name == "ItemStarted" and kind else []

    if name == "TurnDiffUpdated":
        return ["(the working tree changed)"]
    if name == "TurnPlanUpdated":
        plan = get("plan", "steps")
        return [f"plan: {str(plan)[:500]}"] if plan else []
    if name == "Error":
        return [f"ERROR: {get('message', 'error') or note}"]
    if name in ("TurnStarted", "ThreadStarted"):
        return []
    if name == "TurnCompleted":
        return ["turn completed"]
    if name == "ThreadTokenUsageUpdated":
        return []

    return [f"· {name}"]

"""Discover what tests exist, by asking pytest.

Never by parsing source. Collection is the only thing that agrees with what will actually run:
it resolves conftest files, parametrisation, dynamic markers and skips-at-import.

The exit code carries more information than the output does, and conflating two of its values is
the most common way a dashboard lies. `5` means nothing matched, which is usually a filter and is
benign. `2` means collection *failed* — and rendering that as an empty test list tells someone
their suite is empty when in fact it is broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import utcnow
from .procs import kill_tree, spawn

log = logging.getLogger("testboard.inventory")

PLUGIN_MODULE = "testboard_collect_plugin"
COLLECT_TIMEOUT = 180


@dataclass
class CollectResult:
    ok: bool
    items: list[dict]
    exit_code: int
    detail: str          # stderr/stdout tail, only interesting when ok is False
    reason: str          # collected | no-tests | collection-error | harness
    new_nodeids: list[str] = field(default_factory=list)

    @property
    def is_empty_but_healthy(self) -> bool:
        return self.ok and not self.items


def _install_plugin(config: Config) -> Path:
    """Place the collector plugin where the repo's interpreter can import it."""
    target_dir = config.state_dir / "plugins"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{PLUGIN_MODULE}.py"
    source = Path(__file__).with_name("collect_plugin.py")
    if not target.exists() or target.read_bytes() != source.read_bytes():
        shutil.copyfile(source, target)
    return target_dir


async def collect(config: Config, on_spawn=None) -> CollectResult:
    """Ask pytest what exists.

    `on_spawn` hands the process to the caller. Collection is the only spawn outside the
    executor's own command path, so without it the executor had no way to kill a collect that
    somebody cancelled, and no PID recorded for the reconciler to find after a restart.
    """
    python = config.repo_python()
    if not python.exists():
        return CollectResult(
            ok=False, items=[], exit_code=-1, reason="harness",
            detail=(f"the repo interpreter {python} does not exist. Check `runner.python` in "
                    f"testboard.yaml, and that the test repo's virtualenv has been created."),
        )

    plugin_dir = _install_plugin(config)
    out_file = config.state_dir / "collect.json"
    out_file.unlink(missing_ok=True)

    env = config.environment.subprocess_env()
    env["TESTBOARD_COLLECT_OUT"] = str(out_file)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{plugin_dir}{os.pathsep}{existing}" if existing else str(plugin_dir)

    argv = [
        str(python), "-m", "pytest", "--collect-only", "-q",
        "-p", PLUGIN_MODULE,
        *config.runner.test_paths,
    ]

    proc = await spawn(argv, cwd=config.repo_root, env=env)
    if on_spawn is not None:
        on_spawn(proc)
    try:
        # Bounded. Collection imports every conftest in the repository, and one that blocks on
        # the network would otherwise hang this call — and with it the worker and the whole
        # queue — with no way out.
        raw = await asyncio.wait_for(proc.stdout.read(), timeout=COLLECT_TIMEOUT)
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        return CollectResult(
            False, [], -1,
            f"collection did not finish within {COLLECT_TIMEOUT}s and was killed. A conftest that "
            f"blocks on start-up is the usual cause.", "harness",
        )
    text = raw.decode("utf-8", errors="replace")
    code = proc.returncode or 0

    if code == 5:
        return CollectResult(True, [], code, text[-4000:], "no-tests")

    if code != 0:
        # Explicitly not "zero tests". Somebody's import is broken, and saying so is the whole job.
        return CollectResult(False, [], code, text[-4000:], "collection-error")

    if not out_file.exists():
        return CollectResult(
            False, [], code, text[-4000:], "harness",
        )

    try:
        items = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CollectResult(False, [], code, f"{exc}\n{text[-2000:]}", "harness")

    return CollectResult(True, items, code, "", "collected")


async def refresh(config: Config, database, *, origin: str = "merged",
                  source_id: int | None = None, on_spawn=None) -> CollectResult:
    """Collect and persist. The previous inventory is left intact if collection failed.

    `origin` labels anything that turns out to be new. The default is "merged", because outside a
    generation run the only way a test appears is that somebody landed it — which is exactly the
    case the approval step exists for.
    """
    result = await collect(config, on_spawn=on_spawn)
    if result.ok:
        result.new_nodeids = database.replace_inventory(
            result.items, origin=origin, source_id=source_id)
    database.set_meta("collect", {
        "ok": result.ok,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "detail": result.detail,
        "count": len(result.items),
        "at": utcnow(),
    })
    if not result.ok:
        log.error("collection failed (%s, exit %s): %s",
                  result.reason, result.exit_code, result.detail[-500:])
    return result

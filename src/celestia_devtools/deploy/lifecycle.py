#!/usr/bin/env python3
"""Upgrade / rollback with a deployment ledger, and the binding rule that
an upgrade may only run after a backup (a migration is not reversible — the
backup is the rollback path, so the two are one transactional flow).

Ledger: ``<deploy_root>/.deploy-history.jsonl`` — one JSON line per action:
{"ts", "action", "version", "sha256", "backup", "result", "detail"}. It is
append-only and is the data source for the later `deploy status`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from celestia_devtools.deploy import backup as backup_mod
from celestia_devtools.deploy.artifact import Entry, fetch, read_index, resolve
from celestia_devtools.deploy.profile import DeployProfile

Executor = Callable[..., subprocess.CompletedProcess]


def _default_executor(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def history_path(profile: DeployProfile) -> Path:
    return profile.host.deploy_root() / ".deploy-history.jsonl"


def history_append(profile: DeployProfile, **fields) -> dict:
    entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    entry.update(fields)
    path = history_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def history_read(profile: DeployProfile) -> list[dict]:
    path = history_path(profile)
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def bin_path(profile: DeployProfile) -> Path:
    return profile.host.deploy_root() / "bin" / profile.host.face


def prev_path(profile: DeployProfile) -> Path:
    return bin_path(profile).with_suffix(".prev")


def _unit_env(profile: DeployProfile) -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = profile.host.etc_env()
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def _migrate(profile: DeployProfile, binary: Path, executor: Executor) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(_unit_env(profile))
    return executor([str(binary), "db-migrate"], timeout=600, env=env)


def _install(binary: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = binary.read_bytes()
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)


def _restart(profile: DeployProfile, executor: Executor) -> subprocess.CompletedProcess:
    return executor(["systemctl", "restart", profile.host.unit_name()], timeout=300)


@dataclass
class UpgradeOutcome:
    ok: bool
    version: str = ""
    detail: str = ""
    rolled_back: bool = False


def upgrade(profile: DeployProfile, source_base: str, channel: str, target: str,
            executor: Executor = _default_executor,
            dest_dir: Path | None = None) -> UpgradeOutcome:
    """Fetch newest → backup (hard prerequisite) → swap → migrate → on
    migration failure put the previous binary back and restart."""
    index = read_index("{}/index.toml".format(str(source_base).rstrip("/")))
    entry: Entry = resolve(index, channel, target)
    staged = fetch(entry, source_base,
                   dest_dir or (profile.host.deploy_root() / "incoming"))
    current = bin_path(profile)

    # The binding rule: migration is not reversible, so back up first.
    bkp = backup_mod.backup(profile, profile.host.deploy_root() / "backups",
                            executor=executor)

    if current.exists():
        _install(current, prev_path(profile))
    _install(staged, current)

    migrate = _migrate(profile, current, executor)
    if migrate.returncode != 0:
        detail = (migrate.stderr or migrate.stdout or "").strip()[:200]
        # roll the binary back; the database state is the backup's problem,
        # and the ledger says so explicitly
        if prev_path(profile).exists():
            _install(prev_path(profile), current)
        _restart(profile, executor)
        history_append(profile, action="upgrade", version=entry.version,
                       sha256=entry.sha256, backup=str(bkp.path),
                       result="failed", detail="migrate failed; binary rolled back: " + detail)
        return UpgradeOutcome(False, entry.version,
                              "migrate failed; previous binary restored", rolled_back=True)
    _restart(profile, executor)
    history_append(profile, action="upgrade", version=entry.version,
                   sha256=entry.sha256, backup=str(bkp.path), result="ok")
    return UpgradeOutcome(True, entry.version,
                          "upgraded; backup at {}".format(bkp.path.name))


def rollback(profile: DeployProfile,
             executor: Executor = _default_executor) -> UpgradeOutcome:
    previous = prev_path(profile)
    if not previous.exists():
        history_append(profile, action="rollback", result="failed",
                       detail="no previous binary kept")
        return UpgradeOutcome(False, detail="no {} to roll back to".format(previous))
    current = bin_path(profile)
    failed = current.with_suffix(".failed")
    if current.exists():
        _install(current, failed)
    _install(previous, current)
    previous.unlink()
    _restart(profile, executor)
    history_append(profile, action="rollback", result="ok",
                   detail="restored {}; failed copy at {}".format(current.name, failed.name))
    return UpgradeOutcome(True, detail="rolled back to previous binary")

#!/usr/bin/env python3
"""File-level backup and restore for one deployed face.

House rules baked in: NO snapshots (AGENTS §10.2 — stateful nodes never hold
ESXi snapshots; backups are file-level by decree), and the env file rides
along with an explicit warning — losing SHITTIM_CHEST_ENCRYPTION_KEY means
every ciphertext it protected is gone forever; that sentence must reach the
operator, not just the log.

Layout of one backup directory::

    <dest>/<face>-<utc-timestamp>/
        manifest.json     # entries + sha256 + sizes + versions
        data.tar.gz       # /srv/<face>/data (themes/avatars/uploads/…)
        env backup        # /etc/celestia/<face>.env (0600 preserved)
        db.dump           # pg_dump, only when a DB URL is resolvable
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from celestia_devtools.deploy.artifact import sha256_file
from celestia_devtools.deploy.profile import DeployProfile

ENV_KEY_WARNING = (
    "⚠ /etc/celestia/<face>.env 含 SHITTIM_CHEST_ENCRYPTION_KEY——丢了它，"
    "库里与桶里的全部密文永久解不开。备份介质请同等保护。")


@dataclass
class BackupResult:
    path: Path
    entries: dict[str, str] = field(default_factory=dict)  # file -> sha256
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "{}: {} files, {} skipped{}".format(
            self.path.name, len(self.entries), len(self.skipped),
            "" if self.skipped else " (" + ", ".join(self.skipped) + ")")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def resolve_db_url(profile: DeployProfile) -> str | None:
    """DB URL from the face's url file; SHITTIM_CHEST_DATABASE_URL style."""
    env = profile.host.etc_env()
    default_url_file = env.with_name(env.stem + "-db.url")
    url_file = Path(profile.database.url_file or str(default_url_file))
    if url_file.exists():
        url = url_file.read_text(encoding="utf-8").strip()
        if url:
            return url
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("SHITTIM_CHEST_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def backup(profile: DeployProfile, dest: Path,
           executor: Callable[..., subprocess.CompletedProcess] = _run) -> BackupResult:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(dest) / "{}-{}".format(profile.host.face, stamp)
    out.mkdir(parents=True)
    result = BackupResult(path=out)

    data_root = profile.host.deploy_root() / "data"
    if data_root.exists():
        tar_path = out / "data.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(str(data_root), arcname="data")
        result.entries["data.tar.gz"] = sha256_file(tar_path)
    else:
        result.skipped.append("data (absent)")

    env_path = profile.host.etc_env()
    if env_path.exists():
        copy = out / "env"
        shutil.copy2(env_path, copy)
        os.chmod(copy, 0o600)
        result.entries["env"] = sha256_file(copy)
    else:
        result.skipped.append("env (absent)")

    db_url = resolve_db_url(profile)
    if db_url:
        dump = out / "db.dump"
        proc = executor(["pg_dump", "--no-owner", "--no-privileges",
                         "--file", str(dump), db_url], timeout=600)
        if proc.returncode == 0 and dump.exists() and dump.stat().st_size > 0:
            result.entries["db.dump"] = sha256_file(dump)
        else:
            dump.unlink(missing_ok=True)
            result.skipped.append("db (pg_dump failed or empty)")
    else:
        result.skipped.append("db (no url resolved)")

    manifest = {
        "face": profile.host.face,
        "created": stamp,
        "entries": result.entries,
        "skipped": result.skipped,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ENV_KEY_WARNING)
    return result


def restore(profile: DeployProfile, backup_dir: Path,
            executor: Callable[..., subprocess.CompletedProcess] = _run) -> list[str]:
    """Restore data/env/db from a backup directory. Returns applied steps.

    Callers own service stop/start around this (the upgrade path restarts
    at the end; backups taken on a live service have a tear window between
    the data tarball and the pg_dump snapshot — documented, accepted for
    Phase 0). This function refuses nothing except integrity failures.
    """
    applied: list[str] = []
    manifest_path = Path(backup_dir) / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("no manifest.json in {}".format(backup_dir))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: dict[str, str] = manifest.get("entries", {})

    for name, digest in entries.items():
        path = Path(backup_dir) / name
        actual = sha256_file(path)
        if actual != digest:
            raise IOError("integrity failure: {} expected {} got {}".format(
                name, digest[:12], actual[:12]))

    if "env" in entries:
        dest = profile.host.etc_env()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(backup_dir) / "env", dest)
        os.chmod(dest, 0o600)
        applied.append("env")

    if "data.tar.gz" in entries:
        data_root = profile.host.deploy_root() / "data"
        data_root.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(Path(backup_dir) / "data.tar.gz", "r:gz") as tf:
            from celestia_devtools.pyshim import safe_extract
            safe_extract(tf, str(data_root.parent))
        applied.append("data")

    if "db.dump" in entries:
        db_url = resolve_db_url(profile)
        if not db_url:
            raise RuntimeError("db.dump present but no DB URL resolvable")
        proc = executor(["psql", "--quiet", "--file",
                         str(Path(backup_dir) / "db.dump"), db_url,
                         "-v", "ON_ERROR_STOP=1"], timeout=600)
        if proc.returncode != 0:
            raise RuntimeError("psql restore failed: {}".format(
                (proc.stderr or "").strip()[:200]))
        applied.append("db")
    return applied

#!/usr/bin/env python3
"""`deploy verify`, `deploy status`, `deploy uninstall` (wiring slice).

verify: health/epoch checks against a running face — the same suite serves
fresh installs and read-only baselines of live machines.
status: 入驻判据 via `auth.checkSetup` (needs_setup == user count zero) plus
the deployment ledger tail — the re-install guard the plan demands (§1.8:
rotate-secrets refuses once real users exist).
uninstall: stop the unit, remove files; `--purge-data` is a separate
explicit flag (rotate ≠ destroy, same fail-closed rule as the secrets
stage).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from celestia_devtools.deploy import lifecycle
from celestia_devtools.deploy.profile import DeployProfile

Executor = Callable[..., subprocess.CompletedProcess]


def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _http_get(url: str, timeout: int = 15) -> tuple[int, str]:
    import urllib.request
    # loopback: env proxies must not intercept (R2 P3-3)
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _rpc(base: str, method: str, params: dict | None = None) -> tuple[int, dict]:
    import urllib.request
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}).encode()
    req = urllib.request.Request(base + "/api/rpc", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}
    except (OSError, json.JSONDecodeError) as exc:
        return 0, {"error": str(exc)}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def verify(prof: DeployProfile, http_get=_http_get) -> list[Check]:
    base = "http://127.0.0.1:{}".format(prof.host.listen)
    checks: list[Check] = []
    status, body = http_get(base + "/api/health")
    checks.append(Check("health", status == 200,
                        "HTTP {}".format(status if status else body[:60])))
    status, body = http_get(base + "/")
    epoch = ""
    if status == 200:
        import re
        m = re.search(r"main-[A-Za-z0-9_-]+\.js", body)
        epoch = m.group(0) if m else ""
    checks.append(Check("epoch", bool(epoch),
                        epoch or "no main-*.js in index"))
    rows = lifecycle.history_read(prof)
    checks.append(Check("ledger", True,
                        "{} entries; last: {}".format(
                            len(rows),
                            rows[-1]["action"] if rows else "none")))
    backups = prof.host.deploy_root() / "backups"
    if backups.exists():
        ages = sorted(backups.iterdir(), reverse=True)
        checks.append(Check("backup-freshness", bool(ages),
                            str(ages[0].name) if ages else "no backups"))
    else:
        checks.append(Check("backup-freshness", False, "no backup dir"))
    return checks


@dataclass
class FaceStatus:
    needs_setup: bool | None = None
    reachable: bool = False
    history: list[dict] = field(default_factory=list)

    @property
    def registered(self) -> bool:
        return self.reachable and self.needs_setup is False


def status(prof: DeployProfile, rpc=_rpc) -> FaceStatus:
    out = FaceStatus()
    base = "http://127.0.0.1:{}".format(prof.host.listen)
    code, body = rpc(base, "auth.checkSetup")
    if code == 200 and "result" in body:
        out.reachable = True
        out.needs_setup = bool(body["result"].get("needs_setup"))
    out.history = lifecycle.history_read(prof)
    return out


def uninstall(prof: DeployProfile, purge_data: bool = False,
              executor: Executor = _ok) -> list[str]:
    """Stop the unit, remove config/binary; data survives unless --purge-data
    (rotate ≠ destroy). Returns the applied steps."""
    steps: list[str] = []
    unit = prof.host.unit_name()
    proc = executor(["systemctl", "stop", unit], timeout=300)
    if getattr(proc, "returncode", 0) == 0:
        steps.append("stopped {}".format(unit))
    executor(["systemctl", "disable", unit], timeout=60)
    steps.append("disabled {}".format(unit))
    for path in (prof.host.deploy_root() / "bin",
                 Path("/etc/systemd/system") / unit,
                 prof.host.etc_env()):
        if path.exists():
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
            steps.append("removed {}".format(path))
    if purge_data:
        data = prof.host.deploy_root() / "data"
        if data.exists():
            import shutil
            shutil.rmtree(data)
            steps.append("purged {}".format(data))
    return steps


def assert_rotation_allowed(prof: DeployProfile, rpc=_rpc) -> tuple[bool, str]:
    """The §1.8 gate: rotate-secrets may only run while needs_setup is true
    (no real users yet). Fail closed when the answer is unknown."""
    st = status(prof, rpc=rpc)
    if not st.reachable:
        return False, "face unreachable — cannot prove 'no users yet'; refusing"
    if st.registered:
        return False, ("face already has real users (needs_setup=false); "
                       "re-install would destroy them — use password reset "
                       "or secrets rotate instead")
    return True, "needs_setup=true — fresh install, rotation allowed"

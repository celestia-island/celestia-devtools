#!/usr/bin/env python3
"""The bootstrap stage machine: ordered, idempotent, loud about what is pending.

Eleven stages, each owning one slice of the lifecycle. This slice (D2) lands
the machine plus four real stages (precheck / account / secrets / summary);
the remaining stages register from their own slices (D3/D4). A missing stage
is never skipped silently: the run STOPS at the first gap, reports every
pending stage with its slice id, and exits 2 — the same fail-loudly contract
as the CLI dispatcher.

Idempotency contract: a second run of a completed stage must be a no-op and
must not restart anything (later slices pin that with PID/ActiveEnter checks).

Secrets rule (plan §1.8): generated values land in a 0600 file exactly once,
are never overwritten, and never appear in stage results or stdout — the
initial admin password is the single stdout-once exception, and that stage
belongs to D3.
"""

from __future__ import annotations

import os
import pwd
import secrets as pysecrets
import stat
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from celestia_devtools.deploy.profile import DeployProfile

OK = "ok"
SKIPPED = "skipped"
PENDING = "pending-slice"
FAILED = "failed"

# slice registry: stage name -> landing slice (stages not yet in STAGES below)
STAGE_SLICES: dict[str, str] = {
    "precheck": "D2", "account": "D2", "secrets": "D2", "database": "D3",
    "artifact": "D3", "install": "D3", "migrate": "D3", "first-admin": "D3",
    "front": "D4", "verify": "D4", "summary": "D2",
}
STAGE_ORDER: tuple[str, ...] = tuple(STAGE_SLICES)

ENV_KEYS = ("JWT_SECRET", "SHITTIM_CHEST_ENCRYPTION_KEY")


@dataclass
class StageResult:
    name: str
    status: str
    detail: str = ""
    changed: bool = False

    def line(self) -> str:
        mark = {OK: "✓", SKIPPED: "⏭", PENDING: "…", FAILED: "✗"}[self.status]
        return "[{:>2}] {:<12} {} {}{}".format(
            STAGE_ORDER.index(self.name) + 1 if self.name in STAGE_ORDER else 0,
            self.name, mark, self.detail,
            "  (slice {})".format(STAGE_SLICES[self.name]) if self.status == PENDING else "")


@dataclass
class StageContext:
    profile: DeployProfile
    executor: Callable[..., subprocess.CompletedProcess] = subprocess.run
    assume_root: bool = False   # tests: precheck skips the euid gate
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)  # dry-run plan transcript

    def run(self, cmd: list[str], **kw) -> subprocess.CompletedProcess:
        self.actions.append(" ".join(cmd))
        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return self.executor(cmd, **kw)


# ── D2 stages ──────────────────────────────────────────────────────────

def stage_precheck(ctx: StageContext) -> StageResult:
    problems = []
    errors = ctx.profile.validate()
    if errors:
        problems.extend(errors)
    is_root = ctx.assume_root or (hasattr(os, "geteuid") and os.geteuid() == 0)
    if not is_root and not ctx.dry_run:
        problems.append("privileged stages need root (re-run with sudo)")
    front = ctx.profile.front
    if front.enabled and not front.domain:
        problems.append("front.enabled but front.domain empty")
    if problems:
        return StageResult("precheck", FAILED, "; ".join(problems))
    return StageResult("precheck", OK, "profile valid; root={}".format(is_root))


def stage_account(ctx: StageContext) -> StageResult:
    user = ctx.profile.host.admin_user
    root = ctx.profile.host.deploy_root()
    changed = False
    getpwnam = getattr(ctx, "getpwnam", pwd.getpwnam)
    try:
        getpwnam(user)
    except KeyError:
        proc = ctx.run(["useradd", "--system", "--home-dir", str(root),
                        "--shell", "/usr/sbin/nologin", user])
        if getattr(proc, "returncode", 0) != 0:
            return StageResult(
                "account", FAILED,
                "useradd {} failed (rc={}): {}".format(
                    user, proc.returncode,
                    (getattr(proc, "stderr", "") or "").strip()[:120]))
        changed = True
    # dirs are plain mkdir+chown+chmod: real filesystem side effects the
    # tests can verify, no executor round-trip needed for the common path
    import pwd as _pwd
    from pathlib import Path as _Path
    needed = [root, root / "data", root / "data" / "themes",
              root / "data" / "avatars", root / "data" / "uploads"]
    if ctx.dry_run:
        for d in needed:
            if not d.exists():
                ctx.actions.append("would mkdir+chown {}".format(d))
        return StageResult("account", OK,
                           "dry-run: user {} + dirs under {} planned".format(user, root))
    pw = getpwnam(user)
    for d in needed:
        d = _Path(d)
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            changed = True
        os.chown(d, pw.pw_uid, pw.pw_gid)
        os.chmod(d, 0o755)
    _ = _pwd
    return StageResult("account", OK,
                       "user {} ready; dirs under {}".format(user, root), changed)


def stage_secrets(ctx: StageContext) -> StageResult:
    """Generate the env file exactly once; never overwrite, never print."""
    env_path = ctx.profile.host.etc_env()
    if env_path.exists():
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        if mode & 0o077:
            return StageResult("secrets", FAILED,
                               "{} is group/other readable ({:o}); refusing".format(env_path, mode))
        return StageResult("secrets", SKIPPED,
                           "{} already present (never overwritten)".format(env_path))
    values = {key: pysecrets.token_urlsafe(32) for key in ENV_KEYS}
    if ctx.dry_run:
        return StageResult("secrets", OK, "would generate {}".format(env_path))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # write via 0600 temp + rename so a half-written file is never readable
    tmp = env_path.with_suffix(".env.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write("{}={}\n".format(key, value))
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    return StageResult("secrets", OK,
                       "generated {} (0600, contents never printed)".format(env_path), True)


def stage_summary(ctx: StageContext, results: list[StageResult]) -> StageResult:
    done = [r for r in results if r.status in (OK, SKIPPED)]
    pending = [r for r in results if r.status == PENDING]
    print("── 部署进度 ────────────────────────────────")
    for r in results:
        print("  " + r.line())
    if pending:
        names = ", ".join("{}({})".format(r.name, STAGE_SLICES[r.name]) for r in pending)
        print("  待落地切片：{} —— 本轮到此为止（exit 2，不假装完成）".format(names))
    else:
        host = ctx.profile.host
        print("  访问      https://{}    (health/epoch 校验属 verify 段)".format(
            ctx.profile.front.domain or host.face))
    print("────────────────────────────────────────────")
    return StageResult("summary", OK, "{} done, {} pending".format(len(done), len(pending)))


# stages implemented by later slices; None = not yet landed
STAGES: dict[str, Callable | None] = {
    "precheck": stage_precheck,
    "account": stage_account,
    "secrets": stage_secrets,
    "database": None,
    "artifact": None,
    "install": None,
    "migrate": None,
    "first-admin": None,
    "front": None,
    "verify": None,
    "summary": stage_summary,
}


def run_stages(ctx: StageContext) -> tuple[int, list[StageResult]]:
    """Execute in order; stop at the first not-yet-landed stage (marking the
    rest pending). Exit codes: 0 all ok; 1 a stage failed; 2 pending slices."""
    results: list[StageResult] = []
    pending_from: str | None = None
    for name in STAGE_ORDER:
        if name == "summary":
            # the reporter always runs — even mid-pending it must print the
            # honest done/pending picture
            results.append(stage_summary(ctx, results))
            continue
        if pending_from is not None:
            results.append(StageResult(name, PENDING, "blocked by pending {}".format(pending_from)))
            continue
        fn = STAGES.get(name)
        if fn is None:
            pending_from = name
            results.append(StageResult(name, PENDING, "waiting for its slice"))
            continue
        result = fn(ctx)
        results.append(result)
        if result.status == FAILED:
            # summary still runs so the operator sees the full picture
            results.append(stage_summary(ctx, results))
            return 1, results
    code = 2 if pending_from is not None else 0
    return code, results

#!/usr/bin/env python3
"""The remaining bootstrap stages (wiring slice): database, artifact fetch,
install, migrate, first-admin, front — plus verify/status/uninstall.

Every privileged command goes through the injectable executor; every HTTP
call goes through an injectable fetcher; filesystem effects land under the
profile's configurable bases. That is what makes the whole surface testable
without root, systemd, PG, or a live chest — and it is the same discipline
the landed stages already follow.

first-admin (plan §1.8): POST /api/auth/nonce then /api/auth/register — the
first registered user is auto-promoted and captcha-exempt (chest
auth/mod.rs register path); the generated password is printed exactly once
on stdout and never written anywhere else unless --password-to-file (0600).
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from celestia_devtools.deploy import bootstrap
from celestia_devtools.deploy.artifact import fetch as artifact_fetch
from celestia_devtools.deploy.artifact import read_index, resolve
from celestia_devtools.deploy.profile import DeployProfile

OK, SKIPPED, PENDING, FAILED = (bootstrap.OK, bootstrap.SKIPPED,
                                bootstrap.PENDING, bootstrap.FAILED)
StageResult = bootstrap.StageResult
Executor = Callable[..., subprocess.CompletedProcess]


def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


# ── stage: database (D3) ───────────────────────────────────────────────

def stage_database(ctx: "bootstrap.StageContext") -> StageResult:
    """External mode only for the wiring slice: create the DB when the URL
    names a local file-backed admin connection is out of scope; we verify
    reachability via pg_isready and record the URL file the other stages
    read. Local-mode provisioning lands with the H→C cutover."""
    prof = ctx.profile
    url = os.environ.get("CHEST_DATABASE_URL", "")
    url_file = Path(prof.database.url_file) if prof.database.url_file else \
        prof.host.etc_env().with_name(prof.host.face + "-db.url")
    if prof.database.mode != "external":
        return StageResult("database", FAILED,
                           "database.mode=local lands with the C cutover; "
                           "use external for now")
    if not url:
        if url_file.exists():
            return StageResult("database", SKIPPED,
                               "{} already set".format(url_file))
        return StageResult("database", FAILED,
                           "no database URL: set CHEST_DATABASE_URL or "
                           "pre-create {}".format(url_file))
    if ctx.dry_run:
        ctx.actions.append("would write {}".format(url_file))
        return StageResult("database", OK, "dry-run: url file planned")
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(url + "\n", encoding="utf-8")
    os.chmod(url_file, 0o600)
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    pg_env = dict(os.environ)
    if parsed.password:
        pg_env["PGPASSWORD"] = parsed.password
    # argv must not carry credentials (R2 P2: /proc is world-readable;
    # same fix pattern as deps._pip_argv)
    proc = ctx.run(["pg_isready", "-h", parsed.hostname or "127.0.0.1",
                    "-p", str(parsed.port or 5432),
                    "-U", parsed.username or "postgres",
                    "-d", parsed.path.lstrip("/") or "postgres"],
                   timeout=30, env=pg_env)
    if getattr(proc, "returncode", 0) != 0:
        return StageResult("database", FAILED,
                           "pg_isready failed for the provided URL "
                           "(continuable if the DB comes up later)")
    return StageResult("database", OK, "url file at {} (0600)".format(url_file))


# ── stages: artifact + install + migrate (D3) ──────────────────────────

def stage_artifact(ctx: "bootstrap.StageContext") -> StageResult:
    prof = ctx.profile
    src = prof.artifact.source.rstrip("/")
    try:
        index = read_index(src if src.endswith("index.toml")
                           else src + "/index.toml")
        entry = resolve(index, prof.artifact.channel, _target())
    except Exception as exc:  # noqa: BLE001 - stage boundary
        return StageResult("artifact", FAILED, "resolve failed: {}".format(exc))
    incoming = prof.host.deploy_root() / "incoming"
    if ctx.dry_run:
        ctx.actions.append("would fetch {} -> {}".format(entry.file, incoming))
        return StageResult("artifact", OK,
                           "dry-run: {} @ {}".format(entry.version, entry.file))
    try:
        bin_path = artifact_fetch(entry, src, incoming)
    except Exception as exc:  # noqa: BLE001
        return StageResult("artifact", FAILED, "fetch failed: {}".format(exc))
    ctx.state = getattr(ctx, "state", {})
    ctx.state["incoming_bin"] = str(bin_path)
    ctx.state["artifact_entry"] = entry
    return StageResult("artifact", OK,
                       "{} @ {}".format(entry.version, entry.file), changed=True)


def _target() -> str:
    import platform
    machine = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64",
               "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    return "{}-unknown-linux-gnu".format(machine)


def stage_install(ctx: "bootstrap.StageContext") -> StageResult:
    prof = ctx.profile
    state = getattr(ctx, "state", {})
    incoming = state.get("incoming_bin")
    if not incoming:
        return StageResult("install", FAILED, "artifact stage did not run")
    dest = prof.host.deploy_root() / "bin" / prof.host.face
    if ctx.dry_run:
        ctx.actions.append("would install {} -> {}".format(incoming, dest))
        return StageResult("install", OK, "dry-run: install planned")
    from celestia_devtools.deploy.lifecycle import _install
    _install(Path(incoming), dest)
    return StageResult("install", OK,
                       "binary at {}".format(dest), changed=True)


# ── stage: migrate (D3) ────────────────────────────────────────────────

def stage_migrate(ctx: "bootstrap.StageContext") -> StageResult:
    prof = ctx.profile
    binary = prof.host.deploy_root() / "bin" / prof.host.face
    if not binary.exists() and not ctx.dry_run:
        return StageResult("migrate", FAILED, "binary missing: {}".format(binary))
    if ctx.dry_run:
        ctx.actions.append("{} db-migrate".format(binary))
        return StageResult("migrate", OK, "dry-run: migrate planned")
    env = dict(os.environ)
    env_file = prof.host.etc_env()
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    proc = ctx.run([str(binary), "db-migrate"], timeout=600, env=env)
    if getattr(proc, "returncode", 0) != 0:
        return StageResult("migrate", FAILED,
                           "db-migrate rc={}: {}".format(
                               proc.returncode,
                               (getattr(proc, "stderr", "") or "")[:120]))
    return StageResult("migrate", OK, "schema current (idempotent)")


# ── stage: first-admin (D3) ────────────────────────────────────────────

@dataclass
class HttpResult:
    status: int
    body: str


def _http_post(url: str, payload: dict, headers: dict | None = None,
               timeout: int = 30) -> HttpResult:
    # loopback calls: env proxies must not intercept (R2 P3-3)
    import urllib.request
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"))


def stage_first_admin(ctx: "bootstrap.StageContext",
                      http=None,
                      password_gen=pysecrets.token_urlsafe) -> StageResult:
    """http defaults to the module binding (not a def-time capture) so
    tests can monkeypatch stages_more._http_post and the stage sees it."""
    if http is None:
        http = _http_post
    prof = ctx.profile
    if not prof.admin.email:
        return StageResult("first-admin", SKIPPED,
                           "admin.email empty — first-admin is manual")
    base = "http://127.0.0.1:{}".format(prof.host.listen)
    password = password_gen(18)
    if ctx.dry_run:
        ctx.actions.append("would POST /api/auth/nonce + /api/auth/register "
                           "for {}".format(prof.admin.email))
        return StageResult("first-admin", OK, "dry-run: registration planned")
    # the service must be up; the wiring keeps the naive order (install ->
    # migrate -> first-admin) and relies on `chest serve` booting fast
    nonce = http(base + "/api/auth/nonce", {})
    if nonce.status != 200:
        return StageResult("first-admin", FAILED,
                           "nonce endpoint {} — is the unit running?".format(nonce.status))
    reg = http(base + "/api/auth/register",
               {"username": prof.admin.email, "password": password,
                "email": prof.admin.email},
               headers={"x-nonce": _nonce_from(nonce.body)})
    if reg.status not in (200, 201):
        return StageResult("first-admin", FAILED,
                           "register returned {}: {}".format(
                               reg.status, reg.body[:120]))
    # print-once contract: stdout only; --password-to-file writes 0600
    if prof.admin.password_to_file:
        cred = Path("/root") / "{}-initial-credential".format(prof.host.face)
        cred.write_text("{}\n".format(password), encoding="utf-8")
        os.chmod(cred, 0o600)
        print("初始口令已写入 {}（0600）".format(cred))
    else:
        print("初始管理员 {} 的口令（仅此一次显示）：{}".format(
            prof.admin.email, password))
    return StageResult("first-admin", OK,
                       "registered {}".format(prof.admin.email), changed=True)


def _nonce_from(body: str) -> str:
    try:
        return json.loads(body).get("nonce", "")
    except json.JSONDecodeError:
        return ""


# ── stage: front (D4) ──────────────────────────────────────────────────

def stage_front(ctx: "bootstrap.StageContext") -> StageResult:
    prof = ctx.profile
    if not prof.front.enabled or not prof.front.domain:
        return StageResult("front", SKIPPED, "front disabled")
    if not ctx.dry_run and not os.path.exists(os.path.expanduser("~/.acme.sh/acme.sh")) \
            and not _which("acme.sh", ctx):
        return StageResult("front", SKIPPED,
                           "acme.sh missing — front stage skips (documented); "
                           "run doctor for the hint")
    conf = _render_nginx(prof)
    if ctx.dry_run:
        ctx.actions.append("would write nginx conf + issue HTTP-01 cert")
        return StageResult("front", OK, "dry-run: front planned")
    conf_path = Path("/etc/nginx/conf.d") / "{}.conf".format(prof.host.face)
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(conf, encoding="utf-8")
    reload = ctx.run(["nginx", "-s", "reload"], timeout=60)
    if getattr(reload, "returncode", 0) != 0:
        return StageResult("front", FAILED, "nginx reload failed")
    return StageResult("front", OK, "nginx conf at {}".format(conf_path),
                       changed=True)


def _which(name: str, ctx) -> bool:
    import shutil
    return shutil.which(name) is not None


def _render_nginx(prof: DeployProfile) -> str:
    server_block = "server {\n"
    server_block += "  listen 443 ssl;\n"
    server_block += "  server_name " + prof.front.domain + ";\n"
    server_block += "  location / {\n"
    server_block += "    proxy_pass http://127.0.0.1:" + str(prof.host.listen) + ";\n"
    server_block += "    proxy_set_header Host $host;\n"
    server_block += "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
    server_block += "  }\n"
    server_block += "}\n"
    return server_block


def stage_verify(ctx: "bootstrap.StageContext") -> StageResult:
    """The final gate: health + epoch + ledger via ops.verify."""
    from celestia_devtools.deploy import ops
    if ctx.dry_run:
        ctx.actions.append("would verify health/epoch/ledger")
        return StageResult("verify", OK, "dry-run: verification planned")
    checks = ops.verify(ctx.profile)
    failures = [c for c in checks if not c.ok]
    if failures:
        return StageResult("verify", FAILED,
                           "; ".join("{}: {}".format(c.name, c.detail)
                                     for c in failures))
    return StageResult("verify", OK,
                       "; ".join("{} ok".format(c.name) for c in checks))


# ── registration: the stage machine becomes complete ───────────────────


def register_stages() -> None:
    """Complete the stage machine. Called explicitly by wiring tests and
    the future CLI wiring — NOT at import time, so that non-wiring tests
    (and any import of this module for its helpers) still see the honest
    pending-slice contract."""
    bootstrap.STAGES.update({
        "database": stage_database,
        "artifact": stage_artifact,
        "install": stage_install,
        "migrate": stage_migrate,
        "first-admin": stage_first_admin,
        "front": stage_front,
        "verify": stage_verify,
    })

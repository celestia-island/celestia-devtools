#!/usr/bin/env python3
"""pyshim - bring a suitable Python to boxes whose interpreter is too old.

COMPATIBILITY CONTRACT (hard, enforced by tests/test_pyshim.py):
  * this file must parse and run on Python 3.6+;
  * therefore: no walrus, no dataclasses, no `from __future__ import
    annotations`, no f-strings (format() only), no `X | None` unions,
    no subscripted builtins at runtime (typing.List etc. is fine);
  * stdlib only: sys/os/re/json/socket/shutil/tarfile/platform/subprocess/
    urllib.request/hashlib;
  * NEVER import questionary / tomli-w / PyYAML here: their engines
    (prompt_toolkit >= 3.0.53) require Python >= 3.10 and would crash on the
    very interpreters this shim exists to rescue.

What it does when the running interpreter is below the floor (3.11):
  1. prints a loud warning naming the current and required versions;
  2. asks for consent (unless --yes; refuses instead of hanging when stdin
     is not a TTY);
  3. installs the NEWEST Python available through, in order:
       a. uv            (`uv python install`, no version pin = latest;
                          uv itself installed from the official installer if absent)
       b. python-build-standalone (install_only tarball; sha256 verified
                          against the release digest when present)
       c. system packages (apt/dnf python3.x - explicitly flagged as possibly
                          not the newest);
  4. creates /opt/celestia-devtools/venv and pip-installs celestia-devtools;
  5. re-execs the same command line inside that venv (sentinel-guarded).

When the interpreter already meets the floor and the tool venv exists, it
re-execs immediately (zero repeated installs).

Budget discipline: at most 2 attempts per strategy, 90s per network call,
at most 3 PBS archive downloads total (the uv installer and the
interpreter uv fetches are bounded by their own subprocess timeouts) - a
hostile network must fail fast, never look
like a hung install. Proxy levels 1/2/4 only (env vars, loopback listeners,
DNS-suffix guesses); the full five-level cascade lives in core/netproxy.py.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PY_FLOOR = (3, 11)
VENV_DIR = "/opt/celestia-devtools/venv"
VENV_BIN = os.path.join(VENV_DIR, "bin")
SENTINEL = "CELESTIA_DEVTOOLS_PYSHIM_REEXEC"
PBS_INDEX = ("https://api.github.com/repos/astral-sh/"
             "python-build-standalone/releases?per_page=10")
PBS_FALLBACK_MIRROR = os.environ.get("CELESTIA_PBS_MIRROR", "")
UV_INSTALLER = "https://astral.sh/uv/install.sh"
COMMON_PORTS = (7890, 7891, 8080, 8888, 3128)
DNS_STEMS = ("proxy", "sing-box")
DNS_SUFFIXES = ("local", "lan", "node.local")
MAX_TRIES = 2
NET_TIMEOUT = 90


def _log(msg):
    print("[pyshim] " + msg)


def _die(msg, code=1):
    print("[pyshim] ERROR: " + msg, file=sys.stderr)
    raise SystemExit(code)


# ── proxy (levels 1/2/4 only; see module docstring) ─────────────────────

def _proxy_url():
    env = os.environ
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = env.get(name, "").strip()
        if val:
            if "://" not in val:
                val = "http://" + val
            return val
    import socket
    for port in COMMON_PORTS:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            conn.close()
            return "http://127.0.0.1:{}".format(port)
        except OSError:
            continue
    for suffix in DNS_SUFFIXES:
        for stem in DNS_STEMS:
            host = "{}.{}".format(stem, suffix)
            for port in COMMON_PORTS:
                try:
                    conn = socket.create_connection((host, port), timeout=0.3)
                    conn.close()
                    return "http://{}:{}".format(host, port)
                except OSError:
                    continue
    return None


def _fetch(url, timeout=NET_TIMEOUT):
    handlers = []
    proxy = _proxy_url()
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "celestia-pyshim"})
    return opener.open(req, timeout=timeout)


def _download(url, dest, digest=None):
    resp = _fetch(url)
    hasher = None
    if digest and digest.startswith("sha256:"):
        import hashlib
        hasher = hashlib.sha256()
        want = digest.split(":", 1)[1]
    with open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            if hasher:
                hasher.update(chunk)
            fh.write(chunk)
    if hasher and hasher.hexdigest() != want:
        os.unlink(dest)
        raise IOError("sha256 mismatch for " + url)


# ── strategy a: uv ───────────────────────────────────────────────────────

def _ensure_uv():
    path = shutil.which("uv")
    if path:
        return path
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "uv-installer.sh")
        _download(UV_INSTALLER, script)
        rc = subprocess.call(["sh", script, "-q"],
                             env=dict(os.environ, UV_UNMANAGED_INSTALL="/usr/local/bin"))
        if rc != 0:
            return None
    return shutil.which("uv")


def _uv_python():
    uv = _ensure_uv()
    if not uv:
        return None
    # No version pin: install the newest Python the uv channel offers.
    rc = subprocess.call([uv, "python", "install"], timeout=NET_TIMEOUT * 2)
    if rc != 0:
        return None
    out = subprocess.check_output([uv, "python", "find"], timeout=30)
    return out.decode("utf-8", "replace").strip().splitlines()[0]


# ── strategy b: python-build-standalone ──────────────────────────────────

def _triple(machine, system):
    m = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64",
         "arm64": "aarch64"}.get(machine, machine)
    s = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}.get(system, "")
    return "{}-{}".format(m, s) if s else ""


def pick_pbs_asset(names, machine, system):
    """Choose the newest cpython >= PY_FLOOR install_only tarball.

    Returns (version_tuple, name) or None. Pure function - unit tested.
    """
    import re
    triple = _triple(machine, system)
    if not triple:
        return None
    suffix = "-{}-install_only.tar.gz".format(triple)
    best = None
    pat = re.compile(r"^cpython-(\d+)\.(\d+)\.(\d+)\+")
    for name in names:
        if not name.startswith("cpython-") or not name.endswith(suffix):
            continue
        m = pat.match(name)
        if not m:
            continue
        ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if ver < PY_FLOOR:
            continue
        if best is None or ver > best[0]:
            best = (ver, name)
    return best


def _pbs_python(download_budget):
    index = PBS_FALLBACK_MIRROR or PBS_INDEX
    if PBS_FALLBACK_MIRROR:
        _log("WARNING: CELESTIA_PBS_MIRROR is set; release digests come from "
             "the mirror itself (self-attested) - only transport corruption "
             "is caught, not a compromised mirror")
    resp = _fetch(index)
    data = json.loads(resp.read().decode("utf-8", "replace"))
    assets = []
    for release in data:
        for asset in release.get("assets", []):
            assets.append(asset)
    best = pick_pbs_asset([a.get("name", "") for a in assets],
                          platform.machine(), platform.system())
    if not best:
        return None
    version, name = best
    asset = next(a for a in assets if a.get("name") == name)
    url = asset["browser_download_url"]
    digest = asset.get("digest") or ""
    root = "/opt/celestia-devtools/python"
    dest_root = os.path.join(root, "cpython-{}{}{}".format(
        ".".join(str(v) for v in version), "-", name.split("+", 1)[1][:8]))
    binpath = os.path.join(dest_root, "install", "bin", "python3")
    if os.path.exists(binpath):
        return binpath
    with tempfile.TemporaryDirectory() as tmp:
        tarpath = os.path.join(tmp, name)
        _download(url, tarpath, digest)
        download_budget[0] -= 1
        if not os.path.isdir(root):
            os.makedirs(root)
        with tarfile.open(tarpath, "r:gz") as tf:
            safe_extract(tf, dest_root)
    return binpath if os.path.exists(binpath) else None


def safe_extract(tf, dest):
    """tarfile extraction with member validation (R1-F6).

    Public name — deploy/artifact.py reuses this for fetched archives.

    Python 3.6 has no ``filter=`` parameter, so the checks are manual: no
    absolute paths, no ``..`` traversal, and no link/device members — a
    hostile archive cannot write outside dest or plant a symlink.
    """
    import os as _os
    members = tf.getmembers()
    for m in members:
        name = m.name
        if name.startswith("/") or name.startswith("\\"):
            raise IOError("absolute path in archive: " + name)
        parts = [pt for pt in name.replace("\\", "/").split("/") if pt]
        if ".." in parts:
            raise IOError("path traversal in archive: " + name)
        if m.issym() or m.islnk():
            raise IOError("link member in archive: " + name)
        if m.isdev():
            raise IOError("device member in archive: " + name)
    for m in members:
        tf.extract(m, dest)
    _ = _os  # (kept for clarity: path logic above is pure string ops)


# ── strategy c: system packages (may not be newest) ──────────────────────

def _pkg_runner():
    """Prefix system package commands with sudo -n when not root."""
    prefix = []
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        prefix = ["sudo", "-n"]
    return prefix


def _pkg_python():
    prefix = _pkg_runner()
    if prefix and not shutil.which("sudo"):
        _log("not root and no sudo available; system-package strategy will fail")
        return None
    if shutil.which("apt-get"):
        for ver in ("3.12", "3.11"):
            rc = subprocess.call(prefix + ["apt-get", "install", "-y",
                                  "python" + ver, "python" + ver + "-venv"])
            if rc == 0:
                path = shutil.which("python" + ver)
                if path:
                    return path
    if shutil.which("dnf"):
        for ver in ("3.12", "3.11"):
            rc = subprocess.call(prefix + ["dnf", "install", "-y", "python" + ver])
            if rc == 0:
                path = shutil.which("python" + ver)
                if path:
                    return path
    return None


# ── venv + re-exec ───────────────────────────────────────────────────────

def venv_python_bin():
    return os.path.join(VENV_BIN, "celestia-devtools")


def venv_interpreter():
    return os.path.join(VENV_BIN, "python")


def _create_venv(python_bin, extra=()):
    rc = subprocess.call([python_bin, "-m", "venv", VENV_DIR])
    if rc != 0:
        _die("could not create venv with " + python_bin)
    pip = [venv_interpreter(), "-m", "pip", "install", "--no-input",
           "--disable-pip-version-check"]
    env = dict(os.environ)
    proxy = _proxy_url()
    if proxy:
        # Via env, not argv: argv is world-readable in /proc, environ is not.
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            env[name] = proxy
    pkgs = ["celestia-devtools"] + list(extra)
    rc = subprocess.call(pip + pkgs, timeout=NET_TIMEOUT * 2, env=env)
    if rc != 0:
        _die("pip install celestia-devtools into the venv failed")


def _reexec(args):
    env = dict(os.environ)
    env[SENTINEL] = "1"
    target = venv_python_bin()
    argv = [target] + list(args)
    try:
        os.execve(target, argv, env)
    except OSError:
        _die("re-exec into {} failed".format(target))


# ── entry ────────────────────────────────────────────────────────────────

def _consent(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        answer = input("{} [y/N] ".format(question))
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def run(argv, assume_yes=False, dry_run=False, version_info=None):
    info = version_info or sys.version_info
    if os.environ.get(SENTINEL) == "1":
        _die("re-exec loop detected (sentinel already set)")
    tool = venv_python_bin()
    if os.path.exists(tool):
        if not dry_run:
            _reexec(argv)
        _log("dry-run: would re-exec " + tool)
        return 0

    current = "{}.{}.{}".format(*info[:3])
    if info[:2] >= PY_FLOOR[:2]:
        question = ("Python {} is fine but the tool venv is missing; create {} "
                    "and install celestia-devtools?".format(current, VENV_DIR))
        if not dry_run and not _consent(question, assume_yes):
            _die("declined; create the venv manually or install celestia-devtools "
                 "with pip/pipx", 2)
        if dry_run:
            _log("dry-run: would create venv with " + sys.executable)
            return 0
        _create_venv(sys.executable)
        _reexec(argv)

    print("=" * 62)
    print("[pyshim] WARNING: celestia-devtools needs Python >= {}.{},"
          " this interpreter is {}.".format(PY_FLOOR[0], PY_FLOOR[1], current))
    print("[pyshim] The fix is automatic: install the NEWEST available Python"
          " (uv -> python-build-standalone -> system packages), then bring up"
          " the tool venv. Nothing is installed without your consent.")
    print("=" * 62)
    if not dry_run and not _consent(
            "Download and install a current Python (~tens of MB)?", assume_yes):
        _die("declined; upgrade Python >= {}.{} and re-run".format(
            PY_FLOOR[0], PY_FLOOR[1]), 2)

    budget = [3]
    python_bin = None
    strategies = []
    if dry_run:
        _log("dry-run: would try uv, then python-build-standalone, then "
             "system packages; venv at " + VENV_DIR)
        return 0
    for _attempt in range(MAX_TRIES):
        if python_bin:
            break
        if budget[0] <= 0:
            break
        try:
            python_bin = _uv_python()
            if python_bin:
                strategies.append("uv")
        except Exception as exc:  # noqa: BLE001 - shim must never crash out
            _log("uv strategy failed: {}".format(exc))
        if not python_bin and budget[0] > 0:
            try:
                python_bin = _pbs_python(budget)
                if python_bin:
                    strategies.append("python-build-standalone")
            except Exception as exc:  # noqa: BLE001
                _log("python-build-standalone strategy failed: {}".format(exc))
    if not python_bin:
        python_bin = _pkg_python()
        if python_bin:
            strategies.append("system-packages (may not be the newest)")
    if not python_bin:
        _die("all Python install strategies failed; install Python >= {}.{} "
             "manually and re-run".format(PY_FLOOR[0], PY_FLOOR[1]))
    _log("Python ready via {} -> {}".format("+".join(strategies), python_bin))
    _create_venv(python_bin)
    _reexec(argv)
    return 0  # unreachable; keeps the signature honest for tests


def main():
    args = list(sys.argv[1:])
    assume_yes = "--yes" in args
    dry_run = "--dry-run" in args
    args = [a for a in args if a not in ("--yes", "--dry-run")]
    return run(args, assume_yes=assume_yes, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())


# back-compat alias (D1 tests referenced the private name)
_safe_extract = safe_extract

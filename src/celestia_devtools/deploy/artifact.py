#!/usr/bin/env python3
"""The artifact channel: build, index, and fetch release tarballs.

Settled policy (plan §1.10): Phase 0 serves the index from a static
directory over nginx on node-1 (presign upgrade comes later), and the
customer machine never runs cargo/pnpm. This module is the whole channel:

* ``build``   — pack one binary into ``<name>-<version>-<target>.tar.gz``
                (+ a ``.sha256`` sidecar, + a version marker inside);
* ``index``   — (re)write ``index.toml`` grouping artifacts by channel;
* ``fetch``   — resolve ``channel + target`` to the newest entry, verify the
                sha256, and extract via pyshim's member-validated extraction
                (no absolute paths, no ``..``, no link/device members).

Sources are ``file://``/plain paths today; ``https://`` works wherever
netproxy detection finds egress (the fetcher routes through it).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

import tomli_w

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from celestia_devtools.core import netproxy
from celestia_devtools.pyshim import safe_extract

BIN_NAME = "chest"
NAME_RE = re.compile(r"^(?P<name>[a-z0-9-]+)-(?P<version>\d+\.\d+\.\d+)"
                     r"-(?P<target>[a-z0-9_-]+)\.tar\.gz$")


class ArtifactError(Exception):
    """Channel failures: unresolvable channel/target, sha mismatch, bad tar."""


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 512), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Entry:
    file: str
    version: str
    target: str
    sha256: str
    size: int
    date: str

    def to_row(self) -> dict[str, object]:
        return {"file": self.file, "version": self.version, "target": self.target,
                "sha256": self.sha256, "size": self.size, "date": self.date}


# ── build ──────────────────────────────────────────────────────────────

def build(bin_path: Path, version: str, target: str, out_dir: Path,
          name: str = BIN_NAME) -> Entry:
    bin_path = Path(bin_path)
    if not bin_path.is_file():
        raise ArtifactError("no such binary: {}".format(bin_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = "{}-{}-{}.tar.gz".format(name, version, target)
    dest = out_dir / fname
    with open(dest, "wb") as out:
        with tarfile.open(fileobj=out, mode="w:gz") as tf:
            info = tf.gettarinfo(str(bin_path), arcname=name)
            info.mode = 0o755
            with open(bin_path, "rb") as fh:
                tf.addfile(info, fh)
            marker = "{}-{}-{}\n".format(name, version, target).encode()
            minfo = tarfile.TarInfo("{}.version".format(name))
            minfo.size = len(marker)
            import io
            tf.addfile(minfo, io.BytesIO(marker))
    digest = sha256_file(dest)
    (out_dir / (fname + ".sha256")).write_text(
        "{}  {}\n".format(digest, fname), encoding="utf-8")
    return Entry(file=fname, version=version, target=target, sha256=digest,
                 size=dest.stat().st_size,
                 date=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))


# ── index ──────────────────────────────────────────────────────────────

def scan_dir(dir_path: Path) -> list[Entry]:
    entries: list[Entry] = []
    for path in sorted(Path(dir_path).glob("*.tar.gz")):
        m = NAME_RE.match(path.name)
        if not m:
            continue
        sidecar = path.with_suffix(".tar.gz.sha256")
        if not sidecar.exists():
            sidecar = path.parent / (path.name + ".sha256")
        digest = ""
        if sidecar.exists():
            digest = sidecar.read_text(encoding="utf-8").split()[0].strip()
        if not digest:
            digest = sha256_file(path)
        entries.append(Entry(file=path.name,
                             version=m.group("version"),
                             target=m.group("target"),
                             sha256=digest,
                             size=path.stat().st_size,
                             date=_dt.datetime.fromtimestamp(
                                 path.stat().st_mtime, _dt.timezone.utc
                             ).strftime("%Y-%m-%d")))
    return entries


def write_index(dir_path: Path, channels: dict[str, list[Entry]]) -> Path:
    doc = {"channels": {name: {"entries": [e.to_row() for e in entries]}
                        for name, entries in channels.items()}}
    path = Path(dir_path) / "index.toml"
    path.write_text(tomli_w.dumps(doc), encoding="utf-8")
    return path


def read_index(source: str | Path) -> dict[str, list[Entry]]:
    """Load an index from a path, file:// URL, or https:// URL."""
    raw = _load_bytes(source)
    if tomllib is None:  # pragma: no cover
        raise ArtifactError("tomllib unavailable")
    doc = tomllib.loads(raw.decode("utf-8"))
    out: dict[str, list[Entry]] = {}
    for channel, body in doc.get("channels", {}).items():
        out[channel] = [Entry(**{k: row[k] for k in
                                 ("file", "version", "target", "sha256",
                                  "size", "date")})
                        for row in body.get("entries", [])]
    return out


def resolve(index: dict[str, list[Entry]], channel: str,
            target: str) -> Entry:
    candidates = [e for e in index.get(channel, [])
                  if e.target == target]
    if not candidates:
        raise ArtifactError(
            "channel {!r} has no artifact for target {!r}".format(channel, target))
    return max(candidates, key=lambda e: _version_tuple(e.version))


# ── fetch ──────────────────────────────────────────────────────────────

def _load_bytes(source: str | Path) -> bytes:
    text = str(source)
    if text.startswith("file://"):
        text = text[len("file://"):]
    if text.startswith("https://"):
        import urllib.request
        cfg = netproxy.detect()
        handlers = []
        if not cfg.direct:
            handlers.append(urllib.request.ProxyHandler(
                {"http": cfg.proxy_url, "https": cfg.proxy_url}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(text, timeout=120) as resp:  # noqa: S310 - pinned https
            return resp.read()
    return Path(text).read_bytes()


def fetch(entry: Entry, source_base: str, dest_dir: Path,
          name: str = BIN_NAME) -> Path:
    """Download/verify/extract; returns the binary path inside dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / entry.file
    base = str(source_base).rstrip("/")
    source = "{}/{}".format(base, entry.file)
    raw = _load_bytes(source)
    archive.write_bytes(raw)
    digest = sha256_file(archive)
    if digest != entry.sha256:
        archive.unlink(missing_ok=True)
        raise ArtifactError("sha256 mismatch for {}: index says {}, got {}".format(
            entry.file, entry.sha256[:12], digest[:12]))
    with tarfile.open(archive, "r:gz") as tf:
        safe_extract(tf, str(dest_dir))
    bin_path = dest_dir / name
    if not bin_path.exists():
        raise ArtifactError("archive {} has no {} member".format(entry.file, name))
    os.chmod(bin_path, 0o755)
    return bin_path


# ── CLI ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="celestia-devtools deploy artifact",
        description="Build/index/fetch release tarballs (static-directory channel).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="pack a binary into <name>-<version>-<target>.tar.gz")
    b.add_argument("--bin", required=True)
    b.add_argument("--version", required=True)
    b.add_argument("--target", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--name", default=BIN_NAME)
    i = sub.add_parser("index", help="(re)write index.toml for a directory")
    i.add_argument("--dir", required=True)
    i.add_argument("--channel", action="append", required=True,
                   help="channel name; artifact set = current dir scan (repeatable)")
    f = sub.add_parser("fetch", help="resolve+verify+extract one artifact")
    f.add_argument("--source", required=True, help="dir, file:// or https:// base")
    f.add_argument("--channel", default="stable")
    f.add_argument("--target", required=True)
    f.add_argument("--dest", required=True)
    args = ap.parse_args()

    try:
        if args.cmd == "build":
            entry = build(Path(args.bin), args.version, args.target,
                          Path(args.out), name=args.name)
            print("{}\nsha256={}".format(entry.file, entry.sha256))
            return 0
        if args.cmd == "index":
            entries = scan_dir(Path(args.dir))
            channels: dict[str, list[Entry]] = {}
            for channel in args.channel:
                channels[channel] = entries
            path = write_index(Path(args.dir), channels)
            print("{}".format(path))
            return 0
        index = read_index(args.source if str(args.source).endswith("index.toml")
                           else "{}/index.toml".format(str(args.source).rstrip("/")))
        entry = resolve(index, args.channel, args.target)
        bin_path = fetch(entry, str(args.source), Path(args.dest))
        print("{}\n{} -> {}".format(entry.file, entry.version, bin_path))
        return 0
    except ArtifactError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

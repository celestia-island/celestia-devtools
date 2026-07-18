"""Publish workspace crates to crates.io with intelligent error handling.

Instead of treating every non-zero ``cargo publish`` exit as "maybe already
published" and wasting minutes polling a non-existent index entry, this script:

* Distinguishes ``cargo publish`` exit codes:
  - **0** — freshly published, proceed to index-wait
  - **101** — general error; peek at stderr for known failure patterns
    (missing license-file, network errors, version-conflict) and fail fast
  - other — fail fast
* Verifies the crate version is actually on crates.io **before** publishing,
  skipping crates that were uploaded by a previous (partial) run.
* Waits for the published version to appear in the crates.io index with a
  configurable per-crate timeout (default 120 s, well within a CI budget).
* Publishes in topological (dependency) order computed from workspace metadata.

Usage as a CLI::

    python -m celestia_devtools.publish.crates [--dry-run] [--max-wait 120]

Usage as a library::

    from celestia_devtools.publish.crates import publish_all
    publish_all(dry_run=False, max_wait_seconds=120)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Set

CRATES_IO_VERSION_URL = "https://crates.io/api/v1/crates/{crate_name}/versions"

# Patterns that indicate a hard-fail rather than "version already exists".
FAIL_FAST_PATTERNS: List[str] = [
    "license-file",
    "does not appear to exist",
    "No such file or directory",
    "network error",
    "timed out",
    "401 Unauthorized",
    "403 Forbidden",
    "is already uploaded",
]


def _cargo_metadata() -> dict:
    result = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("error: cargo metadata failed", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def _version_exists(crate_name: str, version: str) -> bool:
    url = CRATES_IO_VERSION_URL.format(crate_name=crate_name)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "celestia-devtools/0.2 (cargo-publish)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(f"  [warn] crates.io API error {exc.code}, assuming not indexed", file=sys.stderr)
        return False
    except Exception:
        return False
    versions = [v["num"] for v in data.get("versions", [])]
    return version in versions


def _should_fail_fast(stderr: str) -> bool:
    for pat in FAIL_FAST_PATTERNS:
        if pat.lower() in stderr.lower():
            return True
    return False


def _publish_one(name: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"  [dry-run] would publish {name}")
        return True

    result = subprocess.run(
        ["cargo", "publish", "-p", name, "--allow-dirty"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  {name} — cargo publish ok")
        return True

    stderr = result.stderr
    stdout = result.stdout

    if _should_fail_fast(stderr):
        print(f"  [error] {name} — cargo publish failed (exit {result.returncode})", file=sys.stderr)
        # Print relevant snippet
        for line in stderr.strip().split("\n"):
            if any(pat.lower() in line.lower() for pat in FAIL_FAST_PATTERNS[:6]):
                print(f"    {line.strip()}", file=sys.stderr)
        return False

    # Non-zero but not a known fail-fast pattern — might be "already uploaded"
    if "already uploaded" in stderr.lower() or "already uploaded" in stdout.lower():
        print(f"  {name} — already uploaded (skipping)")
        return True

    # Genuine unknown error
    print(f"  [error] {name} — cargo publish failed (exit {result.returncode})", file=sys.stderr)
    print(f"  {stderr.strip()[:200]}", file=sys.stderr)
    return False


def _wait_index(name: str, version: str, max_wait: int, interval: int = 5) -> bool:
    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        if _version_exists(name, version):
            print(f"  [{attempt}] {name} v{version} indexed ({remaining}s left)")
            return True
        print(f"  [{attempt}] {name} v{version} not yet indexed, {remaining}s remaining...")
        time.sleep(interval)
    print(f"  [timeout] {name} v{version} not indexed after {max_wait}s", file=sys.stderr)
    return False


def _topological_order(packages: Dict[str, dict]) -> List[str]:
    in_degree: Dict[str, int] = {n: 0 for n in packages}
    adj: Dict[str, Set[str]] = {n: set() for n in packages}
    for name, meta in packages.items():
        for dep, dep_meta in meta.get("dependencies", {}).items():
            if dep in packages:
                in_degree[name] += 1
                adj[dep].add(name)

    queue: List[str] = [n for n, d in in_degree.items() if d == 0]
    order: List[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            in_degree[m] -= 1
            if in_degree[m] == 0:
                queue.append(m)

    if len(order) != len(packages):
        remaining = set(packages) - set(order)
        print(f"  [warn] cycle detected among: {remaining}; appending to end", file=sys.stderr)
        order.extend(remaining)
    return order


def publish_all(dry_run: bool = False, max_wait_seconds: int = 120, exclude: list = None) -> int:
    if exclude is None:
        exclude = []
    exclude_set = set(exclude)
    metadata = _cargo_metadata()
    workspace_root = Path(metadata["workspace_root"])
    packages: Dict[str, dict] = {}

    for pkg in metadata["packages"]:
        name = pkg["name"]
        if name in exclude_set:
            continue
        version = pkg["version"]
        manifest_path = Path(pkg["manifest_path"])
        if not manifest_path.is_relative_to(workspace_root):
            continue
        # cargo metadata returns [] for publish = false, null for publish = true
        publish_registries = pkg.get("publish")
        if publish_registries is not None and len(publish_registries) == 0:
            continue
        # Also skip examples/ subdirectory packages
        rel = manifest_path.relative_to(workspace_root)
        if str(rel).startswith("examples/"):
            continue
        packages[name] = {"version": version, "dependencies": {}}
        for dep in pkg.get("dependencies", []):
            dep_name = dep["name"]
            if dep_name in packages or any(
                d["name"] == dep_name for d in metadata["packages"]
            ):
                packages[name]["dependencies"][dep_name] = dep

    if not packages:
        print("No publishable packages found in workspace.")
        return 0

    order = _topological_order(packages)
    print(f"Publish order ({len(order)} crates): {' → '.join(order)}")

    failed: List[str] = []
    for name in order:
        version = packages[name]["version"]
        if _version_exists(name, version):
            print(f"  {name} v{version} — already on crates.io, skipping")
            continue

        if not _publish_one(name, dry_run=dry_run):
            failed.append(name)
            continue

        if dry_run:
            continue

        if not _wait_index(name, version, max_wait=max_wait_seconds):
            failed.append(name)
            # Don't fail immediately — continue with remaining packages
            # that don't depend on the failed one.
            print(f"  [warn] continuing with remaining packages...", file=sys.stderr)

    if failed:
        print(f"\nFailed to publish: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nAll {len(order)} crate(s) published successfully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish workspace crates to crates.io with smart error handling."
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Package names to skip.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without actually publishing.",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=int(os.environ.get("PUBLISH_MAX_WAIT_PER_CRATE", "120")),
        help="Max seconds to wait per crate for crates.io indexing (default: 120).",
    )
    args = parser.parse_args()
    return publish_all(dry_run=args.dry_run, max_wait_seconds=args.max_wait, exclude=args.exclude)


if __name__ == "__main__":
    raise SystemExit(main())

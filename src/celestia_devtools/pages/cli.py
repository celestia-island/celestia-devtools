"""``celestia-devtools pages`` — build, verify and publish a custom page.

    # 1. build one artifact (offline; the raw lane needs nothing at all)
    celestia-devtools pages build --lane raw-html --title "demo" --out /tmp/page

    # 2. check the artifact against the server's matrix and the contract mirror
    celestia-devtools pages verify --artifact /tmp/page --chest-root ../shittim-chest

    # 3. publish it and print the version-pinned tab reference
    CELESTIA_PAGES_TOKEN=… celestia-devtools pages publish --artifact /tmp/page --base https://demo.dev.celestia.world

``publish`` prints one JSON object (``page_id``, ``content_hash``, ``entry``,
``tab_ref``, ``url``) so a script — or an e2e run — can consume the result
without re-deriving the hash.
"""

from __future__ import annotations
import argparse
import json
import re
import pathlib
import sys
from . import builder, client, contract

#: Where sibling checkouts live when a lane needs one. ``None`` means "ask for it".
DEFAULT_SIBLINGS = {"hikari": "../hikari", "tairitsu": "../tairitsu", "chest": "../shittim-chest"}

def _load_artifact(directory: pathlib.Path, entry: str) -> contract.validated_artifact:
    files = contract.collect(directory, entry = entry)
    return contract.validate_files(files, entry = entry)

#: A JWT is three dot-separated base64url runs; refusing the shape here keeps a
#: bearer out of `ps auxww` / shell history / CI logs even by accident.
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")

def _resolve_token(token_file: str | None) -> tuple[str | None, str | None]:
    """Pick up the bearer without ever taking one on the command line.

    Order: ``--token-file``/``$CELESTIA_PAGES_TOKEN`` (which may itself name a
    file), else nothing — the client then reads the same environment variable, so
    a caller that exports it needs no flag at all.
    """

    if token_file is None:
        return client.token_from_env(), None

    looks_like_token = bool(_JWT_SHAPE.match(token_file)) or (len(token_file) > 200)
    candidate = pathlib.Path(token_file)

    if looks_like_token and not candidate.exists():
        return None, "--token-file takes a PATH; a token pasted on the command line would leak through ps/history — write it to a 0600 file instead"

    if not candidate.is_file():
        return None, f"--token-file {token_file}: not a readable file"

    return client.read_token_file(candidate), None

def _cmd_build(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out)
    lane = args.lane

    if lane == "raw-html":
        built = builder.build_raw(out, args.title)
        scopes: list[str] = []
    elif lane == "built-hikari":
        toolchain = pathlib.Path(args.hikari_toolchain or (pathlib.Path(args.hikari_src) / "node_modules"))
        built = builder.build_hikari(out, args.title, toolchain, pathlib.Path(args.hikari_src))
        scopes = list(contract.CLOSED_RPC_SCOPES)
    else:
        toolchain = pathlib.Path(args.hikari_toolchain or (pathlib.Path(args.hikari_src) / "node_modules"))
        built = builder.build_tairitsu(out, args.title, pathlib.Path(args.tairitsu_src), toolchain)
        scopes = list(contract.CLOSED_RPC_SCOPES)

    artifact = _load_artifact(out, args.entry)
    manifest = builder._write_manifest(out, artifact, lane, args.title, builder.LANES[lane]["builder"], scopes, builder._read_lock_digest(out))
    print(json.dumps({"artifact": str(out), "lane": lane, "content_hash": artifact.content_hash, "files": len(artifact.files), "size_bytes": artifact.size_bytes, "built": built, "manifest": manifest["schema"]}, indent = 2))
    return 0

def _cmd_verify(args: argparse.Namespace) -> int:
    directory = pathlib.Path(args.artifact)
    manifest_path = directory / "page.json"
    entry = args.entry

    if manifest_path.is_file() and (entry is None):
        entry = json.loads(manifest_path.read_text(encoding = "utf-8")).get("entry", contract.DEFAULT_ENTRY)

    entry = entry or contract.DEFAULT_ENTRY
    artifact = _load_artifact(directory, entry)
    findings: list[str] = []

    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding = "utf-8"))

        if manifest.get("content_hash") != artifact.content_hash:
            findings.append(f"manifest content_hash {manifest.get('content_hash')} != recomputed {artifact.content_hash}")

        declared = {item["path"]: item["sha256"] for item in manifest.get("files", [])}
        actual = {item.path: item.sha256 for item in artifact.files}

        for path in sorted(set(declared) - set(actual)):
            findings.append(f"manifest lists a file that is not on disk: {path}")

        for path in sorted(set(actual) - set(declared)):
            findings.append(f"file on disk not in the manifest: {path}")

        for path in sorted(set(actual) & set(declared)):
            if actual[path] != declared[path]:
                findings.append(f"sha256 drifted for {path}")

        scopes = manifest.get("rpc_scopes", [])
        unknown = [scope for scope in scopes if scope not in contract.CLOSED_RPC_SCOPES]

        if unknown:
            findings.append(f"rpc_scopes outside the closed set {contract.CLOSED_RPC_SCOPES}: {unknown}")

        if (manifest.get("mode") == "raw-html") and scopes:
            findings.append("raw-html pages must declare no rpc scopes")
    else:
        findings.append("no page.json (run `pages build` first; publish recomputes anyway)")

    if args.chest_root:
        findings.extend(contract.check_contract_drift(pathlib.Path(args.chest_root)))

    report = {"artifact": str(directory), "entry": entry, "content_hash": artifact.content_hash, "size_bytes": artifact.size_bytes, "files": len(artifact.files), "findings": findings}
    print(json.dumps(report, indent = 2))
    return 0 if not findings else 1

def _cmd_publish(args: argparse.Namespace) -> int:
    directory = pathlib.Path(args.artifact)
    manifest = {}

    if (directory / "page.json").is_file():
        manifest = json.loads((directory / "page.json").read_text(encoding = "utf-8"))

    entry = args.entry or manifest.get("entry") or contract.DEFAULT_ENTRY
    mode = args.mode or manifest.get("mode")

    if mode not in contract.MODES:
        print(f"publish: unknown mode {mode!r} (from page.json or --mode); expected one of {contract.MODES}", file = sys.stderr)
        return 2

    artifact = _load_artifact(directory, entry)

    if artifact.size_bytes > client.PRACTICAL_ARTIFACT_CAP:
        print(f"publish: artifact is {artifact.size_bytes} bytes; the largest measured pass on the public faces is {client.PRACTICAL_ARTIFACT_CAP} bytes ({client.artifact_cap_label()}). P1 has no chunking, so this refuses locally instead of streaming bytes the front door will cut. Raise it with $CELESTIA_PAGES_BODY_CAP only if you have measured a larger body passing.", file = sys.stderr)
        return 2

    token, token_error = _resolve_token(args.token_file)

    if token_error:
        print(f"publish: {token_error}", file = sys.stderr)
        return 2

    pages = client.pages_client(args.base, token = token, workspace_id = args.workspace)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "mode": mode, "entry": entry, "content_hash": artifact.content_hash, "size_bytes": artifact.size_bytes, "files": len(artifact.files), "would_publish": f"{{page_id}}/{artifact.content_hash}/{entry}"}, indent = 2))
        return 0

    try:
        record = pages.publish_artifact(artifact, title = args.title or manifest.get("title") or directory.name, mode = mode, expires_at = args.expires_at, page_id = args.page_id)
    except client.publish_failed as failure:
        # The draft row exists and the id is the retry handle: print it as JSON so a
        # caller can `--page-id` the next attempt instead of leaking a quota row.
        print(json.dumps({"page_id": failure.page_id, "created": failure.created, "error": failure.message, "retry": ["--page-id", failure.page_id]}, indent = 2))
        print(f"publish: {failure.message}", file = sys.stderr)
        return 1

    print(json.dumps(record, indent = 2))
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools pages", description = "Build, verify and publish chest custom pages (P1).")
    sub = parser.add_subparsers(dest = "command", required = True)
    build = sub.add_parser("build", help = "build one artifact for a provenance lane")
    build.add_argument("--lane", required = True, choices = sorted(builder.LANES))
    build.add_argument("--title", required = True)
    build.add_argument("--out", required = True, help = "artifact directory to write")
    build.add_argument("--entry", default = contract.DEFAULT_ENTRY)
    build.add_argument("--hikari-src", default = DEFAULT_SIBLINGS["hikari"])
    build.add_argument("--hikari-toolchain", default = None, help = "node_modules tree with vite/vue/sass (default: <hikari-src>/node_modules)")
    build.add_argument("--tairitsu-src", default = DEFAULT_SIBLINGS["tairitsu"])
    build.set_defaults(handler = _cmd_build)
    verify = sub.add_parser("verify", help = "re-check an artifact against the server matrix")
    verify.add_argument("--artifact", required = True)
    verify.add_argument("--entry", default = None)
    verify.add_argument("--chest-root", default = DEFAULT_SIBLINGS["chest"], help = "chest checkout used for the contract drift pin")
    verify.set_defaults(handler = _cmd_verify)
    publish = sub.add_parser("publish", help = "upload + publish and print the version-pinned tab ref")
    publish.add_argument("--artifact", required = True)
    publish.add_argument("--base", required = True, help = "chest origin, e.g. https://demo.dev.celestia.world")
    publish.add_argument("--token-file", default = None, help = "file holding the bearer token, 0600 (default: $CELESTIA_PAGES_TOKEN, which may name a file)")
    publish.add_argument("--workspace", default = None, help = "workspace uuid to bind the session to")
    publish.add_argument("--title", default = None)
    publish.add_argument("--mode", default = None, choices = sorted(contract.MODES))
    publish.add_argument("--entry", default = None)
    publish.add_argument("--page-id", default = None, help = "re-upload an existing page instead of creating one")
    publish.add_argument("--expires-at", default = None, help = "RFC 3339 deadline (the sweep flips the row at it)")
    publish.add_argument("--dry-run", action = "store_true")
    publish.set_defaults(handler = _cmd_publish)
    args = parser.parse_args(argv)

    try:
        return int(args.handler(args))
    except (contract.contract_error, client.pages_error, RuntimeError, ValueError) as error:
        print(f"pages: {error}", file = sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

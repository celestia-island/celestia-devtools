#!/usr/bin/env python3
"""Bundle the five org protocol/legal docs from docs.celestia.world.

The WebUI login/register surfaces (shittim-chest, erp.celestia.world, …) show
the same five agreements — license, CLA, code of conduct, security policy and
contributing guide — with a per-document language selector.  The canonical,
fully internationalized copies live in the docs.celestia.world repo
(``docs/<lang>/meta/*.md``, 11 languages), so this command vendors them into a
frontend repo's generated area as per-(type, lang) markdown assets plus a
tiny TypeScript manifest:

    <repo>/packages/webui/.generated/protocols/
        vendor/<type>.<lang>.md   one markdown file per available (type, lang)
        meta.ts                   type -> lang -> {file, lastRevised}

The frontend then pulls each vendor file lazily (``import.meta.glob`` + raw
import), so only the (type, lang) pair the user actually opens is fetched —
vite embeds the rest as on-demand vendor chunks.  No backend endpoint, no
auth: the content is public static legal text.

docs.celestia.world checkout resolution (cheap first, mirroring ``locate``):

    1. ``$DOCS_CELESTIA_WORLD_ROOT`` (explicit override)
    2. sibling ``../docs.celestia.world`` (the org dev layout)
    3. shallow ``git clone`` into ``<repo_root>/target/docs-celestia-shared``
       (``target/`` is gitignored everywhere, so CI self-pulls with no
       extra ignore rules)

``lastRevised`` is the git committer date of each source file inside the docs
checkout (best effort; ``null`` when git metadata is unavailable).

Usage::

    celestia-devtools protocol-bundle [--repo-root PATH] [--out PATH]
                                      [--docs-root PATH] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ORG_GIT_BASE = "https://github.com/celestia-island"
DOCS_REPO = "docs.celestia.world"
DOCS_GIT_URL = f"{ORG_GIT_BASE}/{DOCS_REPO}.git"
DOCS_ENV_VAR = "DOCS_CELESTIA_WORLD_ROOT"
DOCS_MARKER = "justfile"  # any root file of the docs checkout; justfile is stable

# Preferred language order — matches the frontend SUPPORTED_LOCALES lists.
ALL_LANGS = ["en", "zh-Hans", "zh-Hant", "ja", "ko", "de", "fr", "es", "pt", "ar", "ru"]

# (protocol type) -> (file under docs/<lang>/meta/, relative to the docs root).
TYPE_FILES: dict[str, str] = {
    "license": "meta/license.md",
    "cla": "meta/cla.md",
    "code-of-conduct": "meta/code-of-conduct.md",
    "security": "meta/security.md",
    "contributing": "meta/CONTRIBUTING.md",
}

# Clone destination lives inside the repo's own gitignored ``target/`` dir
# (same convention as repo/locate.py). Override via $CELESTIA_DEV_TARGET_DIR.
TARGET_DIR = os.environ.get("CELESTIA_DEV_TARGET_DIR", "target")
CLONE_SUBDIR = "docs-celestia-shared"


def _stderr(level: str, msg: str) -> None:
    print(f"[protocol-bundle] {level}: {msg}", file=sys.stderr)


def _docs_ok(cand: Path) -> bool:
    """A docs checkout must have at least one language's meta/ directory."""
    return bool(cand and (cand / DOCS_MARKER).exists() and (cand / "docs").is_dir())


def resolve_docs_root(repo_root: Path, docs_root: Path | None = None) -> Path | None:
    """Resolve the docs.celestia.world checkout (env → sibling → clone)."""
    if docs_root is not None:
        c = Path(docs_root).expanduser()
        if _docs_ok(c):
            return c.resolve()
        _stderr("warn", f"explicit --docs-root {c} is not a docs checkout")
        return None

    # 1. explicit override
    for var in (DOCS_ENV_VAR, "DOCS_CELESTIA_WORLD_REPO"):
        if env := os.environ.get(var):
            c = Path(env).expanduser()
            if _docs_ok(c):
                return c.resolve()
            _stderr("warn", f"{var}={c} is not a docs checkout")

    # 2. sibling layout (org dev layout: /mnt/codespace/<repo> siblings)
    sib = repo_root.parent / DOCS_REPO
    if _docs_ok(sib):
        return sib.resolve()

    # 3. last resort: shallow clone into the repo's gitignored target/ dir
    clone = repo_root / TARGET_DIR / CLONE_SUBDIR
    if not _docs_ok(clone):
        try:
            clone.parent.mkdir(parents=True, exist_ok=True)
            _stderr("warn", f"{DOCS_REPO} not found locally — cloning into {clone}")
            subprocess.run(
                ["git", "clone", "--depth", "1", DOCS_GIT_URL, str(clone)],
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _stderr("error", f"git clone of {DOCS_REPO} failed: {exc}")
    if _docs_ok(clone):
        return clone.resolve()
    return None


def _git_last_modified(docs_root: Path, rel: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", rel],
            cwd=docs_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    date = out.stdout.strip()
    return date or None


def collect_languages(docs_root: Path) -> list[str]:
    """Return the docs languages that carry a meta/ dir, in ALL_LANGS order."""
    present: set[str] = set()
    meta_root = docs_root / "docs"
    for cand in meta_root.iterdir():
        if cand.is_dir() and (cand / "meta").is_dir():
            present.add(cand.name)
    ordered = [lang for lang in ALL_LANGS if lang in present]
    for extra in sorted(present - set(ALL_LANGS)):
        ordered.append(extra)
    return ordered


def generate(
    repo_root: Path,
    docs_root: Path,
    out_dir: Path | None = None,
    verbose: bool = False,
) -> int:
    """Vendor the five protocol docs into packages/webui/.generated/protocols.

    Returns the number of vendored documents (0 is an error state — the
    caller should treat it as failure).
    """
    out_dir = out_dir or repo_root / "packages" / "webui" / ".generated" / "protocols"
    vendor_dir = out_dir / "vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, dict[str, dict]] = {}
    doc_count = 0

    for doc_type, rel_suffix in TYPE_FILES.items():
        docs: dict[str, dict] = {}
        for lang in collect_languages(docs_root):
            rel = f"docs/{lang}/{rel_suffix}"
            src = docs_root / rel
            if not src.is_file():
                continue
            content = src.read_text(encoding="utf-8")
            file_name = f"{doc_type}.{lang}.md"
            (vendor_dir / file_name).write_text(content, encoding="utf-8")
            docs[lang] = {
                "file": file_name,
                "lastRevised": _git_last_modified(docs_root, rel),
            }
            doc_count += 1
            if verbose:
                revised = docs[lang]["lastRevised"]
                print(f"  {doc_type}/{lang} <- {rel}"
                      + (f"  ({revised[:10]})" if revised else ""))
        if docs:
            meta[doc_type] = docs

    if doc_count == 0:
        _stderr("error", f"no protocol docs found under {docs_root / 'docs'}")
        return 0

    header = (
        "// AUTO-GENERATED by celestia-devtools protocol-bundle — do not edit\n"
        f"// Source: {DOCS_REPO} docs/<lang>/meta/"
        "{license,cla,code-of-conduct,security,CONTRIBUTING}.md\n"
        "// Vendor markdown lives in ./vendor/<type>.<lang>.md and is pulled\n"
        "// lazily by the frontend via import.meta.glob.\n\n"
    )
    parts = [
        header,
        "export interface ProtocolVendorMeta {\n",
        "  file: string;\n",
        "  lastRevised: string | null;\n",
        "}\n\n",
        "export const PROTOCOL_VENDOR_META: Record<string, Record<string, ProtocolVendorMeta>> = ",
        json.dumps(meta, ensure_ascii=False, indent=2),
        ";\n",
    ]
    (out_dir / "meta.ts").write_text("".join(parts), encoding="utf-8")
    rel_out = os.path.relpath(out_dir, repo_root)
    print(f"Done: {doc_count} protocol docs ({len(meta)} types) -> "
          f"{rel_out}/ (vendor/*.md + meta.ts)")
    return doc_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bundle the five org protocol docs from docs.celestia.world "
                    "into packages/webui/.generated/protocols as lazy vendor assets"
    )
    parser.add_argument("--repo-root", default=None,
                        help="caller repo root (default: cwd)")
    parser.add_argument("--out", default=None,
                        help="output directory (default: "
                             "<repo>/packages/webui/.generated/protocols)")
    parser.add_argument("--docs-root", default=None,
                        help="explicit docs.celestia.world checkout "
                             "(default: $DOCS_CELESTIA_WORLD_ROOT → sibling → clone)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    docs_root = resolve_docs_root(repo_root, Path(args.docs_root) if args.docs_root else None)
    if docs_root is None:
        _stderr("error", f"could not locate {DOCS_REPO}; set {DOCS_ENV_VAR}")
        return 127

    count = generate(
        repo_root,
        docs_root,
        Path(args.out).resolve() if args.out else None,
        verbose=args.verbose,
    )
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())

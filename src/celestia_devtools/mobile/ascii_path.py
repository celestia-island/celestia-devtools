"""ASCII-path infrastructure for mobile builds.

Why this module exists
======================

The Android/NDK toolchain family (rustc→lld, AGP, aapt2) does not reliably
accept non-ASCII filesystem paths — the canonical break is NDK's lld
receiving GBK-encoded bytes for a checkout under a non-English Windows user
directory (``D:\\源代码\\…`` → ``cannot find version script`` / ``cannot
open …rcgu.o``). wowsp carries a two-part fix (``android.overridePathCheck``
plus an ASCII ``CARGO_TARGET_DIR``); this module is the shared
implementation of the second half plus the general audit, so every mobile
consumer (wowsp, shittim-chest tauri_mobile, harmony) gets the same
guarantees from one place.

The compat contract with wowsp's ``scripts/android_cargo_target.py`` is
the OVERRIDE, not the fallback order: this resolver honors
``WOWSP_ANDROID_CARGO_TARGET`` first (before the celestia alias), and the
wowsp adapter exports that variable with OUR resolved path — so whenever
this facility drives a wowsp build, both resolvers converge on one
directory regardless of their differing fallback names.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class AsciiPathError(RuntimeError):
    """A path that must be ASCII is not — carries the operator remedy."""


def is_ascii(text: str) -> bool:
    """True when every character is ASCII (the toolchain-safe alphabet)."""
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _windows_localappdata() -> Path | None:
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw) if raw else None


def _windows_temp() -> Path | None:
    raw = os.environ.get("TEMP") or os.environ.get("TMP")
    return Path(raw) if raw else None


def ascii_cargo_target_candidates() -> list[Path]:
    """Candidate ASCII CARGO_TARGET_DIR roots, in the wowsp-compatible
    priority order (env override first — the resolver's contract is that
    the FIRST creatable pure-ASCII candidate wins)."""
    out: list[Path] = []
    override = (os.environ.get("WOWSP_ANDROID_CARGO_TARGET") or "").strip() or (
        os.environ.get("CELESTIA_MOBILE_CARGO_TARGET") or ""
    ).strip()
    if override:
        out.append(Path(override))
    localappdata = _windows_localappdata()
    if localappdata is not None:
        out.append(localappdata / "celestia" / "mobile-target")
        out.append(localappdata / "wowsp" / "android-target")
    temp = _windows_temp()
    if temp is not None:
        out.append(temp / "celestia-mobile-target")
    if os.name == "nt":
        out.append(Path("C:/Users/Public/celestia-mobile-target"))
    else:
        out.append(Path(tempfile.gettempdir()) / "celestia-mobile-target")
    return out


def ensure_ascii_dir(path: Path, purpose: str) -> Path:
    """Create ``path`` and verify it is ASCII.

    Raises :class:`AsciiPathError` with an operator-usable message when the
    path contains non-ASCII characters — the caller must NOT fall back to
    an unverified location silently.
    """
    text = str(path)
    if not is_ascii(text):
        raise AsciiPathError(
            f"the {purpose} directory must be ASCII-only, got {text!r} — "
            "set WOWSP_ANDROID_CARGO_TARGET (or CELESTIA_MOBILE_CARGO_TARGET) "
            "to an ASCII path; the NDK linker cannot accept encoded bytes "
            "from non-ASCII paths"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_ascii_cargo_target() -> Path:
    """First creatable ASCII candidate from :func:`ascii_cargo_target_candidates`."""
    last_error: Exception | None = None
    for candidate in ascii_cargo_target_candidates():
        try:
            return ensure_ascii_dir(candidate, "mobile CARGO_TARGET_DIR")
        except (AsciiPathError, OSError) as exc:
            # Non-ASCII or uncreatable (stale override, read-only parent):
            # fall through to the next candidate — never abort while
            # alternatives remain (wowsp's resolver does the same).
            last_error = exc
    raise AsciiPathError(
        f"no creatable ASCII CARGO_TARGET_DIR candidate — last failure: {last_error}"
    )


def audit_ascii(paths: dict[str, Path | str | None]) -> list[str]:
    """Audit named paths for ASCII compliance; returns problem lines.

    Callers (``mobile doctor``, the build adapters) feed every path the
    toolchain will see — the repo, toolchain homes, target dirs, the
    generated ``local.properties`` directory — and surface the returned
    lines verbatim. An empty list means the build is path-safe.
    """
    problems: list[str] = []
    for name, value in paths.items():
        if value is None:
            continue
        text = str(value)
        if not is_ascii(text):
            problems.append(
                f"{name}: non-ASCII path {text!r} — the NDK/gradle toolchain "
                "may fail or silently misbehave; relocate or junction it to "
                "an ASCII path"
            )
    return problems

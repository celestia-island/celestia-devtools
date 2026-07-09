#!/usr/bin/env python3
"""Commit-message linter enforcing the celestia-island org gitmoji convention.

---

Rules (applied to the subject line — first line of the commit message):

1.  Must start with a gitmoji from gitmoji.dev.
2.  Must NOT use Conventional Commits prefixes (``feat:``, ``fix:``, etc.) —
    the emoji **is** the type marker.
3.  First letter after the emoji must be uppercase (``[A-Z]``).
4.  Must NOT start with a bare version number or filler phrase (``v1.2.3``,
    ``bump version``, ``update to``, etc.).
5.  Must end with a period (``.``).
6.  Must be English-only (no CJK characters or wide punctuation).

Exemptions (checked before rules):
-  ``Merge ...`` / ``Merge branch ...`` (git default).
-  ``Revert ...`` (git revert).

The ``lint()`` function returns a list of violation strings; an empty list means
the message passes all rules.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import List

# ── gitmoji.dev canonical emoji set ───────────────────────────────────────────
# Sourced from https://gitmoji.dev.  Each entry is the raw emoji character
# (including variation selectors and ZWJ sequences where applicable).  The
# lookup is `subject.startswith(emoji)`, so multi-codepoint emoji work
# correctly without decoding.
GITMOJI_WHITELIST: frozenset[str] = frozenset([
    "\U0001f3a8",    # ðŸŽ¨ :art:
    "\u26a1\ufe0f",  # âš¡ï¸ :zap:
    "⚡",        # ⚡ :zap (without variation selector)
    "\U0001f525",    # ðŸ”¥ :fire:
    "\U0001f41b",    # ðŸ› :bug:
    "\U0001f691",    # ðŸš‘ :ambulance:
    "\u2728",        # âœ¨ :sparkles:
    "\U0001f4dd",    # ðŸ“ :memo:
    "\U0001f680",    # ðŸš€ :rocket:
    "\U0001f484",    # ðŸ’„ :lipstick:
    "\U0001f389",    # ðŸŽ‰ :tada:
    "\u2705",        # âœ… :white_check_mark:
    "\U0001f512",    # ðŸ”’ :lock:
    "\U0001f516",    # ðŸ”– :bookmark:
    "🔗",    # 🔗 :link (org: symlink/copilot)
    "\U0001f6a8",    # ðŸš¨ :rotating_light:
    "\U0001f6a7",    # ðŸš§ :construction:
    "\U0001f49a",    # ðŸ’š :green_heart:
    "\u2b07\ufe0f",  # â¬‡ï¸ :arrow_down:
    "\u2b06\ufe0f",  # â¬†ï¸ :arrow_up:
    "\U0001f4cc",    # ðŸ“Œ :pushpin:
    "\U0001f477",    # ðŸ‘· :construction_worker:
    "\U0001f4c8",    # ðŸ“ˆ :chart_with_upwards_trend:
    "\u267b\ufe0f",  # â™»ï¸ :recycle:
    "\u2795",        # âž• :heavy_plus_sign:
    "\u2796",        # âž– :heavy_minus_sign:
    "\U0001f527",    # ðŸ”§ :wrench:
    "\U0001f528",    # ðŸ”¨ :hammer:
    "\U0001f310",    # ðŸŒ :globe_with_meridians:
    "\u270f\ufe0f",  # âœï¸ :pencil2:
    "\U0001f4a9",    # ðŸ’© :poop:
    "\u23ea",        # âª :rewind:
    "\U0001f500",    # ðŸ”€ :twisted_rightwards_arrows:
    "🔄",    # 🔄 :counterclockwise_arrows_button (org: sync/refresh)
    "\U0001f4e6",    # ðŸ“¦ :package:
    "\U0001f47d",    # ðŸ‘½ :alien:
    "\U0001f69a",    # ðŸšš :truck:
    "\U0001f4c4",    # ðŸ“„ :page_facing_up:
    "\U0001f4a5",    # ðŸ’¥ :boom:
    "\U0001f371",    # ðŸ± :bento:
    "\u267f\ufe0f",  # â™¿ï¸ :wheelchair:
    "\U0001f4a1",    # ðŸ’¡ :bulb:
    "\U0001f37b",    # ðŸ» :beers:
    "\U0001f4ac",    # ðŸ’¬ :speech_balloon:
    "\U0001f5c3\ufe0f",  # ðŸ—ƒï¸ :card_file_box:
    "\U0001f50a",    # ðŸ”Š :loud_sound:
    "\U0001f507",    # ðŸ”‡ :mute:
    "\U0001f465",    # ðŸ‘¥ :busts_in_silhouette:
    "\U0001f6b8",    # ðŸš¸ :children_crossing:
    "\U0001f3d7\ufe0f",  # ðŸ—ï¸ :building_construction:
    "\U0001f4f1",    # ðŸ“± :iphone:
    "\U0001f921",    # ðŸ¤¡ :clown_face:
    "\U0001f95a",    # ðŸ¥š :egg:
    "\U0001f648",    # ðŸ™ˆ :see_no_evil:
    "\U0001f4f8",    # ðŸ“¸ :camera_flash:
    "\u2697\ufe0f",  # âš—ï¸ :alembic:
    "\U0001f50d",    # ðŸ” :mag:
    "\U0001f3f7\ufe0f",  # ðŸ·ï¸ :label:
    "\U0001f331",    # ðŸŒ± :seedling:
    "\U0001f6a9",    # ðŸš© :triangular_flag_on_post:
    "\U0001f945",    # ðŸ¥… :goal_net:
    "\U0001f4ab",    # ðŸ’« :dizzy:
    "\U0001f5d1\ufe0f",  # ðŸ—‘ï¸ :wastebasket:
    "\U0001f6c2",    # ðŸ›‚ :passport_control:
    "\U0001fa79",    # ðŸ©¹ :adhesive_bandage:
    "\U0001f9d0",    # ðŸ§ :monocle_face:
    "\u26b0\ufe0f",  # âš°ï¸ :coffin:
    "\U0001f9ea",    # ðŸ§ª :test_tube:
    "\U0001f454",    # ðŸ‘” :necktie:
    "\U0001fa7a",    # ðŸ©º :stethoscope:
    "\U0001f9f1",    # ðŸ§± :bricks:
    "\U0001f9d1\u200d\U0001f4bb",  # ðŸ§‘â€ðŸ’» :technologist:
    "\U0001f4b8",    # ðŸ’¸ :money_with_wings:
    "\U0001f9f5",    # ðŸ§µ :thread:
    "\U0001f9ba",    # ðŸ¦º :safety_vest:
    "📜",    # 📜 :scroll (org: license)
])

# ── Regexes (compiled once) ──────────────────────────────────────────────────

# Conventional Commits prefix: feat, fix, chore, docs, style, refactor, perf,
# test, build, ci, revert, net, init — with optional scope parens.
_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(?:feat|fix|chore|docs|style|refactor|perf|test|build|ci|revert|net|init)"
    r"(?:\([^)]*\))?\s*[:!]",
)

# Leading bare version number or filler phrases (after gitmoji and space).
_VERSION_OR_FILLER_START_RE = re.compile(
    r"^(?:v?\d+\.\d+(?:\.\d+)*|bump\s|update\sto\b|upgrade\sto\b|downgrade\s)",
    re.IGNORECASE,
)

# CJK and full-width characters (Chinese, Japanese, Korean, etc.).
_CJK_RE = re.compile(r"[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3200-\u32ff"
                     r"\u3300-\u33ff\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]")

# First English letter position (used for capital-letter check).
_FIRST_ALPHA_RE = re.compile(r"[A-Za-z]")

# Trailing period on the subject line.
_ENDS_WITH_PERIOD_RE = re.compile(r"\.\s*$")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _first_line(text: str) -> str:
    """Return the subject line (up to first newline, stripped)."""
    return text.split("\n", 1)[0].rstrip()


def _has_any_gitmoji_prefix(subject: str) -> bool:
    """Check whether *subject* starts with any emoji in the whitelist."""
    for emoji in GITMOJI_WHITELIST:
        if subject.startswith(emoji):
            return True
    return False


def _trim_gitmoji(subject: str) -> str:
    """Strip the leading gitmoji and any whitespace from the subject."""
    for emoji in GITMOJI_WHITELIST:
        if subject.startswith(emoji):
            return subject[len(emoji):].lstrip()
    return subject


# ── Public API ───────────────────────────────────────────────────────────────

def lint(subject: str) -> List[str]:
    """Validate a single commit-message subject line.

    Returns a list of violation descriptions.  An empty list means the message
    passes all rules.  The check is designed to be safe to call from hooks and
    CI — it never raises.
    """
    violations: List[str] = []

    # Treat empty or whitespace-only subjects as a hard violation.
    if not subject or not subject.strip():
        violations.append("commit message is empty")
        return violations

    # Exemptions: merge and revert commits.
    if subject.startswith("Merge ") or subject.startswith("Revert "):
        return []

    # Rule 1 — gitmoji prefix.
    if not _has_any_gitmoji_prefix(subject):
        violations.append(
            "must start with a gitmoji (https://gitmoji.dev); "
            "e.g. '🐛 Fix the parser crash.'"
        )
        return violations  # remaining rules depend on correct prefix

    # Text after the gitmoji.
    tail = _trim_gitmoji(subject)

    # Rule 2 — no Conventional Commits prefix.
    if _CONVENTIONAL_PREFIX_RE.match(tail):
        violations.append(
            "must NOT use a Conventional Commits prefix (feat:/fix:/chore:/etc.); "
            "the emoji is the type marker"
        )

    # Rule 3 — first letter after emoji must be uppercase.
    alpha = _FIRST_ALPHA_RE.search(tail)
    if alpha is not None:
        if not alpha.group()[0].isupper():
            violations.append(
                "first letter after the emoji must be uppercase; "
                f"found lowercase '{alpha.group()}'"
            )
    else:
        violations.append("summary must contain at least one English letter")

    # Rule 4 — no version-number / filler start.
    if _VERSION_OR_FILLER_START_RE.match(tail):
        violations.append(
            "must NOT start with a bare version number or filler phrase "
            "(e.g. '0.3', 'Bump version', 'Update to'); "
            "describe the change instead"
        )

    # Rule 5 — trailing period.
    # 🔄 sync/refresh messages often carry timestamp metadata in parens, so
    # the period requirement is relaxed for them.
    if subject.startswith("\U0001f504"):
        pass  # 🔄 — exempt from period rule
    elif not _ENDS_WITH_PERIOD_RE.search(subject):
        violations.append("must end with a period (.)")

    # Rule 6 — English-only (no CJK).
    cjk = _CJK_RE.search(subject)
    if cjk is not None:
        violations.append(
            f"must be English-only; found CJK character "
            f"'{cjk.group()}' at position {cjk.start()}"
        )

    return violations


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point for ``celestia-devtools commit-msg-lint``."""
    parser = argparse.ArgumentParser(
        prog="celestia-devtools commit-msg-lint",
        description="Validate commit messages against the org gitmoji convention.",
    )
    sub = parser.add_subparsers(dest="subcmd")

    # check <file>
    p_check = sub.add_parser("check", help="validate a commit message")
    p_check.add_argument(
        "file", nargs="?", default=None,
        help="path to the commit message file (as passed by git to the hook; $1)",
    )
    p_check.add_argument(
        "--subject", default=None,
        help="validate a literal subject string instead of a file",
    )
    p_check.add_argument(
        "--stdin-subjects", action="store_true",
        help="read subjects from stdin (one per line) for batch CI validation",
    )

    args = parser.parse_args()

    if not args.subcmd:
        parser.print_help()
        return 2

    subjects: List[str] = []

    if args.subject is not None:
        subjects.append(args.subject)
    elif args.stdin_subjects:
        raw = sys.stdin.read()
        subjects = [line.strip() for line in raw.splitlines() if line.strip()]
    elif args.file is not None:
        try:
            raw = open(args.file, encoding="utf-8").read()
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
        subjects.append(_first_line(raw))
    else:
        print("error: either FILE, --subject, or --stdin-subjects required",
              file=sys.stderr)
        return 2

    errors: List[str] = []
    for subject in subjects:
        violations = lint(subject)
        if violations:
            errors.append(subject)
            for v in violations:
                errors.append(f"  - {v}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(
            "\nCommit message format:  <gitmoji> <Capitalized English summary.>\n"
            "Examples:\n"
            "  ✅ 🐛 Fix the parser crash.\n"
            "  ✅ ✨ Add distributed tracing support.\n"
            "  ❌ fix: broken thing\n"
            "  ❌ 🐛 修复解析器\n",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

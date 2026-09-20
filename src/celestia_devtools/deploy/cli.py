#!/usr/bin/env python3
"""`celestia-devtools deploy …` — production-target lifecycle dispatch.

This slice wires `deploy doctor` only; the remaining subcommands land with
their own slices and must fail loudly (exit 2) until then — a silent stub that
pretends success would be worse than an honest "not yet".

Planned surface (see PLAN §2.4 / _reports/deploy-phase0-plan-2026-09-20.md):
    bootstrap (bare `deploy`, wizard) · verify · backup · restore · upgrade ·
    rollback · status · artifact · secrets · uninstall
"""

from __future__ import annotations

import sys

PLANNED = (
    "bootstrap (bare `deploy`, interactive wizard)  — slice D2",
    "verify                                          — slice D4",
    "backup / restore                                — slice D4",
    "upgrade / rollback                              — slice D4",
    "status (入驻判据 / 版本 / 迁移水位 / 台账)       — slice D2/D4",
    "artifact build|publish|index                    — slice D3",
    "secrets rotate|show-meta                        — slice D2",
    "uninstall                                       — slice D4",
)


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: celestia-devtools deploy <subcommand> [options]")
        print()
        print("  doctor    pre-flight report: proxy / python deps / system tools")
        print()
        print("planned in later slices:")
        for line in PLANNED:
            print("  " + line)
        print()
        print("The bare `deploy` wizard (one-command install with TUI) lands in slice D2;")
        print("use `deploy doctor` to check a target box in the meantime.")
        return 2 if not argv else 0

    sub, rest = argv[0], argv[1:]
    if sub == "doctor":
        from celestia_devtools.deploy import doctor
        sys.argv = ["deploy-doctor", *rest]
        return int(doctor.main() or 0)

    print("error: 'deploy {}' is not implemented yet".format(sub), file=sys.stderr)
    print("planned subcommands:", file=sys.stderr)
    for line in PLANNED:
        print("  " + line, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

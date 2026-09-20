#!/usr/bin/env python3
"""`celestia-devtools deploy …` — production-target lifecycle dispatch.

Bare `deploy` is the one-command install: flags/env seed answers, the wizard
asks only what is missing (questionary when a TTY, plain numbered prompts
otherwise), the profile lands at /etc/celestia/<face>.profile.toml (0600, no
secrets), and the bootstrap stage machine runs. Stages from later slices stop
the run loudly (exit 2) with the pending-slice list — never a fake success.

Subcommands: `doctor` (read-only report). The rest land with their own slices
and fail loudly (exit 2) until then.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from celestia_devtools.deploy import bootstrap, profile as profile_mod, wizard

PLANNED = (
    "verify / backup / restore / upgrade / rollback — slice D4",
    "status (入驻判据 / 版本 / 迁移水位 / 台账)       — slice D4",
    "artifact build|publish|index                    — slice D3",
    "secrets rotate|show-meta                        — slice D4",
    "uninstall                                       — slice D4",
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="celestia-devtools deploy",
        description="One-command production install (wizard) for a single-host target.")
    ap.add_argument("--profile", help="path to <face>.profile.toml (skips the wizard)")
    ap.add_argument("--admin-email", help="initial admin email (seed; prompts for the rest)")
    ap.add_argument("--domain", help="public domain for the front/TLS stage (seed)")
    ap.add_argument("--level", choices=("hosted", "selfhosted"), help="profile template preset")
    ap.add_argument("--yes", action="store_true", help="skip the final confirmation")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--non-interactive", action="store_true",
                    help="never prompt; missing answers are a hard error")
    ap.add_argument("--json", action="store_true", help="machine-readable stage results")
    return ap


def _seed_from_args(args, parsed_profile: profile_mod.DeployProfile | None) -> dict[str, str]:
    seed: dict[str, str] = {}
    if parsed_profile is not None:
        p = parsed_profile
        seed.update({
            "host.face": p.host.face,
            "host.level": p.host.level,
            "host.listen": str(p.host.listen),
            "front.domain": p.front.domain,
            "admin.email": p.admin.email,
            "admin.password_to_file": "y" if p.admin.password_to_file else "n",
            "artifact.channel": p.artifact.channel,
        })
    if args.level:
        seed["host.level"] = args.level
    if args.admin_email:
        seed["admin.email"] = args.admin_email
    if args.domain:
        seed["front.domain"] = args.domain
    return seed


def run_bootstrap(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    parsed_profile = None
    if args.profile:
        try:
            parsed_profile = profile_mod.DeployProfile.read(Path(args.profile))
        except (OSError, profile_mod.ProfileError) as exc:
            print("error: --profile: {}".format(exc), file=sys.stderr)
            return 2

    seed = _seed_from_args(args, parsed_profile)
    interactive = (not args.non_interactive) and sys.stdin is not None and sys.stdin.isatty()

    try:
        answers, provenance = wizard.collect(seed, interactive=interactive)
        prof = wizard.build_profile(answers, base=parsed_profile)
    except wizard.WizardAbort as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    def _note(line: str) -> None:
        print(line, file=sys.stderr if args.json else sys.stdout)

    for key in sorted(provenance):
        _note("  {:<24} = {:<28} {}".format(key, answers[key], provenance[key]))
    if not args.yes and interactive and not args.dry_run:
        try:
            ok = input("\n确认执行以上配置？[y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            print("declined; nothing was changed", file=sys.stderr)
            return 2

    # dry-run changes nothing — the profile write itself is part of the plan
    if args.dry_run:
        _note("dry-run: would write {}".format(prof.host.profile_path()))
    else:
        prof.write(prof.host.profile_path())
    real_stdout = sys.stdout
    if args.json:
        # the summary block goes to stderr so stdout stays pure JSON
        sys.stdout = sys.stderr
    try:
        ctx = bootstrap.StageContext(profile=prof, assume_root=False,
                                     dry_run=args.dry_run)
        code, results = bootstrap.run_stages(ctx)
    finally:
        sys.stdout = real_stdout
    if args.json:
        import json
        print(json.dumps({"exit": code, "stages": [
            {"name": r.name, "status": r.status, "detail": r.detail,
             "changed": r.changed} for r in results]}, ensure_ascii=False, indent=2))
    return code


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        _build_parser().print_help()
        print("\nsubcommands: doctor (read-only report); planned: " + "; ".join(PLANNED))
        return 0
    if argv and argv[0] == "doctor":
        from celestia_devtools.deploy import doctor
        sys.argv = ["deploy-doctor", *argv[1:]]
        return int(doctor.main() or 0)
    if argv and not argv[0].startswith("-"):
        # a subcommand we do not implement yet
        print("error: 'deploy {}' is not implemented yet".format(argv[0]), file=sys.stderr)
        for line in PLANNED:
            print("  " + line, file=sys.stderr)
        return 2
    return run_bootstrap(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

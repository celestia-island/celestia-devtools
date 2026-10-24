#!/usr/bin/env python3
"""The deploy wizard: one question table, two renderers, zero hand-rolled TUI.

Design (settled): `questionary` renders the interactive path when a TTY and
the import succeed; everything else degrades to a plain numbered ``input()``
loop over the *same* question table — the non-TTY path is the CI-tested path,
so the fallback cannot rot. No f-string-TUI, no ANSI art of our own.

Precedence in practice: a --profile seeds first, CLI flags override the
seed, and the wizard only *asks* for what is still missing — each answer
records where it came from. (No environment-variable seeding today.)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from celestia_devtools.deploy.profile import (admin_section, artifact_section, database_section, deploy_profile, front_section, host_section)
import re

try:
    import questionary
except ImportError:  # pragma: no cover - optional interactive UI dependency
    questionary = None  # type: ignore[assignment]

class wizard_abort(Exception):
    """User declined / non-interactive with missing answers."""

@dataclass
class question:
    key: str                      # "host.face" — section.attr
    prompt: str
    default: str = ""
    choices: tuple[str, ...] = ()  # single-choice question when non-empty
    boolean: bool = False          # y/n question
    required: bool = True
    validate: Callable[[str], str | None] = lambda v: None  # error or None

def _port_ok(value: str) -> str | None:
    try:
        port = int(value)
    except ValueError:
        return "port must be an integer"

    if not (1 <= port <= 65535):
        return "port out of range 1-65535"

    return None

def _slug_ok(value: str) -> str | None:
    return None if re.match(r"^[a-z][a-z0-9-]{0,30}$", value) else "lowercase slug required"

def _email_ok(value: str) -> str | None:
    if not value:
        return None  # optional questions may stay empty

    return None if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value) else "invalid email address"

QUESTION_TABLE: tuple[question, ...] = (question("host.face", "实例名（systemd unit 与目录都由它派生）", default = "chest", validate = _slug_ok), question("host.level", "档位（hosted=自家机器 / selfhosted=客户自持）", choices = ("hosted", "selfhosted"), default = "hosted"), question("host.listen", "应用监听端口（前门反代 443，别用 80）", default = "3000", validate = _port_ok), question("front.domain", "对外域名（TLS 只走 HTTP-01；留空 = 关闭前门段）", default = "", required = False), question("admin.email", "初始管理员邮箱（首用户自动提权；事后可改）", validate = _email_ok), question("admin.password_to_file", "初始口令输出（y=写 0600 文件 / n=仅打印一次）", boolean = True, default = "n"), question("artifact.channel", "制品通道", choices = ("stable", "internal"), default = "stable"))

# ── renderers ─────────────────────────────────────────────────────────

def _questionary_available() -> bool:
    return questionary is not None

def ask_questionary(q: question, seeded: str | None) -> tuple[str, str]:
    """Ask one question via questionary; returns (answer, provenance)."""

    if questionary is None:  # pragma: no cover - callers gate on _questionary_available
        raise wizard_abort("questionary is not installed")

    if seeded is not None:
        return seeded, "(seeded)"

    if q.boolean:
        ans = questionary.confirm(q.prompt, default = q.default == "y").ask()

        if ans is None:
            raise wizard_abort("cancelled at {!r}".format(q.key))

        return ("y" if ans else "n"), "(asked)"
    elif q.choices:
        try:
            idx = q.choices.index(q.default)
        except ValueError:
            idx = 0

        ans = questionary.select(q.prompt, choices = list(q.choices), default = q.choices[idx]).ask()
    else:
        ans = questionary.text(q.prompt, default = q.default).ask()

    if ans is None:  # Ctrl-C / Esc
        raise wizard_abort("cancelled at {!r}".format(q.key))

    return str(ans), "(asked)"

def ask_plain(q: question, seeded: str | None, input_fn = input, print_fn = print) -> tuple[str, str]:
    """The degraded renderer: numbered choices and plain prompts on stdin."""

    if seeded is not None:
        return seeded, "(seeded)"

    while True:
        if q.boolean:
            print_fn("{} [{}]: ".format(q.prompt, "Y/n" if q.default == "y" else "y/N"))
            raw = input_fn("> ").strip().lower()
            ans = q.default if not raw else ("y" if raw in ("y", "yes") else "n")
        elif q.choices:
            print_fn(q.prompt)

            for i, choice in enumerate(q.choices, 1):
                marker = "*" if choice == q.default else " "
                print_fn("  {} {}) {}".format(marker, i, choice))

            raw = input_fn("> ").strip()

            if not raw:
                ans = q.default
            elif raw.isdigit() and (1 <= int(raw) <= len(q.choices)):
                ans = q.choices[int(raw) - 1]
            elif raw in q.choices:
                ans = raw
            else:
                print_fn("  ✗ 选择 1-{} 或回车取默认".format(len(q.choices)))
                continue
        else:
            print_fn("{}{}".format(q.prompt, " [{}]".format(q.default)if q.default else ""))
            raw = input_fn("> ").strip()
            ans = raw if raw else q.default

        error = q.validate(ans)

        if q.required and not ans:
            print_fn("  ✗ 此项必填")
            continue

        if error:
            print_fn("  ✗ {}".format(error))
            continue

        return ans, "(asked)"

def collect(seed: dict[str, str], *, interactive: bool, input_fn = input, print_fn = print) -> tuple[dict[str, str], dict[str, str]]:
    """Run the table; returns (answers, provenance). In non-interactive mode a
    missing required answer aborts loudly instead of hanging on stdin."""
    answers: dict[str, str] = {}
    provenance: dict[str, str] = {}
    use_q = interactive and _questionary_available()

    for q in QUESTION_TABLE:
        seeded = seed.get(q.key)

        if use_q:
            ans, src = ask_questionary(q, seeded)
        elif seeded is not None:
            ans, src = seeded, "(seeded)"
        elif interactive:
            ans, src = ask_plain(q, seeded, input_fn = input_fn, print_fn = print_fn)
        else:
            if q.required and not q.default:
                raise wizard_abort("non-interactive and no answer for required {!r} " "(pass --admin-email / --profile)".format(q.key))

            ans, src = q.default, "(default)"

        if q.required and not ans:
            raise wizard_abort("empty answer for required {!r}".format(q.key))

        error = q.validate(ans)

        if error:
            raise wizard_abort("{}: {}".format(q.key, error))

        answers[q.key] = ans
        provenance[q.key] = src

    return answers, provenance

def build_profile(answers: dict[str, str], base: deploy_profile | None = None) -> deploy_profile:
    """Apply the table's answers onto `base` when given (a --profile carries
    every field, including ones the table never asks about — srv_base,
    etc_base, database.*, front.email — and those must ride through
    untouched), else construct a fresh profile from the answers alone."""

    if base is None:
        profile = deploy_profile(host = host_section(face = answers["host.face"], level = answers["host.level"], listen = int(answers["host.listen"])), artifact = artifact_section(channel = answers["artifact.channel"]), database = database_section(mode = "external"), front = front_section(enabled = bool(answers.get("front.domain")), domain = answers.get("front.domain", ""), email = answers.get("front.email", "")), admin = admin_section(email = answers["admin.email"], password_to_file = answers["admin.password_to_file"] == "y"))
    else:
        profile = base
        profile.host.face = answers["host.face"]
        profile.host.level = answers["host.level"]
        profile.host.listen = int(answers["host.listen"])
        profile.artifact.channel = answers["artifact.channel"]
        profile.front.domain = answers.get("front.domain", "")
        profile.front.enabled = bool(profile.front.domain)

        if answers.get("front.email"):
            profile.front.email = answers["front.email"]

        profile.admin.email = answers["admin.email"]
        profile.admin.password_to_file = answers["admin.password_to_file"] == "y"

    errors = profile.validate()

    if errors:
        raise wizard_abort("; ".join(errors))

    return profile

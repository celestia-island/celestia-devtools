"""Tests for the deploy wizard: one table, two renderers, loud aborts."""

from __future__ import annotations

import pytest

from celestia_devtools.deploy import wizard


FULL_SEED = {
    "host.face": "chest",
    "host.level": "selfhosted",
    "host.listen": "3000",
    "front.domain": "acme.example",
    "admin.email": "ops@acme.example",
    "admin.password_to_file": "n",
    "artifact.channel": "stable",
}


class TestSeeded:
    def test_full_seed_never_touches_stdin(self):
        answers, prov = wizard.collect(FULL_SEED, interactive=False,
                                       input_fn=_boom_input)
        assert answers == FULL_SEED
        assert set(prov.values()) == {"(seeded)"}

    def test_build_profile_maps_answers(self):
        prof = wizard.build_profile(FULL_SEED)
        assert prof.host.face == "chest"
        assert prof.host.level == "selfhosted"
        assert prof.front.enabled and prof.front.domain == "acme.example"
        assert prof.admin.email == "ops@acme.example"
        assert prof.admin.password_to_file is False


class TestNonInteractive:
    def test_missing_required_aborts_with_pointer(self):
        with pytest.raises(wizard.WizardAbort, match="admin.email"):
            wizard.collect({}, interactive=False, input_fn=_boom_input)

    def test_partial_seed_aborts_on_the_gap(self):
        seed = dict(FULL_SEED)
        del seed["front.domain"]
        # front.domain is optional (empty = front disabled) → allowed
        answers, _ = wizard.collect(seed, interactive=False, input_fn=_boom_input)
        assert answers["front.domain"] == ""


class TestPlainRenderer:
    def _feed(self, *lines):
        it = iter(lines)
        return lambda _prompt="": next(it)

    def test_choice_by_number_and_validation_loop(self):
        q = wizard.Question("host.listen", "端口", default="3000",
                            validate=wizard._port_ok)
        ans, src = wizard.ask_plain(q, None,
                                    input_fn=self._feed("99999", "3001"),
                                    print_fn=lambda *a, **k: None)
        assert ans == "3001" and src == "(asked)"

    def test_choice_selects_by_number_and_bad_input_retries(self):
        q = wizard.Question("host.level", "档位",
                            choices=("hosted", "selfhosted"), default="hosted")
        ans, _ = wizard.ask_plain(q, None,
                                  input_fn=self._feed("bogus", "2"),
                                  print_fn=lambda *a, **k: None)
        assert ans == "selfhosted"

    def test_boolean_default_and_override(self):
        q = next(x for x in wizard.QUESTION_TABLE
                 if x.key == "admin.password_to_file")
        ans, _ = wizard.ask_plain(q, None, input_fn=self._feed(""),
                                  print_fn=lambda *a, **k: None)
        assert ans == "n"
        ans, _ = wizard.ask_plain(q, None, input_fn=self._feed("y"),
                                  print_fn=lambda *a, **k: None)
        assert ans == "y"


class TestDegradation:
    def test_interactive_without_questionary_falls_back_to_plain(self, monkeypatch):
        """The CI-tested guarantee: TTY or not, if questionary cannot import,
        the same table renders as plain prompts — nothing hangs."""
        monkeypatch.setattr(wizard, "_questionary_available", lambda: False)
        captured_prompts = []
        answers, prov = wizard.collect(
            FULL_SEED, interactive=True,
            input_fn=lambda _p="": "",
            print_fn=lambda *a, **k: captured_prompts.append(a))
        assert answers == FULL_SEED
        assert set(prov.values()) == {"(seeded)"}  # fully seeded: still no asks


class TestBuildProfile:
    def test_empty_domain_disables_front(self):
        seed = dict(FULL_SEED, **{"front.domain": ""})
        prof = wizard.build_profile(seed)
        assert prof.front.enabled is False
        assert prof.front.domain == ""

    def test_invalid_email_raises_abort(self):
        seed = dict(FULL_SEED, **{"admin.email": "not-an-email"})
        with pytest.raises(wizard.WizardAbort, match="admin.email"):
            wizard.build_profile(seed)


def _boom_input(_prompt=""):
    raise AssertionError("stdin must not be touched in this mode")

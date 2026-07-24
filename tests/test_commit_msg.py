#!/usr/bin/env python3
"""Tests for the commit-message linter (``celestia_devtools.vcs.commit_msg``)."""

from celestia_devtools.vcs.commit_msg import lint, GITMOJI_WHITELIST


class TestGitmojiWhitelist:
    """Verify the gitmoji whitelist covers common cases."""

    def test_common_emojis_present(self):
        """Canonical gitmoji set includes the most-used emojis."""
        assert "\U0001f41b" in GITMOJI_WHITELIST  # 🐛
        assert "\u2728" in GITMOJI_WHITELIST      # ✨
        assert "\U0001f680" in GITMOJI_WHITELIST  # 🚀
        assert "\U0001f4dd" in GITMOJI_WHITELIST  # 📝
        assert "\U0001f512" in GITMOJI_WHITELIST  # 🔒
        assert "\u267b\ufe0f" in GITMOJI_WHITELIST  # ♻️
        assert "\U0001f504" in GITMOJI_WHITELIST  # 🔄 (org extension)


class TestMergeAndRevertExemptions:
    def test_merge_commit_bypasses_all_rules(self):
        assert lint("Merge branch 'fix' into master") == []
        assert lint("Merge pull request #42 from celestia-island/dev") == []

    def test_revert_commit_bypasses_all_rules(self):
        assert lint('Revert "Fix the parser crash."') == []
        assert lint("Revert something") == []


class TestRule1GitmojiPrefix:
    def test_passes_with_valid_emoji(self):
        assert lint("\U0001f41b Fix the parser crash.") == []

    def test_fails_without_emoji(self):
        v = lint("Fix the parser crash.")
        assert any("gitmoji" in violation for violation in v)

    def test_fails_with_conventional_commit(self):
        v = lint("feat: add new parser")
        assert any("gitmoji" in violation for violation in v)

    def test_sync_emoji_passes(self):
        """🔄 is an org extension for sync/refresh operations."""
        assert lint("\U0001f504 Sync provider model list (updated 2026-07-07T09:34Z)") == []

    def test_sync_emoji_with_period_also_passes(self):
        assert lint("\U0001f504 Sync provider model list.") == []


class TestSyncEmojiPeriodExemption:
    """🔄-prefixed messages are exempt from the trailing-period rule."""

    def test_sync_without_period_passes(self):
        # No period — should pass because 🔄 exempts from period rule
        assert lint("\U0001f504 Sync provider model list (updated today)") == []

    def test_sync_with_parenthetical_timestamp_passes(self):
        assert lint("\U0001f504 Sync provider model list (updated 2026-07-07T09:34Z)") == []

    def test_sync_still_requires_uppercase(self):
        v = lint("\U0001f504 sync provider model list.")
        assert any("uppercase" in violation for violation in v)


class TestRule2NoConventionalPrefix:
    def test_fails_feat_prefix(self):
        v = lint("\U0001f41b feat: add new parser")
        assert any("Conventional Commits" in violation for violation in v)

    def test_fails_fix_prefix(self):
        v = lint("\U0001f41b fix: crash")
        assert any("Conventional Commits" in violation for violation in v)

    def test_fails_ci_with_scope_prefix(self):
        v = lint("\U0001f41b ci(docs): deploy")
        assert any("Conventional Commits" in violation for violation in v)

    def test_fails_refactor_prefix(self):
        v = lint("\U0001f41b refactor: split module.")
        assert any("Conventional Commits" in violation for violation in v)


class TestRule3CapitalLetter:
    def test_passes_uppercase(self):
        assert lint("\U0001f41b Fix the parser crash.") == []

    def test_fails_lowercase(self):
        v = lint("\U0001f41b fix the parser crash.")
        assert any("uppercase" in violation for violation in v)

    def test_fails_no_alpha(self):
        v = lint("\U0001f41b 123")
        assert any("least one English letter" in violation for violation in v)


class TestRule4NoVersionOrFillerPrefix:
    def test_fails_version_number_start(self):
        v = lint("\u2b06\ufe0f 0.3.0 is released.")  # ⬆️
        assert any("version" in violation.lower() for violation in v)

    def test_fails_bump_start(self):
        v = lint("\u2b06\ufe0f Bump version.")
        assert any("version" in violation.lower() for violation in v)

    def test_passes_descriptive_content(self):
        assert lint("\u2b06\ufe0f Upgrade the HTTP client to v2.") == []


class TestRule5TrailingPeriod:
    def test_passes_with_period(self):
        assert lint("\U0001f41b Fix the parser crash.") == []

    def test_fails_without_period(self):
        v = lint("\U0001f41b Fix the parser crash")
        assert any("period" in violation.lower() for violation in v)


class TestRule6EnglishOnly:
    def test_passes_english(self):
        assert lint("\u2728 Add distributed tracing.") == []

    def test_fails_chinese(self):
        v = lint("\u2728 \u4fee\u590d\u89e3\u6790\u5668\u5d29\u6e83.")
        assert any("CJK" in violation for violation in v)

    def test_fails_mixed(self):
        v = lint("\u2728 Fix \u89e3\u6790\u5668 crash.")
        assert any("CJK" in violation for violation in v)


class TestEmptyAndWhitespace:
    def test_empty_fails(self):
        v = lint("")
        assert any("empty" in violation.lower() for violation in v)

    def test_whitespace_fails(self):
        v = lint("   ")
        assert any("empty" in violation.lower() for violation in v)


class TestRegressionRealHistory:
    """Sample real commit subjects from celestia-island master branches."""

    def test_noa_commits_pass(self):
        # noa master: 🐛 Eliminate aws-lc-sys C dependency...
        assert lint(
            "\U0001f41b Eliminate aws-lc-sys C dependency and enable Windows builds."
        ) == []

    def test_kirino_commits_pass(self):
        # kirino master: 🔒 Add zero-trust authentication with trust scoring.
        assert lint(
            "\U0001f512 Add zero-trust authentication with trust scoring."
        ) == []

    def test_aoba_commits_pass(self):
        # aoba master: 🐛 Use license-file only so crates.io accepts...
        assert lint(
            "\U0001f41b Use license-file only so crates.io accepts the non-SPDX SySL license."
        ) == []

#!/usr/bin/env python3
"""Tests for the release-notes generator (``celestia_devtools.publish.release_notes``)."""

import pytest

from celestia_devtools.publish import release_notes as rn


# ── subject parsing ──────────────────────────────────────────────────────────

def test_parse_subject_extracts_pr_number():
    assert rn.parse_subject("🐛 Fix the nonce handshake. (#359)") == (
        359, "🐛 Fix the nonce handshake.")


def test_parse_subject_without_reference():
    assert rn.parse_subject("✨ Add a direct push.") == (None, "✨ Add a direct push.")


def test_parse_subject_keeps_inner_parens():
    assert rn.parse_subject("🔧 Support :is() tokens. (#12)") == (
        12, "🔧 Support :is() tokens.")


def test_parse_subject_legacy_period_after_reference():
    # aoba-era squash subjects end with the period AFTER the reference.
    assert rn.parse_subject(
        "💚 Fix CLI E2E MQTT/HTTP test stderr handling and cleanup. (#128)."
    ) == (128, "💚 Fix CLI E2E MQTT/HTTP test stderr handling and cleanup.")


# ── gitmoji classification ───────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("✨ Add invitation pages. (#37)", "✨ Features"),
    ("🎉 Bootstrap the repo. (#1)", "✨ Features"),
    ("🐛 Fix the nonce endpoint. (#20)", "🐛 Fixes"),
    ("🚑 Hotfix the parser crash. (#44)", "🐛 Fixes"),
    ("🩹 Patch a typo in the error path. (#9)", "🐛 Fixes"),
    ("✏️ Fix typo in README. (#8)", "🐛 Fixes"),
    ("⚡ Cache the compiled shaders. (#31)", "⚡ Performance"),
    ("📈 Add a benchmark for the walker. (#33)", "⚡ Performance"),
    ("♻️ Extract the shared walker. (#28)", "♻️ Refactoring"),
    ("🎨 Reorder the module layout. (#29)", "♻️ Refactoring"),
    ("🏗️ Restructure the crate graph. (#30)", "♻️ Refactoring"),
    ("🚚 Rename Tui.* RPC methods to Sync.*. (#26)", "♻️ Refactoring"),
    ("💄 Allow toasts to wrap to 3 lines. (#24)", "💄 UI"),
    ("📱 Fix the layout on narrow screens. (#25)", "💄 UI"),
    ("♿ Label the icon buttons. (#27)", "💄 UI"),
    ("🌐 Add mockMode i18n to all locales. (#63)", "🌐 Localization"),
    ("📝 Add deployment guide. (#8)", "📝 Documentation"),
    ("💡 Clarify the retry comment. (#10)", "📝 Documentation"),
    ("💬 Reword the onboarding copy. (#11)", "📝 Documentation"),
    ("✅ Add commit message lint CI workflow. (#32)", "✅ Tests"),
    ("🧪 Test the annotated-tag path. (#13)", "✅ Tests"),
    ("⬆️ Update kirino to 0.6. (#40)", "⬆️ Dependencies"),
    ("⬇️ Pin serde to 1.0. (#41)", "⬆️ Dependencies"),
    ("📌 Lock the toolchain. (#42)", "⬆️ Dependencies"),
    ("➕ Add plana-rpc-client. (#62)", "⬆️ Dependencies"),
    ("➖ Drop the unused mock dep. (#43)", "⬆️ Dependencies"),
    ("🔒 Enforce DATABASE_URL as mandatory. (#112)", "🔒 Security"),
    ("🛡️ Fail closed on unknown issuers. (#44)", "🔒 Security"),
    ("🔥 Remove the SSE endpoints. (#67)", "🔥 Removals"),
    ("🔧 Sort Cargo.toml deps alphabetically. (#52)", "🔧 Maintenance"),
    ("🔖 Release v0.2.16.", "🔧 Maintenance"),
    ("👷 Build with pnpm 11. (#45)", "🔧 Maintenance"),
    ("📦 Package the installer. (#46)", "🔧 Maintenance"),
    ("🚀 Deploy via the tag workflow. (#47)", "🔧 Maintenance"),
    ("🍱 Refresh the bundled icons. (#48)", "🔧 Maintenance"),
    ("🚨 Fix nonminimal_bool lint debt. (#173)", "🔧 Maintenance"),
    ("📜 Switch to the SySL license. (#5)", "📜 License"),
    ("📄 Update the license headers. (#6)", "📜 License"),
    ("🔗 Sync copilot settings. (#7)", "🔄 Sync"),
    ("🔄 Sync the ledger from master. (#9)", "🔄 Sync"),
    ("Replace hand-rolled JSON-RPC types with plana types (#117)", "Other Changes"),
    ("🙂 Unknown emoji. (#1)", "Other Changes"),
    ("Bump actions/checkout from 4 to 7 (#55)", "⬆️ Dependencies"),
])
def test_classify(subject, expected):
    assert rn.classify(subject) == expected


# ── semver tags ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag,expected", [
    ("v0.2.9", (0, 2, 9)),
    ("v0.3", (0, 3, 0)),
    ("v1.0.0", (1, 0, 0)),
    ("flasher-v1.2.3", (1, 2, 3)),
    ("fonts", None),
    ("res-latest", None),
    ("mod-hub", None),
])
def test_parse_semver(tag, expected):
    assert rn.parse_semver(tag) == expected


def test_resolve_previous_tag_picks_highest_below():
    tags = [{"name": n} for n in ["v0.2.10", "v0.2.9", "v0.1.0", "res-latest"]]

    def fetch(url):
        return tags  # single short page

    assert rn.resolve_previous_tag("o/r", "v0.3.0", fetch) == "v0.2.10"


def test_resolve_previous_tag_ignores_non_semver_current():
    assert rn.resolve_previous_tag("o/r", "fonts", lambda url: [{"name": "fonts"}]) is None


def test_resolve_previous_tag_paginates():
    page1 = [{"name": f"v1.{i}.0"} for i in range(100)]
    page2 = [{"name": "v2.0.0"}]

    def fetch(url):
        assert "per_page=100" in url
        return page2 if "page=2" in url else page1

    assert rn.resolve_previous_tag("o/r", "v3.0.0", fetch) == "v2.0.0"


# ── commit walking ───────────────────────────────────────────────────────────

def _commit(sha, message):
    return {"sha": sha, "commit": {"message": message}}


def test_tag_commit_sha_lightweight():
    def fetch(url):
        return {"object": {"type": "commit", "sha": "abc"}}

    assert rn.tag_commit_sha("o/r", "v1.0.0", fetch) == "abc"


def test_tag_commit_sha_annotated():
    calls = []

    def fetch(url):
        calls.append(url)
        if "/git/ref/tags/" in url:
            return {"object": {"type": "tag", "sha": "tagobj"}}
        return {"object": {"type": "commit", "sha": "abc"}}

    assert rn.tag_commit_sha("o/r", "v1.0.0", fetch) == "abc"
    assert any("/git/tags/tagobj" in c for c in calls)


def test_collect_entries_groups_and_stops_at_base():
    commits = [
        _commit("c1", "✨ Add feature A. (#41)"),
        _commit("c2", "🐛 Fix feature A. (#42)"),
        _commit("c3", "✨ Add feature A better. (#41)"),  # duplicate PR → dropped
        _commit("c4", "Direct push without a PR."),       # no reference → skipped
        _commit("base", "🔖 Release v0.1.0."),            # previous tag → stop
        _commit("old", "✨ From before the release. (#1)"),
    ]

    def fetch(url):
        if "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": "base"}}
        return commits

    sections, skipped = rn.collect_entries("o/r", "v0.2.0", "v0.1.0", fetch)
    assert skipped == 1
    assert sections == [
        ("✨ Features", [(41, "✨ Add feature A."), ]),
        ("🐛 Fixes", [(42, "🐛 Fix feature A.")]),
    ]


def test_collect_entries_paginates_until_base_found():
    page1 = [_commit(f"s{i}", f"🐛 Fix {i}. (#{i})") for i in range(100)]
    page2 = [_commit("base", "🔖 Release v0.1.0.")]
    pages = {1: page1, 2: page2}
    seen_pages = []

    def fetch(url):
        if "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": "base"}}
        page = int(url.rsplit("page=", 1)[1])
        seen_pages.append(page)
        return pages[page]

    sections, skipped = rn.collect_entries("o/r", "v0.2.0", "v0.1.0", fetch)
    assert seen_pages == [1, 2]
    assert skipped == 0
    assert len(sections[0][1]) == 100


def test_collect_entries_raises_when_base_never_reached():
    def fetch(url):
        if "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": "elsewhere"}}
        return [_commit("c1", "✨ Add. (#1)")]

    with pytest.raises(rn.GitHubError, match="never reached"):
        rn.collect_entries("o/r", "v0.2.0", "v0.1.0", fetch)


def test_collect_entries_full_history_when_no_base():
    def fetch(url):
        if "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": "head"}}
        return [_commit("c1", "✨ Add. (#1)")]

    sections, skipped = rn.collect_entries("o/r", "v1.0.0", None, fetch)
    assert sections == [("✨ Features", [(1, "✨ Add.")])]
    assert skipped == 0


# ── rendering ────────────────────────────────────────────────────────────────

def test_build_body_standard_format():
    sections = [
        ("✨ Features", [(41, "✨ Add invitation pages.")]),
        ("🐛 Fixes", [(40, "🐛 Fix the nonce endpoint."), (38, "🐛 Fix the crash.")]),
    ]
    body = rn.build_body("o/r", "v0.2.0", "v0.1.0", sections)
    assert body == (
        "## What's Changed\n"
        "\n"
        "### ✨ Features\n"
        "\n"
        "- [#41](https://github.com/o/r/pull/41) ✨ Add invitation pages.\n"
        "\n"
        "### 🐛 Fixes\n"
        "\n"
        "- [#40](https://github.com/o/r/pull/40) 🐛 Fix the nonce endpoint.\n"
        "- [#38](https://github.com/o/r/pull/38) 🐛 Fix the crash.\n"
        "\n"
        "**Full Changelog**: https://github.com/o/r/compare/v0.1.0...v0.2.0\n"
    )


def test_build_body_without_previous_tag():
    body = rn.build_body("o/r", "v1.0.0", None, [("✨ Features", [(1, "✨ Add.")])])
    assert "Full Changelog" not in body


def test_build_body_empty_range():
    body = rn.build_body("o/r", "v1.0.0", "v0.9.0", [])
    assert "_No pull requests were merged in this release._" in body
    assert "**Full Changelog**: https://github.com/o/r/compare/v0.9.0...v1.0.0" in body


# ── release upsert ───────────────────────────────────────────────────────────

def test_upsert_release_patches_existing(monkeypatch):
    calls = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            if url.endswith("/releases/tags/v0.2.0"):
                return 200, {"id": 7, "draft": True}
            if "/releases?" in url:
                return 200, []
            raise rn.NotFoundError("missing")
        return 200, {}

    monkeypatch.setattr(rn, "_request", fake_request)
    outcome = rn.upsert_release("o/r", "v0.2.0", "BODY", "tok")
    assert "draft" in outcome
    assert calls[0] == ("GET",
                        f"{rn.API_ROOT}/repos/o/r/releases/tags/v0.2.0", None)
    assert calls[1][:2] == ("PATCH", f"{rn.API_ROOT}/repos/o/r/releases/7")
    assert calls[1][2] == {"body": "BODY"}  # state untouched, no name overwrite


def test_upsert_release_creates_draft_when_missing(monkeypatch):
    calls = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            if "/releases?" in url:
                return 200, []  # listing fallback: no existing release
            raise rn.NotFoundError("missing")
        return 201, {}

    monkeypatch.setattr(rn, "_request", fake_request)
    outcome = rn.upsert_release("o/r", "v0.2.0", "BODY", "tok", title="v0.2.0")
    assert "draft" in outcome
    method, url, payload = [c for c in calls if c[0] == "POST"][0]
    assert url == f"{rn.API_ROOT}/repos/o/r/releases"
    assert payload == {"tag_name": "v0.2.0", "name": "v0.2.0",
                       "body": "BODY", "draft": True}


def test_upsert_release_publish_flag(monkeypatch):
    def fake_request(method, url, token, payload=None):
        if method == "GET":
            if "/releases?" in url:
                return 200, []
            raise rn.NotFoundError("missing")
        return 201, {}

    monkeypatch.setattr(rn, "_request", fake_request)
    outcome = rn.upsert_release("o/r", "v0.2.0", "BODY", "tok", publish=True)
    assert "published" in outcome


# ── CLI end-to-end (network monkeypatched) ───────────────────────────────────

def test_cli_writes_body_file(monkeypatch, tmp_path):
    tags = [{"name": "v0.1.0"}]
    commits = [
        _commit("c1", "✨ Add the thing. (#1)"),
        _commit("base", "🔖 Release v0.1.0."),
    ]

    def fake_request(method, url, token, payload=None):
        assert method == "GET"
        if "/tags?" in url:
            return 200, tags
        if "/git/ref/tags/" in url:
            return 200, {"object": {"type": "commit", "sha": "base"}}
        if "/commits?" in url:
            return 200, commits
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(rn, "resolve_token", lambda: "tok")
    monkeypatch.setattr(rn, "_request", fake_request)
    out = tmp_path / "notes.md"
    rc = rn.main(["--repo", "o/r", "--tag", "v0.2.0", "--out", str(out)])
    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert "### ✨ Features" in body
    assert "compare/v0.1.0...v0.2.0" in body


def test_cli_requires_repo_and_tag(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setattr(rn, "resolve_token", lambda: None)
    with pytest.raises(SystemExit) as exc:
        rn.main([])
    assert exc.value.code == 2


def test_cli_apply_posts_release(monkeypatch):
    calls = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if "/tags?" in url:
            return 200, []
        if "/git/ref/tags/" in url:
            return 200, {"object": {"type": "commit", "sha": "head"}}
        if "/commits?" in url:
            return 200, [_commit("c1", "✨ Add. (#1)")]
        if "/releases?" in url:
            return 200, []  # no existing release (draft-safe listing fallback)
        if method == "GET":
            raise rn.NotFoundError("missing")
        return 201, {}

    monkeypatch.setattr(rn, "resolve_token", lambda: "tok")
    monkeypatch.setattr(rn, "_request", fake_request)
    rc = rn.main(["--repo", "o/r", "--tag", "v1.0.0", "--no-previous", "--apply"])
    assert rc == 0
    methods = [c[0] for c in calls]
    assert "POST" in methods
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[2]["draft"] is True

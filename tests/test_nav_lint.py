"""Tests for celestia_devtools.lint.nav_lint."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow running the tests against a source checkout without a pip
# install (PYTHONPATH-style); the CI venv install makes this a no-op.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.lint import nav_lint  # noqa: E402


def write(tmp_path: Path, body: str, name: str = "sample.ts") -> Path:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


def codes(findings):
    """Normalized finding families (rooted-literal variants collapse)."""
    return [f.code.split("-rooted")[0] for f in findings]


# ---------------------------------------------------------------------------
# router targets
# ---------------------------------------------------------------------------


def test_router_literal_paths_pass(tmp_path):
    f = write(tmp_path, 'router.push("/backend");\nrouter.replace(`/@${id}#h`);\n$router.push("#think");\n')
    assert nav_lint.scan_file(f) == []


def test_router_object_locations_pass(tmp_path):
    f = write(
        tmp_path,
        "router.push({ path: '/backend', query });\n"
        "router.push({ name: 'login' });\n"
        "router.push({ hash: `#${h}`, query: route.query });\n",
    )
    assert nav_lint.scan_file(f) == []


def test_router_bare_identifier_flagged(tmp_path):
    f = write(tmp_path, "router.push(redirect);\n")
    assert codes(nav_lint.scan_file(f)) == ["router-target"]


def test_router_stringified_undefined_flagged(tmp_path):
    # The field-classic: template that lost its leading slash.
    f = write(tmp_path, "router.push(`@${ref}`);\n")
    assert codes(nav_lint.scan_file(f)) == ["router-target"]


def test_router_absolute_url_flagged(tmp_path):
    f = write(tmp_path, "router.replace('https://evil.example/x');\n")
    assert codes(nav_lint.scan_file(f)) == ["router-target"]


def test_router_nav_ok_annotation_suppresses(tmp_path):
    f = write(
        tmp_path,
        "// nav-ok: sanitized through safeRedirect()\n"
        "router.push(redirect);\n",
    )
    assert nav_lint.scan_file(f) == []


def test_router_bare_nav_ok_requires_reason(tmp_path):
    f = write(tmp_path, "router.push(redirect); // nav-ok\n")
    assert codes(nav_lint.scan_file(f)) == ["router-target"]
    assert nav_lint.scan_file(f, require_reason=False) == []


def test_string_replace_not_flagged(tmp_path):
    f = write(tmp_path, "const s = name.replace(/a/, 'b');\n")
    assert nav_lint.scan_file(f) == []


# ---------------------------------------------------------------------------
# location writes (bypass the History net)
# ---------------------------------------------------------------------------


def test_location_rooted_literal_passes(tmp_path):
    f = write(tmp_path, 'location.assign("/");\nwindow.location.href = "/backend#x";\n')
    assert nav_lint.scan_file(f) == []


def test_location_variable_flagged(tmp_path):
    f = write(tmp_path, "window.location.href = url;\n")
    assert codes(nav_lint.scan_file(f)) == ["location-target"]


def test_location_origin_concat_flagged(tmp_path):
    # The LoginView-class producer: origin + possibly-undefined value.
    f = write(tmp_path, "window.location.href = `${window.location.origin}${raw}`;\n")
    assert codes(nav_lint.scan_file(f)) == ["location-target"]


def test_location_nav_ok_with_reason_passes(tmp_path):
    f = write(
        tmp_path,
        "window.location.href = url; // nav-ok: regex-validated https?:// absolute",
    )
    assert nav_lint.scan_file(f) == []


# ---------------------------------------------------------------------------
# history calls
# ---------------------------------------------------------------------------


def test_history_pathname_suffix_passes(tmp_path):
    f = write(tmp_path, 'history.pushState(null, "", `${window.location.pathname}${suffix}`);\n')
    assert nav_lint.scan_file(f) == []


def test_history_rooted_literal_passes(tmp_path):
    f = write(tmp_path, 'history.replaceState(null, "", "/backend");\n')
    assert nav_lint.scan_file(f) == []


def test_history_garbage_url_flagged(tmp_path):
    f = write(tmp_path, 'window.history.pushState(null, "", target);\n')
    assert codes(nav_lint.scan_file(f)) == ["history-url"]


def test_history_multiline_call_flagged(tmp_path):
    f = write(
        tmp_path,
        "history.pushState(\n"
        "  state,\n"
        '  "",\n'
        '  url,\n'
        ");\n",
    )
    assert codes(nav_lint.scan_file(f)) == ["history-url"]


def test_history_two_arg_call_passes(tmp_path):
    f = write(tmp_path, 'window.history.pushState(state, "");\n')
    assert nav_lint.scan_file(f) == []


# ---------------------------------------------------------------------------
# CLI + traversal
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, capsys):
    good = write(tmp_path, 'router.push("/x");\n', "good.ts")
    bad = write(tmp_path, "router.push(x);\n", "bad.ts")
    rc = nav_lint.main([str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "bad.ts" in out and "router-target" in out
    assert "1 finding" in out

    rc = nav_lint.main([str(good)])
    assert rc == 0

    rc = nav_lint.main([str(tmp_path), "--allow-bare-nav-ok"])
    assert rc == 1  # bad.ts has no annotation at all


def test_skips_node_modules_and_dist(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.ts").write_text("router.push(x);\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("router.push(y);\n")
    targets = list(nav_lint.iter_targets([tmp_path]))
    assert targets == []


def test_vue_template_push_scanned(tmp_path):
    f = write(tmp_path, '<button @click="$router.push(target)">go</button>\n', "Comp.vue")
    assert codes(nav_lint.scan_file(f)) == ["router-target"]

# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for celestia_devtools.lint.type_escapes.

The first test is the one CI relies on: a gate that cannot catch its own known
positive would report every repository clean. The rest pin the contract the
reusable workflow depends on — exit codes (0 clean, 1 violations, 2 usage), the
allowlist scoping, the `*.d.ts` advisory split, and the JSON envelope.
"""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

# Allow running the tests against a source checkout without a pip
# install (PYTHONPATH-style); the CI venv install makes this a no-op.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.core import cli  # noqa: E402
from celestia_devtools.lint import type_escapes  # noqa: E402

def write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_text(body, encoding = "utf-8")
    return target

def test_selftest_passes(capsys):
    """The CI-covered control: the scanner proves itself before judging a tree."""

    assert type_escapes.main(["--selftest"]) == 0
    assert "selftest OK" in capsys.readouterr().out

def test_dispatcher_reaches_the_command():
    assert cli.COMMANDS["type-escapes"] == "celestia_devtools.lint.type_escapes"
    assert callable(sys.modules["celestia_devtools.lint.type_escapes"].main)

def test_clean_tree_exits_zero(tmp_path):
    write(tmp_path, "clean.ts", "export const value: unknown = JSON.parse(raw);\nexport const width = 1;\n")
    assert type_escapes.main([str(tmp_path)]) == 0

def test_dirty_tree_exits_one(tmp_path, capsys):
    write(tmp_path, "dirty.ts", "export const payload = body as any;\n")
    assert type_escapes.main([str(tmp_path)]) == 1
    assert "cast-any" in capsys.readouterr().out

def test_missing_path_is_a_usage_error(tmp_path, capsys):
    assert type_escapes.main([str(tmp_path / "absent")]) == 2
    assert "no such path" in capsys.readouterr().err

def test_path_without_scannable_files_is_a_usage_error(tmp_path, capsys):
    """A mistyped path must not read as clean: zero files scanned is exit 2."""

    write(tmp_path, "README.md", "# nothing to scan here\n")
    assert type_escapes.main([str(tmp_path)]) == 2
    assert "no scannable file" in capsys.readouterr().err
    assert type_escapes.main([str(tmp_path), "--allow-empty"]) == 0

def test_default_target_is_the_git_root(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check = True)
    write(tmp_path, "packages/web/src/escape.ts", "const a = b as unknown as Widget;\n")
    monkeypatch.chdir(tmp_path / "packages" / "web")
    assert type_escapes.main([]) == 1

def test_dts_is_advisory_until_fail_on_dts(tmp_path, capsys):
    write(tmp_path, "shim.d.ts", "declare const payload: any;\n")
    assert type_escapes.main([str(tmp_path)]) == 0
    assert "declaration files (advisory" in capsys.readouterr().out
    assert type_escapes.main([str(tmp_path), "--fail-on-dts"]) == 1

def test_json_output_shape(tmp_path, capsys):
    write(tmp_path, "dirty.ts", "export const payload = body as any;\n")
    assert type_escapes.main([str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["hits"][0]["rule"] == "cast-any"
    assert payload["scanned"] == 1
    assert payload["dts_count"] == 0

def test_same_line_allowlist_is_line_scoped(tmp_path):
    body = "const a = b as any;\nconst c = d as any; // ts-escape-ok: fixture keeps this line\nconst e = f as any;\n"
    path = write(tmp_path, "scoped.ts", body)
    hits = type_escapes.scan_text(path.read_text(encoding = "utf-8"), "scoped.ts")
    assert [hit["line"] for hit in hits] == [1, 3]

def test_file_allowlist_needs_a_reason():
    excused = "// ts-escape-allow-file: generated glue, regenerate upstream\nconst a = b as any;\n"
    bare = "// ts-escape-allow-file:\nconst a = b as any;\n"
    assert type_escapes.scan_text(excused, "a.ts") == []
    assert len(type_escapes.scan_text(bare, "b.ts")) == 1

def test_unknown_annotations_are_not_escapes():
    body = "const a: unknown = input;\nconst b: Record<string, unknown> = {};\n"
    assert type_escapes.scan_text(body, "ok.ts") == []

def test_every_rule_fires_on_its_own_shape():
    body = "const a = b as unknown as T;\nconst c = (d as unknown) as T;\nconst e = f as any;\nconst g = <any>h;\nconst i: any = j;\nconst k: Promise<any> = l();\nconst m: () => any = n;\n// @ts-expect-error\nconst o = p;\n"
    seen = {hit["rule"] for hit in type_escapes.scan_text(body, "rules.ts")}
    assert seen == {"double-cast", "paren-dbl-cast", "cast-any", "any-generic", "any-annot", "any-return", "suppress"}

def test_prose_comments_and_strings_never_fire():
    body = "// as any is described here\n/* as unknown as appears in a block comment */\nconst s = \"cast as any is fine inside a string\";\nexpect(el).toEqual(expect.any(String));\n"
    assert type_escapes.scan_text(body, "prose.ts") == []

def test_vue_and_mts_files_are_scanned(tmp_path):
    write(tmp_path, "widget.vue", "<script lang=\"ts\">\nconst a = b as any;\n</script>\n")
    write(tmp_path, "mod.mts", "export const c = d as unknown as Widget;\n")
    assert type_escapes.main([str(tmp_path)]) == 1

def test_build_output_and_dependencies_are_skipped(tmp_path):
    write(tmp_path, "node_modules/pkg/escape.ts", "const a = b as any;\n")
    write(tmp_path, "dist/bundle.ts", "const c = d as any;\n")
    write(tmp_path, "src/ok.ts", "export const width = 1;\n")
    assert type_escapes.main([str(tmp_path)]) == 0

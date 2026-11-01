# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for celestia_devtools.lint.ts_surface.

The declaration-emit path is exercised with a fake checker on purpose: the gate
must be testable without a TypeScript toolchain, and the fake also proves the
command fails loudly (rather than reading as clean) when the emit produces
nothing.
"""

from __future__ import annotations
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.lint import ts_surface  # noqa: E402

def write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_text(body, encoding = "utf-8")

    return target

def fake_emit(tmp_path: Path, declaration: str) -> str:
    """A fake checker that writes `declaration` into the requested --outDir."""

    script = write(tmp_path, "fake_tsc.py", f'''import pathlib, sys

argv = sys.argv[1:]
out = pathlib.Path(argv[argv.index("--outDir") + 1])
out.mkdir(parents = True, exist_ok = True)
(out / "index.d.ts").write_text({declaration!r}, encoding = "utf-8")
''')

    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

def test_selftest_passes(capsys):
    assert ts_surface.main(["--selftest"]) == 0
    assert "selftest OK" in capsys.readouterr().out

def test_authored_any_on_the_surface_fails(tmp_path, capsys):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "noEmit": true}\n}\n')
    checker = fake_emit(tmp_path, "export declare const payload: any;\n")
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker]) == 1
    assert "any-annot" in capsys.readouterr().out

def test_clean_surface_passes(tmp_path):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "noEmit": true}\n}\n')
    checker = fake_emit(tmp_path, "export declare const payload: unknown;\nexport declare function read(value: string): void;\n")
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker]) == 0

def test_vue_synthesised_any_is_suppressed_but_counted(tmp_path, capsys):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "noEmit": true}\n}\n')
    declaration = 'export declare const C: import("vue").DefineComponent<{}, {}, {}, {}, string, import("vue").ComponentProvideOptions, true, {}, any>;\n'
    checker = fake_emit(tmp_path, declaration)
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker]) == 0
    assert "suppressed" in capsys.readouterr().out

def test_keep_noise_surfaces_the_vue_synthesis(tmp_path, capsys):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "noEmit": true}\n}\n')
    declaration = 'export declare const C: import("vue").DefineComponent<{}, {}, {}, {}, string, import("vue").ComponentProvideOptions, true, {}, any>;\n'
    checker = fake_emit(tmp_path, declaration)
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker, "--keep-noise"]) == 1
    assert "ComponentProvideOptions" in capsys.readouterr().out

def test_missing_tsconfig_is_a_usage_error(tmp_path, capsys):
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "absent.json")]) == 2
    assert "no such tsconfig" in capsys.readouterr().err

def test_an_emit_that_produces_nothing_fails_loudly(tmp_path, capsys):
    """A silent empty emit must not read as a clean surface."""

    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true}\n}\n')
    script = write(tmp_path, "noop.py", "pass\n")
    checker = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    assert ts_surface.main(["emit-scan", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker]) == 1
    assert "produced no files" in capsys.readouterr().err

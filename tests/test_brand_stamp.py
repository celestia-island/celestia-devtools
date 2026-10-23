"""brand-stamp idempotency and guardrails (stdlib tmp projects)."""

from pathlib import Path

from celestia_devtools.brand.stamp import _stamp

EXIT_CONFLICT = 3


def _mk_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8")
    return root


def test_stamp_writes_all_three_artifacts(tmp_path: Path) -> None:
    proj = _mk_project(tmp_path / "x")
    rc = _stamp(proj, "icon")
    assert rc == 0
    assert (proj / "build.rs").is_file()
    assert "winresource" in (proj / "Cargo.toml").read_text(encoding="utf-8")
    assert (proj / "brand" / "icon.ico").is_file()


def test_stamp_is_idempotent(tmp_path: Path) -> None:
    proj = _mk_project(tmp_path / "x")
    assert _stamp(proj, "icon") == 0
    manifest = (proj / "Cargo.toml").read_text(encoding="utf-8")
    assert _stamp(proj, "icon") == 0  # second run: our own template = no-op
    assert (proj / "Cargo.toml").read_text(encoding="utf-8") == manifest


def test_stamp_refuses_existing_build_rs(tmp_path: Path) -> None:
    proj = _mk_project(tmp_path / "x")
    (proj / "build.rs").write_text("// bespoke\n", encoding="utf-8")
    assert _stamp(proj, "icon") == EXIT_CONFLICT
    assert (proj / "build.rs").read_text(encoding="utf-8") == "// bespoke\n"


def test_stamp_requires_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _stamp(empty, "icon") == 2


def test_stamp_evernight_icon(tmp_path: Path) -> None:
    """The per-member icon wires identically to the family default."""
    proj = _mk_project(tmp_path / "x")
    assert _stamp(proj, "evernight") == 0
    assert (proj / "brand" / "evernight.ico").is_file()

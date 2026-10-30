"""Tests for the family-layer data file (repo/family_layers.toml, SR-20)."""

from __future__ import annotations
import tomllib
from pathlib import Path
import pytest
from celestia_devtools.repo.family_versions import PLANA_PUBLISHED, family_layers_error, load_family_layers, parse_layer_line
from celestia_devtools.repo.family_versions import _FAMILY_LAYERS_PATH

def test_bundled_table_is_pinned():
    layers = load_family_layers()
    assert layers["updated_at"] == "2026-10-30"
    assert layers["source"].startswith("plana/.github/workflows/publish.yml")
    assert layers["plana_published"] == {"plana": (0, 2, 0), "plana-rpc-client": (0, 2, 0), "plana-rpc-server": (0, 1, 0), "plana-celestia-types": (0, 1, 0), "plana-tauri": (0, 1, 0), "plana-config": (0, 1, 0), "plana-build-info": (0, 1, 0), "plana-infra-utils": (0, 1, 0), "plana-llm-provider": (0, 1, 0), "plana-dispatch-client": (0, 1, 0)}
    assert [entry[0] for entry in layers["rust_layers"]] == ["kirino", "plana", "hikari"]
    assert dict((name, line) for name, line, _why in layers["rust_layers"]) == {"kirino": (0, 9, 0), "plana": (0, 2, 0), "hikari": (0, 3, 0)}
    assert layers["rust_overrides"] == {"kirino_session": ("kirino", (0, 7, 0), layers["rust_overrides"]["kirino_session"][2])}
    assert load_family_layers()["rust_overrides"]["kirino_session"][:2] == ("kirino", (0, 7, 0))
    assert dict((name, line) for name, line, _why in layers["npm_layers"]) == {"@celestia-island/hikari": (0, 55, 0), "@celestia-island/kirino": (0, 7, 0), "@celestia-island/plana-types": (0, 1, 0), "@celestia-island/plana-rpc-client": (0, 1, 0)}

def test_module_constants_come_from_the_data_file():
    assert PLANA_PUBLISHED["plana"] == (0, 2, 0)

def test_data_file_sits_next_to_the_module():
    assert _FAMILY_LAYERS_PATH.is_file()
    assert _FAMILY_LAYERS_PATH.name == "family_layers.toml"

def test_parse_layer_line():
    assert parse_layer_line("0.2", "x") == (0, 2, 0)
    assert parse_layer_line(" 12.34 ", "x") == (12, 34, 0)

    for bad in ("0", "0.2.1", "v0.2", "", None, 2):
        with pytest.raises(family_layers_error):
            parse_layer_line(bad, "x")

def _write(tmp_path, text):
    path = tmp_path / "family_layers.toml"
    path.write_text(text, encoding = "utf-8")
    return path

BASE = 'updated_at = "2026-10-25"\nsource = "test"\n\n[plana_published]\nplana = "0.2"\n\n[rust_layers.kirino]\nline = "0.7"\nwhy = "auth"\n\n[npm_layers."@celestia-island/hikari"]\nline = "0.55"\nwhy = "ui"\n'

def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(family_layers_error, match = "not found"):
        load_family_layers(tmp_path / "nope.toml")

def test_malformed_toml_fails_loudly(tmp_path):
    path = _write(tmp_path, "[plana_published\nplana = \"0.2\"\n")

    with pytest.raises(family_layers_error, match = "not valid TOML"):
        load_family_layers(path)

def test_bad_line_fails_loudly(tmp_path):
    path = _write(tmp_path, BASE.replace('plana = "0.2"', 'plana = "0.x"'))

    with pytest.raises(family_layers_error, match = "major.minor"):
        load_family_layers(path)

def test_missing_reason_fails_loudly(tmp_path):
    path = _write(tmp_path, BASE.replace('why = "ui"', 'why = "  "'))

    with pytest.raises(family_layers_error, match = "why"):
        load_family_layers(path)

def test_empty_published_table_fails_loudly(tmp_path):
    path = _write(tmp_path, BASE.replace('[plana_published]\nplana = "0.2"\n', '[plana_published]\n'))

    with pytest.raises(family_layers_error, match = "plana_published"):
        load_family_layers(path)

def test_a_family_cannot_override_itself(tmp_path):
    path = _write(tmp_path, BASE.replace('[rust_layers.kirino]\nline = "0.7"', '[rust_layers.kirino]\nline = "0.7"\ncrates = { kirino = "0.2" }'))

    with pytest.raises(family_layers_error):
        load_family_layers(path)

def test_a_parked_member_must_belong_to_its_family(tmp_path):
    path = _write(tmp_path, BASE.replace('[rust_layers.kirino]\nline = "0.7"', '[rust_layers.kirino]\nline = "0.7"\ncrates = { plana = "0.2" }'))

    with pytest.raises(family_layers_error):
        load_family_layers(path)

def test_an_empty_parked_member_table_fails_loudly(tmp_path):
    path = _write(tmp_path, BASE.replace('[rust_layers.kirino]\nline = "0.7"', '[rust_layers.kirino]\nline = "0.7"\ncrates = {}'))

    with pytest.raises(family_layers_error):
        load_family_layers(path)

def test_a_parked_member_on_the_npm_track_fails_loudly(tmp_path):
    body = BASE + '\n[npm_layers."@celestia-island/kirino"]\nline = "0.7"\nwhy = "ui"\ncrates = { kirino-session = "0.7" }\n'
    path = _write(tmp_path, body)

    with pytest.raises(family_layers_error):
        load_family_layers(path)

def test_a_parked_member_is_loaded_as_an_override(tmp_path):
    path = _write(tmp_path, BASE.replace('[rust_layers.kirino]\nline = "0.7"', '[rust_layers.kirino]\nline = "0.9"\ncrates = { kirino-session = "0.7" }'))
    layers = load_family_layers(path)
    assert layers["rust_overrides"] == {"kirino_session": ("kirino", (0, 7, 0), "auth")}
    assert dict((name, line) for name, line, _why in layers["rust_layers"])["kirino"] == (0, 9, 0)

def test_wheel_includes_the_data_file():
    """Hatchling force-includes packaged data explicitly; a missing entry ships a broken gate."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding = "utf-8"))
    forced = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert forced.get("src/celestia_devtools/repo/family_layers.toml") == "celestia_devtools/repo/family_layers.toml"

def test_member_name_normalisation(tmp_path):
    path = _write(tmp_path, BASE.replace('plana = "0.2"', 'plana_rpc_client = "0.2"'))
    layers = load_family_layers(path)
    assert layers["plana_published"] == {"plana-rpc-client": (0, 2, 0)}

def test_missing_rust_layer_table_fails_loudly(tmp_path):
    body = BASE.replace('[rust_layers.kirino]\nline = "0.7"\nwhy = "auth"\n\n', '')

    with pytest.raises(family_layers_error, match = "rust_layers"):
        load_family_layers(_write(tmp_path, body))

def test_missing_stamp_fails_loudly(tmp_path):
    body = BASE.replace('updated_at = "2026-10-25"\n', '')

    with pytest.raises(family_layers_error, match = "updated_at"):
        load_family_layers(_write(tmp_path, body))

def test_leading_zero_line_is_rejected(tmp_path):
    path = _write(tmp_path, BASE.replace('plana = "0.2"', 'plana = "00.2"'))

    with pytest.raises(family_layers_error, match = "major.minor"):
        load_family_layers(path)

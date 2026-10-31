"""Guards for the data files the wheel force-includes.

*Why this exists:* four files ship through Hatchling's ``force-include`` rather
than through ``packages = [...]`` -- ``common.just`` (the just template every
consumer stages), ``lint/p0_ledger.toml`` (the P0 gate's authoritative ledger),
``repo/family_layers.toml`` (the family-layer baselines) and
``lint/ts_policy.toml`` (the TypeScript strictness catalog every ``ts-policy``
caller reads). Only ``family_layers.toml`` had a guard when it landed on
2026-10-25. Deleting a single line from
``pyproject.toml`` therefore ships a wheel where ``init`` stages a missing
template and ``p0-gate`` fails closed for every caller -- and the 2,263-test
suite stayed green, because nothing imported the files, only referenced them by
path.

Two halves are asserted, because either can rot alone:

- the **build contract** (``pyproject.toml`` still force-includes the file at the
  wheel path the code reads), with a positive control on the table's size so the
  assertion cannot pass vacuously over an empty dict;
- the **runtime reach** (the code path that consumes the file can actually load
  it, and the file carries real content rather than a placeholder).
"""

from __future__ import annotations
import tomllib
from pathlib import Path
import celestia_devtools
from celestia_devtools.lint.p0_gate import bundled_ledger_path, load_ledger
from celestia_devtools.repo.init import common_just_path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: (source path in the tree, wheel path the code resolves at runtime)
FORCED_DATA = [("src/celestia_devtools/common.just", "celestia_devtools/common.just"), ("src/celestia_devtools/lint/p0_ledger.toml", "celestia_devtools/lint/p0_ledger.toml"), ("src/celestia_devtools/repo/family_layers.toml", "celestia_devtools/repo/family_layers.toml"), ("src/celestia_devtools/lint/ts_policy.toml", "celestia_devtools/lint/ts_policy.toml")]

def _force_include() -> dict:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding = "utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

def _assert_package_is_this_checkout() -> None:
    # The build half reads the checkout's pyproject.toml; without this the two
    # runtime tests could pass against an installed copy that happens to carry
    # the files, so the two halves would not be describing the same package.
    installed = Path(celestia_devtools.__file__).resolve().parent
    assert installed == (_REPO_ROOT / "src" / "celestia_devtools").resolve(), f"runtime checks resolved to {installed}, not this checkout"

def test_force_include_table_covers_every_data_file():
    forced = _force_include()
    # Positive control: an empty or renamed table must fail loudly here rather
    # than let the per-file loop below pass over nothing.
    assert len(forced) == len(FORCED_DATA)
    missing = [(src, dest) for src, dest in FORCED_DATA if forced.get(src) != dest]
    assert missing == []

def test_common_just_is_reachable_and_carries_recipes():
    _assert_package_is_this_checkout()
    path = common_just_path()
    assert path.is_file()
    text = path.read_text(encoding = "utf-8")
    # A truncation or a stub still satisfies is_file(); the staging path needs
    # the real recipe bodies that every consumer's justfile imports.
    assert "{{ _devtools }}" in text
    assert text.count("\nprefetch:") == 1

def test_bundled_p0_ledger_is_reachable_and_parses():
    _assert_package_is_this_checkout()
    path = bundled_ledger_path()
    assert path.is_file()
    entries = load_ledger(path)
    assert entries
    assert {entry.status for entry in entries} <= {"open", "closed"}
    assert all(entry.id and entry.title and entry.scope for entry in entries)

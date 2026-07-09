"""npx precompiled-package generator for celestia-island Rust crates.

Turns a cross-compiled binary into a distributable npm package that users run
via ``npx @celestia-island/<pkg>`` — no Rust toolchain required.

Two-layer package model (the standard Rust-binary-on-npm pattern):

  - **Platform subpackages** — one per ``rust_target``, e.g.
    ``@celestia-island/shirabe-linux-x64``, each containing only that target's
    binary. These carry the real bytes.
  - **Root package** — ``@celestia-island/shirabe``, with ``optionalDependencies``
    on every platform subpackage and a small ``bin/`` shim + ``postinstall``
    hook that picks the platform subpackage matching the host at runtime.

The generator only *writes* ``package.json`` files and stages the binary into a
platform subpackage ``dist/``. It is deliberately free of network calls so it is
unit-testable; actual ``npm publish`` happens in CI.
"""

from celestia_devtools.npm.platforms import Platform, PLATFORMS
from celestia_devtools.npm.packager import (
    generate_root_package,
    generate_subpackage,
)

__all__ = [
    "Platform",
    "PLATFORMS",
    "generate_root_package",
    "generate_subpackage",
]

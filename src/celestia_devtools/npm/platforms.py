"""Platform matrix for npx binary distribution.

Maps each Rust cross-compilation target we publish to:

  - a ``npm_suffix`` appended to the platform subpackage name
    (``@celestia-island/<pkg><npm_suffix>``, e.g. ``-linux-x64``)
  - the ``os``/``cpu`` pair Node uses to select the subpackage at install time
    (``process.platform`` / ``process.arch``)

The set mirrors the three-way matrix already used by ``shirabe/release.yml``
(linux-x64, macos-arm64, windows-x64) so the same generator serves every
celestia-island crate that opts into npx distribution.

Add an entry here to publish a new target; everything downstream (root
``optionalDependencies``, subpackage ``os``/``cpu`` filters, the host-selector
shim) is derived from this table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    """One publishable target triple and its npm packaging metadata."""

    #: Rust target triple (``rustup target add <this>``).
    rust_target: str
    #: Suffix appended to the base package name for the subpackage
    #: (e.g. ``-linux-x64`` → ``@celestia-island/shirabe-linux-x64``).
    npm_suffix: str
    #: ``process.platform`` value Node reports on this OS (``linux``/``darwin``/``win32``).
    node_os: str
    #: ``process.arch`` value Node reports on this CPU (``x64``/``arm64``).
    node_cpu: str
    #: Whether this is a musl (statically linked) variant. Node cannot
    #: distinguish glibc vs musl, so the install shim falls back to the musl
    #: package only when the glibc one is absent or the user opts in via env.
    musl: bool = False

    @property
    def subpackage_suffix(self) -> str:
        """The suffix including its leading dash (already present in ``npm_suffix``)."""
        return self.npm_suffix


#: The canonical publishable matrix. Order matters only for stable test output.
#:
#: The matrix covers the platforms a typical npm user runs on in 2026:
#:
#: - **linux-x64 / linux-arm64** — the two server CPU classes (x86_64 cloud +
#:   AWS Graviton / Ampere ARM).
#: - **linux-x64-musl** — Alpine and distroless containers (statically linked).
#: - **darwin-arm64** — Apple Silicon Macs (M1–M4).
#: - **darwin-x64** — Intel Macs (Rosetta cannot run arm64 binaries on x64).
#: - **win32-x64** — Windows on Intel/AMD.
PLATFORMS: tuple[Platform, ...] = (
    Platform(
        rust_target="x86_64-unknown-linux-gnu",
        npm_suffix="-linux-x64",
        node_os="linux",
        node_cpu="x64",
    ),
    Platform(
        rust_target="aarch64-unknown-linux-gnu",
        npm_suffix="-linux-arm64",
        node_os="linux",
        node_cpu="arm64",
    ),
    Platform(
        rust_target="x86_64-unknown-linux-musl",
        npm_suffix="-linux-x64-musl",
        node_os="linux",
        node_cpu="x64",
        # Alpine/musl is still linux-x64 from Node's perspective; the suffix
        # distinguishes the dynamically-vs-statically linked variants so a user
        # on musl can opt in explicitly, while glibc users get the default.
        musl=True,
    ),
    Platform(
        rust_target="aarch64-apple-darwin",
        npm_suffix="-darwin-arm64",
        node_os="darwin",
        node_cpu="arm64",
    ),
    Platform(
        rust_target="x86_64-apple-darwin",
        npm_suffix="-darwin-x64",
        node_os="darwin",
        node_cpu="x64",
    ),
    Platform(
        rust_target="x86_64-pc-windows-msvc",
        npm_suffix="-win32-x64",
        node_os="win32",
        node_cpu="x64",
    ),
)


def find_platform(rust_target: str) -> Platform | None:
    """Look up a :class:`Platform` by its Rust target triple, or ``None``."""
    for p in PLATFORMS:
        if p.rust_target == rust_target:
            return p
    return None

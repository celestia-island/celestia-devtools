"""Pure package-manifest generators for the npx distribution model.

These functions build the ``dict`` representations of the two-layer npm package
manifest (root + per-platform subpackage). They are intentionally pure and
side-effect free so they can be unit-tested without touching the filesystem.

The generated manifests follow the conventional "optionalDependencies on
platform subpackages + a postinstall selector" pattern used by widely deployed
native-binary npm packages (esbuild, @biomejs/biome, turbo, etc.). Under that
model npm only downloads the one platform subpackage matching the host
(``optionalDependencies`` are filtered by ``os``/``cpu``), so users never pay
the bandwidth cost of binaries for other platforms.

Three artifacts are produced:

  * :func:`generate_root_package` — the root ``package.json`` (name ``@scope/pkg``).
  * :func:`generate_subpackage`  — a platform subpackage ``package.json``.
  * :func:`generate_postinstall_shim` — the root ``bin`` + ``postinstall`` JS
    that resolves the installed subpackage at runtime and execs its binary.
"""

from __future__ import annotations

from typing import Any

from celestia_devtools.npm.platforms import Platform

#: The published node/shim. Kept low to stay installable on old Node.
_MIN_NODE = ">=18"

#: Default license string for celestia-island native packages.
_DEFAULT_LICENSE = "SySL-1.0"

#: Filename of the runtime selector script dropped into the root package.
SHIM_FILENAME = "install.js"


def generate_root_package(
    *,
    scope: str,
    name: str,
    version: str,
    platforms,
    binary: str,
    description: str = "",
    license: str = _DEFAULT_LICENSE,
    repository: str | None = None,
    homepage: str | None = None,
) -> dict[str, Any]:
    """Build the root ``package.json`` as a dict.

    Parameters mirror the two-layer model: the root package depends, via
    ``optionalDependencies``, on one subpackage per platform. ``binary`` is the
    leaf filename (no extension) the shim execs — e.g. ``"shirabe"``; the shim
    adds ``.exe`` on Windows automatically.

    The root package owns a single ``bin`` entry named *name* that points at
    the generated :data:`SHIM_FILENAME`, which locates the platform subpackage
    actually installed on the host and spawns its binary.
    """
    scope = scope.lstrip("@")
    full_name = f"@{scope}/{name}"
    optional_deps = {
        f"@{scope}/{name}{p.npm_suffix}": version for p in platforms
    }

    pkg: dict[str, Any] = {
        "name": full_name,
        "version": version,
        "description": description or f"{name} — prebuilt binary (npx launcher)",
        "license": license,
        "bin": {name: SHIM_FILENAME},
        "scripts": {"postinstall": f"node {SHIM_FILENAME}"},
        "optionalDependencies": optional_deps,
        "engines": {"node": _MIN_NODE},
        "files": [SHIM_FILENAME],
    }
    if repository:
        pkg["repository"] = repository
    if homepage:
        pkg["homepage"] = homepage
    pkg["publishConfig"] = {"access": "public"}
    return pkg


def generate_subpackage(
    *,
    scope: str,
    name: str,
    version: str,
    platform: Platform,
    binary: str,
    description: str = "",
    license: str = _DEFAULT_LICENSE,
    repository: str | None = None,
) -> dict[str, Any]:
    """Build a single platform subpackage ``package.json`` as a dict.

    The subpackage carries only the native binary for its platform and is
    installed only where ``os``/``cpu`` match (so it is never downloaded on
    other hosts). ``binary`` is the leaf filename without extension; the
    packager stage renames the cross-compiled artifact to it (adding ``.exe``
    on Windows).
    """
    scope = scope.lstrip("@")
    full_name = f"@{scope}/{name}{platform.npm_suffix}"
    leaf = f"{binary}.exe" if platform.node_os == "win32" else binary

    pkg: dict[str, Any] = {
        "name": full_name,
        "version": version,
        "description": description or f"{name} prebuilt binary — {platform.rust_target}",
        "license": license,
        "os": [platform.node_os],
        "cpu": [platform.node_cpu],
        # The binary lives at the package root so the selector can find it by
        # a stable path: <subpackage dir>/<leaf>.
        "files": [leaf],
        "engines": {"node": _MIN_NODE},
    }
    if repository:
        pkg["repository"] = repository
    pkg["publishConfig"] = {"access": "public"}
    return pkg


def generate_postinstall_shim(
    *,
    scope: str,
    name: str,
    binary: str,
    platforms,
) -> str:
    """Generate the runtime JS selector script (the package's only JS file).

    The shim does two jobs:

      1. On ``postinstall``: locate the platform subpackage that npm installed
         for this host and copy/verify its binary next to the root ``bin`` so
         the launcher path resolves.
      2. When invoked as the ``bin`` entry: spawn that binary with the
         forwarded argv, inheriting stdio so it behaves exactly like the
         native tool.

    It is written defensively (no optional-chaining for max Node compat, no
    dependencies) and resolves the subpackage by trying each candidate
    platform suffix against the host's ``process.platform``/``process.arch``.
    """
    scope = scope.lstrip("@")
    # Build a JSON-ish lookup the JS can consume. We embed it as a JS array of
    # objects to avoid requiring JSON.parse at module scope on every require.
    entries = ",\n".join(
        "    { suffix: %r, os: %r, cpu: %r }"
        % (p.npm_suffix, p.node_os, p.node_cpu)
        for p in platforms
    )
    return _SHIM_TEMPLATE.format(
        scope=scope,
        name=name,
        binary=binary,
        entries=entries,
        shim=SHIM_FILENAME,
    )


#: Template for the selector/launcher script. Kept dependency-free and ES5-ish.
_SHIM_TEMPLATE = """\
#!/usr/bin/env node
// AUTO-GENERATED by celestia-devtools npm-dist. Do not edit by hand.
//
// {shim} — locates the installed platform subpackage for @{scope}/{name} on
// the current host and either (a) stages its binary during postinstall, or
// (b) launches it as the {binary} command when invoked via the root `bin`.
"use strict";

var path = require("path");
var fs = require("fs");
var child = require("child_process");

var SCOPE = "@{scope}";
var NAME = "{name}";
var BINARY = "{binary}";            // leaf name, no extension
var IS_WIN = process.platform === "win32";
var EXE = IS_WIN ? BINARY + ".exe" : BINARY;

// Platforms this root package knows about. Order is irrelevant; the host
// matches at most one.
var PLATFORMS = [
{entries}
];

function rootDir() {{
  // __dirname is the root package install location.
  return __dirname;
}}

function matchPlatform() {{
  for (var i = 0; i < PLATFORMS.length; i++) {{
    var p = PLATFORMS[i];
    if (p.os === process.platform && p.cpu === process.arch) {{
      return p;
    }}
  }}
  return null;
}}

function tryResolveSubpackageDir() {{
  // The platform subpackage is an optionalDependency of this root package, so
  // npm hoists it into node_modules under this package's install dir. Try the
  // standard resolutions in order; if none resolve, fall back to scanning
  // node_modules for a matching suffix.
  var candidates = [];
  var p = matchPlatform();
  if (!p) return null;
  var subName = SCOPE + "/" + NAME + p.suffix;
  var root = rootDir();

  candidates.push(path.join(root, "node_modules", subName, EXE));
  candidates.push(path.join(root, "..", subName, EXE));
  // npm v7+ hoist under the top-level node_modules.
  candidates.push(path.join(root, "..", "..", subName, EXE));

  for (var i = 0; i < candidates.length; i++) {{
    try {{
      if (fs.existsSync(candidates[i])) return candidates[i];
    }} catch (e) {{ /* keep scanning */ }}
  }}

  // Last resort: scan sibling @scope dirs for a suffix matching this host.
  try {{
    var dirs = [
      path.join(root, "node_modules", SCOPE),
      path.join(root, "..", SCOPE),
      path.join(root, "..", "..", SCOPE),
    ];
    for (var d = 0; d < dirs.length; d++) {{
      if (!fs.existsSync(dirs[d])) continue;
      fs.readdirSync(dirs[d]).forEach(function (entry) {{
        if (entry.indexOf(NAME) === 0) {{
          var candidate = path.join(dirs[d], entry, EXE);
          try {{
            if (fs.existsSync(candidate)) candidates.push(candidate);
          }} catch (e) {{ /* ignore */ }}
        }}
      }});
    }}
  }} catch (e) {{ /* ignore */ }}

  // Re-scan the (possibly extended) candidate list.
  for (var j = 0; j < candidates.length; j++) {{
    try {{
      if (fs.existsSync(candidates[j])) return candidates[j];
    }} catch (e) {{ /* keep scanning */ }}
  }}
  return null;
}}

function stage() {{
  // postinstall: ensure the binary is executable (unix) and present.
  var bin = tryResolveSubpackageDir();
  if (!bin) {{
    // Not fatal: the host may simply not match any optionalDependency.
    process.exit(0);
  }}
  try {{
    if (!IS_WIN) fs.chmodSync(bin, 0o755);
  }} catch (e) {{ /* best effort */ }}
}}

function launch() {{
  var bin = tryResolveSubpackageDir();
  if (!bin) {{
    var p = matchPlatform();
    var host = process.platform + "-" + process.arch;
    process.stderr.write(
      SCOPE + "/" + NAME + ": no prebuilt binary for " + host +
      ". This package ships binaries for: " +
      PLATFORMS.map(function (p) {{ return p.os + "-" + p.cpu; }}).join(", ") +
      ".\\n"
    );
    process.exit(1);
  }}
  var childProc = child.spawn(bin, process.argv.slice(2), {{
    stdio: "inherit",
    windowsHide: false,
    // On Windows, spawning a binary by path needs the shell to resolve it the
    // same way CreateProcess would a typed command; harmless on unix.
    shell: IS_WIN,
  }});
  childProc.on("error", function (err) {{
    process.stderr.write(String(err) + "\\n");
    process.exit(1);
  }});
  childProc.on("exit", function (code, signal) {{
    if (signal) process.kill(process.pid, signal);
    process.exit(code == null ? 1 : code);
  }});
}}

var invoked = path.basename(process.argv[1] || "");
if (invoked === "{shim}" && process.env.npm_lifecycle_event === "postinstall") {{
  stage();
}} else {{
  launch();
}}
"""

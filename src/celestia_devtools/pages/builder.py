"""Build a custom-page artifact for one of the three provenance lanes.

A *lane* is where the page came from, which is exactly what the server records in
``custom_pages.mode``:

``raw-html``
    Hand-written HTML. Needs nothing but Python, so this lane always works — it is
    the one a submitter uses to test the hosting pipe itself.

``built-hikari``
    A Vue page authored against the hikari design system, bundled by vite. The
    hikari npm package ships **TypeScript sources** (no prebuilt ESM), so a
    bundler is not optional; this lane reuses the toolchain hikari itself pins
    rather than installing anything.

``built-tairitsu``
    A Rust ``rsx!`` component compiled to ``wasm32-wasip2``, transpiled by jco,
    and served with the browser-glue runtime. This is the lane whose glue had to
    be fixed upstream; the assembly here is the same five steps the spike proved.

Every lane writes a directory plus ``page.json`` — the manifest the publish step
uploads as its claim: per-file sha256, the canonical content hash, the declared
``rpc_scopes``, the builder identity and (for the built lanes) the lock digest.
Nothing here reaches for the network: a missing toolchain is an error, not a
download.
"""

from __future__ import annotations
import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from . import contract

#: What each lane writes and which tool it needs.
LANES: dict[str, dict[str, str]] = {"raw-html": {"needs": "", "builder": "celestia-devtools/pages (raw)"}, "built-hikari": {"needs": "node", "builder": "celestia-devtools/pages (hikari+vite)"}, "built-tairitsu": {"needs": "cargo", "builder": "celestia-devtools/pages (tairitsu+jco)"}}

#: The child half of the page contract (dossier §5.2), shared by every lane so a
#: page cannot forget to announce itself or to answer the theme handshake.
PAGE_RUNTIME_JS = """\
// page-runtime.js — the guest half of the custom-page contract.
//
// The panel owns the frame; the page only answers. Three messages matter:
//   panel -> page  init  {nonce, pageId, theme, locale, caps}
//   page  -> panel ready {}                       (sent once, on load)
//   page  -> panel rpc   {nonce, method, params}  (only for declared scopes)
//   panel -> page  rpc-result {id, ok, value|error}
// and the theme handshake may be re-sent at any time to re-skin a live page.
"use strict";

const state = { nonce: null, pageId: null, caps: [], theme: {}, locale: null };
window.__page = state;

function applyTheme(theme) {
  if (!theme || typeof theme !== "object") return;
  state.theme = theme;
  for (const [key, value] of Object.entries(theme)) {
    if (typeof key === "string" && key.startsWith("--")) {
      document.documentElement.style.setProperty(key, String(value));
    }
  }
  document.documentElement.setAttribute("data-theme-applied", "1");
}

function applyLocale(locale) {
  if (typeof locale !== "string" || !locale) return;
  state.locale = locale;
  document.documentElement.lang = locale;
  for (const node of document.querySelectorAll("[data-i18n-key]")) {
    const key = node.getAttribute("data-i18n-key");
    const table = window.__pageStrings || {};
    if (key && typeof table[key] === "string") node.textContent = table[key];
  }
}

window.addEventListener("message", (event) => {
  const data = event.data;
  if (!data || typeof data !== "object" || typeof data.type !== "string") return;
  if (data.type === "init") {
    state.nonce = data.nonce || null;
    state.pageId = data.pageId || null;
    state.caps = Array.isArray(data.caps) ? data.caps : [];
    applyTheme(data.theme);
    applyLocale(data.locale);
    document.documentElement.setAttribute("data-page-ready", "1");
  } else if (data.type === "theme") {
    applyTheme(data.theme);
    if (data.locale) applyLocale(data.locale);
  }
});

// Ask the panel for a method it declared it would relay (v1: view.fetch only).
let rpcId = 0;
window.__pageRpc = function (method, params) {
  if (!state.caps.includes(method)) {
    return Promise.reject(new Error("scope not declared for this page: " + method));
  }
  rpcId += 1;
  const id = rpcId;
  return new Promise((resolve, reject) => {
    const onReply = (event) => {
      const data = event.data;
      if (!data || data.type !== "rpc-result" || data.id !== id) return;
      window.removeEventListener("message", onReply);
      if (data.ok) resolve(data.value);
      else reject(new Error(data.error || "rpc failed"));
    };
    window.addEventListener("message", onReply);
    parent.postMessage({ type: "rpc", id, nonce: state.nonce, method, params }, "*");
  });
};

parent.postMessage({ type: "ready" }, "*");
"""

RAW_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0; padding: 20px;
    background: var(--hi-color-background, #0f1720);
    color: var(--hi-color-text-primary, #e6eef5);
    font: 14px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .card {
    max-width: 520px; padding: 18px 20px; border-radius: 10px;
    border: 1px solid var(--hi-color-border, #2a3a49);
    background: var(--hi-color-surface, #16202b);
  }
  h1 { margin: 0 0 8px; font-size: 17px; color: var(--hi-color-primary, #146e74); }
  p { margin: 0 0 8px; color: var(--hi-color-text-secondary, #9fb3c4); }
  code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: #7fd1c0; }
  #bridge-out { margin-top: 10px; }
</style>
</head>
<body>
  <div class="card" data-page-marker="raw-html">
    <h1 data-i18n-key="title">__TITLE__</h1>
    <p data-i18n-key="body">Hand-written HTML, hosted by chest as a custom page.</p>
    <p>locale: <code id="locale">en</code> · page: <code id="page-id">unknown</code></p>
    <p><button id="bridge" type="button">ask the panel (view.fetch)</button></p>
    <pre id="bridge-out"></pre>
  </div>
  <script src="./page-runtime.js"></script>
  <script src="./probe.js"></script>
  <script>
    // A declared-scope call and a refused one, side by side: the first reaches
    // the panel, the second must be rejected locally before it is even sent.
    document.getElementById("bridge").addEventListener("click", async () => {
      const out = document.getElementById("bridge-out");
      try {
        const value = await window.__pageRpc("view.fetch", { path: "/api/health" });
        out.textContent = "view.fetch ok: " + JSON.stringify(value).slice(0, 160);
      } catch (error) {
        out.textContent = "view.fetch failed: " + String(error);
      }
    });
    setInterval(() => {
      const el = document.getElementById("locale");
      if (el && window.__page.locale) el.textContent = window.__page.locale;
      const pid = document.getElementById("page-id");
      if (pid && window.__page.pageId) pid.textContent = window.__page.pageId;
    }, 200);
  </script>
</body>
</html>
"""

#: The isolation probe: the same negative control the spike proved, shipped with
#: every artifact so the e2e can read the verdicts out of the page itself.
PAGE_PROBE_JS = """\
// probe.js — isolation self-report. The panel can read these verdicts.
"use strict";
const report = {};
function attempt(name, fn) {
  try {
    const value = fn();
    report[name] = { outcome: "allowed", value: String(value).slice(0, 120) };
  } catch (error) {
    report[name] = { outcome: "blocked", error: String(error && error.name) };
  }
}
attempt("read_parent_dom", () => parent.document.body.innerHTML);
attempt("read_parent_title", () => parent.document.title);
attempt("read_host_cookie", () => document.cookie);
attempt("read_host_localstorage", () => localStorage.getItem("chest.spike.hostOnly"));
attempt("read_host_sessionstorage", () => sessionStorage.getItem("chest.spike.hostOnly"));
attempt("read_host_global", () => parent.__hostSecret);
attempt("navigate_top_hash", () => { top.location.hash = "page-escape"; return "assigned"; });
attempt("open_popup", () => { if (window.open("./popup.html", "_blank") === null) throw new Error("blocked"); return "opened"; });
attempt("read_frame_ancestor_url", () => parent.location.href);
window.__isolation = report;
fetch("/p/raw/ping.json", { credentials: "include" }).then(
  () => { report.fetch_host_with_credentials = { outcome: "allowed" }; },
  (error) => { report.fetch_host_with_credentials = { outcome: "blocked", error: String(error && error.name) }; },
);
"""

def _unescape(template: str) -> str:
    """Undo the ``{{``/``}}`` escaping the templates carry for readability.

    They are plain strings (never passed through ``str.format``), so without this
    the doubled braces land in the emitted vite config / Cargo.toml and the tool
    fails with a JSON/TOML parse error far from the cause.
    """
    return template.replace("{{", "{").replace("}}", "}")

def _run(command: list[str], cwd: pathlib.Path | None = None) -> str:
    finished = subprocess.run(command, cwd = cwd, capture_output = True, text = True)

    if finished.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed ({finished.returncode}): {finished.stderr.strip()[:600]}")

    return finished.stdout

def _read_lock_digest(root: pathlib.Path) -> str | None:
    """Digest of the dependency lock, so a rebuild is auditable.

    ``Cargo.lock`` / ``package-lock.json`` / ``pnpm-lock.yaml`` — whichever the
    lane produced. ``None`` means the lane has no lock (raw HTML).
    """

    for name in ("Cargo.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
        candidate = root / name

        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()

    return None

def _write_manifest(out_dir: pathlib.Path, artifact: contract.validated_artifact, mode: str, title: str, builder: str, rpc_scopes: list[str], lock_digest: str | None) -> dict:
    manifest = {"schema": 1, "title": title, "mode": mode, "entry": artifact.entry, "builder": builder, "deps_lock_hash": lock_digest, "rpc_scopes": rpc_scopes, "content_hash": artifact.content_hash, "size_bytes": artifact.size_bytes, "files": [{"path": item.path, "sha256": item.sha256, "size": item.size}for item in artifact.files]}
    (out_dir / "page.json").write_text(json.dumps(manifest, indent = 2) + "\n", encoding = "utf-8")
    return manifest

def build_raw(out_dir: pathlib.Path, title: str) -> dict:
    """Lane ``raw-html``: emit the hand-written page. Always offline."""
    out_dir.mkdir(parents = True, exist_ok = True)
    (out_dir / "index.html").write_text(RAW_INDEX_HTML.replace("__TITLE__", title), encoding = "utf-8")
    (out_dir / "page-runtime.js").write_text(PAGE_RUNTIME_JS, encoding = "utf-8")
    (out_dir / "probe.js").write_text(PAGE_PROBE_JS, encoding = "utf-8")
    return {"lane": "raw-html", "tool": "none"}

HIKARI_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>__TITLE__</title></head>
<body><div id="app" data-page-marker="built-hikari"></div>
<script type="module" src="/src/main.ts"></script>
<script src="./page-runtime.js"></script>
<script src="./probe.js"></script>
</body></html>
"""

HIKARI_MAIN_TS = """\
// The hikari lane's entry: the design system consumed exactly as the panel does
// (npm package, TypeScript sources) and mounted into #app.
import { createApp, h } from "vue";
import { HButton, HCard } from "@celestia-island/hikari";
import "@celestia-island/hikari/styles";

const App = {
  render() {
    return h("div", { class: "card", "data-page-marker": "built-hikari" }, [
      h("h1", {}, "__TITLE__"),
      h("p", {}, "Built from the hikari design system, bundled by vite."),
      h(HCard, { title: "hikari" }, () => h("div", {}, "a real HCard")),
      h(HButton, { label: "HButton" }),
    ]);
  },
};

createApp(App).mount("#app");
"""

VITE_CONFIG = """\
import {{ defineConfig }} from "vite";
import vue from "@vitejs/plugin-vue";
import vueJsx from "@vitejs/plugin-vue-jsx";

export default defineConfig({{
  root: __ROOT__,
  base: "./",
  cacheDir: __CACHE__,
  plugins: [vue(), vueJsx()],
  resolve: {{
    alias: [
      {{ find: /^@celestia-island\\/hikari\\/styles$/, replacement: __HIKARI_SRC__ + "/styles/index.scss" }},
      {{ find: /^@celestia-island\\/hikari$/, replacement: __HIKARI_SRC__ + "/index.ts" }},
    ],
  }},
  build: {{
    outDir: __OUT__,
    emptyOutDir: true,
    target: "es2022",
    cssCodeSplit: false,
    rollupOptions: {{ output: {{ entryFileNames: "assets/app.js", assetFileNames: "assets/app.[ext]" }} }},
  }},
}});
"""

def build_hikari(out_dir: pathlib.Path, title: str, toolchain: pathlib.Path, hikari_src: pathlib.Path) -> dict:
    """Lane ``built-hikari``: vite-build a hikari page with hikari's own toolchain.

    ``toolchain`` is a ``node_modules`` tree that already contains vite, vue,
    ``@vitejs/plugin-vue``, ``@vitejs/plugin-vue-jsx`` and sass — in practice
    ``hikari/packages/vue/node_modules``. Nothing is installed here: a missing
    toolchain is an error, because a page builder that silently reaches for the
    network is a page builder that breaks on a build host.
    """
    vite_bin = toolchain / "vite" / "bin" / "vite.js"

    if not vite_bin.is_file():
        raise RuntimeError(f"hikari lane needs vite in {toolchain} (looked for {vite_bin}); pass --hikari-toolchain")

    if not (hikari_src / "index.ts").is_file():
        raise RuntimeError(f"hikari lane needs the package sources at {hikari_src}; pass --hikari-src")

    work = pathlib.Path(tempfile.mkdtemp(prefix = "page-hikari-"))
    project = work / "project"
    (project / "src").mkdir(parents = True)
    (project / "index.html").write_text(HIKARI_INDEX_HTML.replace("__TITLE__", title), encoding = "utf-8")
    (project / "src" / "main.ts").write_text(HIKARI_MAIN_TS.replace("__TITLE__", title), encoding = "utf-8")
    (project / "page-runtime.js").write_text(PAGE_RUNTIME_JS, encoding = "utf-8")
    (project / "probe.js").write_text(PAGE_PROBE_JS, encoding = "utf-8")
    node_modules = project / "node_modules"
    node_modules.symlink_to(toolchain)
    config = project / "vite.config.mjs"
    rendered = _unescape(VITE_CONFIG)
    rendered = rendered.replace("__ROOT__", json.dumps(str(project)))
    rendered = rendered.replace("__CACHE__", json.dumps(str(work / "vite-cache")))
    rendered = rendered.replace("__OUT__", json.dumps(str(work / "dist")))
    rendered = rendered.replace("__HIKARI_SRC__", json.dumps(str(hikari_src)))
    config.write_text(rendered, encoding = "utf-8")
    _run(["node", str(vite_bin), "build", "--config", str(config)], cwd = project)
    built = work / "dist"
    out_dir.mkdir(parents = True, exist_ok = True)

    for item in sorted(built.rglob("*")):
        if item.is_file():
            target = out_dir / item.relative_to(built)
            target.parent.mkdir(parents = True, exist_ok = True)
            shutil.copy2(item, target)

    for extra in ("page-runtime.js", "probe.js"):
        shutil.copy2(project / extra, out_dir / extra)

    return {"lane": "built-hikari", "tool": _run(["node", "--version"]).strip()}

TAIRITSU_BOOT_JS = """\
// boot.js — instantiate the page's component with no network access.
//
// jco's `-I sync` output exports `instantiate(getCoreModule, imports)`; the
// core modules are inlined as base64 (below) because the hosted document is
// served under `default-src 'none'`, where fetching them is refused.
import { INTERFACES, wasiImports } from "./glue/interfaces.js";
import { instantiate } from "./component-wrapper/__STEM__.js";

const CORE_B64 = JSON.parse(document.getElementById("core-wasm-b64").content.textContent);
const COMPILED = new Map();

async function precompile() {
  for (const [name, b64] of Object.entries(CORE_B64)) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    COMPILED.set(name, await WebAssembly.compile(bytes));
  }
}

// jco calls this WITHOUT awaiting, so it must be synchronous.
function getCoreModule(path) {
  const name = String(path).split("/").pop();
  const module = COMPILED.get(name);
  if (!module) throw new Error("core module not precompiled: " + name);
  return module;
}

async function main() {
  const imports = { ...wasiImports };
  for (const [spec, exported] of Object.entries(INTERFACES)) {
    imports["tairitsu-browser:full/" + spec.replace("@tairitsu-glue/", "")] = exported;
  }
  await precompile();
  const exports = await instantiate(getCoreModule, imports);
  globalThis.__wasmExports = exports;
  if (typeof globalThis.__setWasmExports === "function") globalThis.__setWasmExports(exports);
  const entry = exports.run || exports.tairitsu_component_bootstrap ||
    (exports["tairitsu-browser:full/lifecycle@0.2.0"] || {}).start;
  if (typeof entry === "function") await entry();
  document.body.setAttribute("data-page-ready", "1");
}

main().catch((error) => {
  document.body.setAttribute("data-page-error", "1");
  console.error("[page] boot failed", error);
});
"""

TAIRITSU_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  body { margin: 0; padding: 20px; background: var(--hi-color-background, #0f1720);
         color: var(--hi-color-text-primary, #e6eef5); font: 14px/1.6 system-ui, sans-serif; }
  #app { max-width: 560px; }
</style>
</head>
<body>
  <div id="app" data-page-marker="built-tairitsu">boot: <code id="page-boot">starting</code></div>
  <template id="core-wasm-b64">__CORES_JSON__</template>
  <script type="module" src="./boot.js"></script>
  <script src="./page-runtime.js"></script>
  <script src="./probe.js"></script>
</body>
</html>
"""

TAIRITSU_GLUE_ENTRY = """\
// interfaces-entry.ts — tairitsu's own runtime registry plus the WASI subset a
// component imports. No adapters: the glue is complete upstream.
import "./handles";
import "./helpers";
import { INTERFACES } from "./registry";

import * as wasiCli from "@bytecodealliance/preview2-shim/cli";
import * as wasiClocks from "@bytecodealliance/preview2-shim/clocks";
import * as wasiIo from "@bytecodealliance/preview2-shim/io";
import * as wasiRandom from "@bytecodealliance/preview2-shim/random";

export const wasiImports: Record<string, unknown> = {
  "wasi:cli/environment": wasiCli.environment, "wasi:cli/exit": wasiCli.exit,
  "wasi:cli/stdin": wasiCli.stdin, "wasi:cli/stdout": wasiCli.stdout,
  "wasi:cli/stderr": wasiCli.stderr, "wasi:cli/terminal-input": wasiCli.terminalInput,
  "wasi:cli/terminal-output": wasiCli.terminalOutput, "wasi:cli/terminal-stdin": wasiCli.terminalStdin,
  "wasi:cli/terminal-stdout": wasiCli.terminalStdout, "wasi:cli/terminal-stderr": wasiCli.terminalStderr,
  "wasi:clocks/monotonic-clock": wasiClocks.monotonicClock, "wasi:clocks/wall-clock": wasiClocks.wallClock,
  "wasi:io/error": wasiIo.error, "wasi:io/poll": wasiIo.poll, "wasi:io/streams": wasiIo.streams,
  "wasi:random/random": wasiRandom.random, "wasi:random/insecure": wasiRandom.insecure,
  "wasi:random/insecure-seed": wasiRandom.insecureSeed,
};

export { INTERFACES };
"""

TAIRITSU_CRATE = """\
[package]
name = "__NAME__"
version = "0.1.0"
edition = "2021"
publish = false

[lib]
crate-type = ["cdylib", "rlib"]

[dependencies]
anyhow = "1"
tairitsu-vdom = {{ path = "__SRC__/packages/vdom" }}
tairitsu-macros = {{ path = "__SRC__/packages/macros" }}
tairitsu-web = {{ path = "__SRC__/packages/web", default-features = false, features = ["browser", "vdom", "wit-bindings"] }}

[profile.release]
opt-level = "z"
lto = true
codegen-units = 1
panic = "abort"
strip = true
"""

TAIRITSU_LIB_RS = """\
use tairitsu_macros::rsx;
use tairitsu_vdom::VNode;
use tairitsu_web::WitPlatform;

pub fn render() -> VNode {
    rsx! {
        div { class: "page",
            h1 { "__TITLE__" }
            p { "A Rust rsx! component, compiled to wasm32-wasip2 and hosted as a custom page." }
        }
    }
}

pub fn run_app() -> anyhow::Result<()> {
    let platform = WitPlatform::new()?;
    let vnode = render();
    let root = platform.mount_vnode_to_app(vnode.clone())?;
    tairitsu_web::init_runtime(root);
    Ok(())
}

#[unsafe(no_mangle)]
pub extern "C" fn run() {
    let _ = run_app();
}

/// Required by tairitsu-web: it references this symbol from the consumer crate.
#[unsafe(no_mangle)]
pub extern "C" fn tairitsu_component_bootstrap() {
    run();
}
"""

def build_tairitsu(out_dir: pathlib.Path, title: str, tairitsu_src: pathlib.Path, toolchain: pathlib.Path, work: pathlib.Path | None = None, cargo_home: pathlib.Path | None = None) -> dict:
    """Lane ``built-tairitsu``: the five-step wasip2 + jco pipeline.

    Each step is where a naive attempt breaks: (1) the rust target must exist and
    the build must stay offline; (2) ``-I sync`` is what makes the wrapper
    instantiable without an import map; (3) the core modules must be inlined
    because the hosted document forbids network fetches; (4) the browser glue is
    one same-origin module; (5) the page shell.
    """

    if not (tairitsu_src / "packages" / "web" / "Cargo.toml").is_file():
        raise RuntimeError(f"tairitsu lane needs a tairitsu checkout at {tairitsu_src}; pass --tairitsu-src")

    if shutil.which("jco") is None:
        raise RuntimeError("tairitsu lane needs jco on PATH (npm i -g @bytecodealliance/jco)")

    vite_bin = toolchain / "vite" / "bin" / "vite.js"

    if not vite_bin.is_file():
        raise RuntimeError(f"tairitsu lane needs vite for the glue bundle (looked for {vite_bin})")

    base = work or pathlib.Path(tempfile.mkdtemp(prefix = "page-tairitsu-"))
    crate = base / "crate"
    (crate / "src").mkdir(parents = True)
    crate_name = "page_artifact"
    stem = "page_artifact"
    (crate / "Cargo.toml").write_text(_unescape(TAIRITSU_CRATE).replace("__NAME__", crate_name).replace("__SRC__", str(tairitsu_src)), encoding = "utf-8")
    (crate / "src" / "lib.rs").write_text(TAIRITSU_LIB_RS.replace("__TITLE__", title), encoding = "utf-8")
    target_dir = base / "target"
    environment = dict(os.environ, CARGO_TARGET_DIR = str(target_dir))

    if cargo_home is not None:
        # Point cargo at the shared registry cache: an offline build cannot
        # resolve the wasm target's dependency graph from an empty home.
        environment["CARGO_HOME"] = str(cargo_home)

    command = ["cargo", "build", "--release", "--target", "wasm32-wasip2", "--offline"]

    if (crate / "Cargo.lock").is_file():
        command.insert(2, "--locked")

    finished = subprocess.run(command, cwd = crate, capture_output = True, text = True, env = environment)

    if finished.returncode != 0:
        raise RuntimeError(f"cargo build failed: {finished.stderr.strip()[:600]}")

    wasm = target_dir / "wasm32-wasip2" / "release" / f"{stem}.wasm"
    wrapper = base / "component-wrapper"
    wrapper.mkdir(parents = True)
    _run(["jco", "transpile", str(wasm), "-I", "sync", "-o", str(wrapper), "--no-typescript", "-q"])
    cores = {}

    for core in sorted(wrapper.glob("*.wasm")):
        cores[core.name] = base64.b64encode(core.read_bytes()).decode("ascii")
        core.unlink()

    # Glue bundle: tairitsu's own runtime registry, one ES module.
    staging = base / "glue-src"
    staging.mkdir(parents = True)

    for item in sorted((tairitsu_src / "packages" / "browser-glue" / "src" / "runtime").glob("*.ts")):
        shutil.copy2(item, staging / item.name)

    (staging / "interfaces-entry.ts").write_text(TAIRITSU_GLUE_ENTRY, encoding = "utf-8")
    (staging / "node_modules").symlink_to(toolchain)
    glue_out = base / "glue"
    config = staging / "vite.config.mjs"
    config.write_text("import { defineConfig } from \"vite\";\n" "export default defineConfig({ root: " + json.dumps(str(staging)) + ", cacheDir: " + json.dumps(str(base / "vite-cache")) + ", build: { outDir: " + json.dumps(str(glue_out)) + ", emptyOutDir: true, target: \"es2022\", lib: { entry: " + json.dumps(str(staging / "interfaces-entry.ts")) + ", formats: [\"es\"], fileName: () => \"interfaces.js\" } } });\n", encoding = "utf-8")
    _run(["node", str(vite_bin), "build", "--config", str(config)], cwd = staging)
    out_dir.mkdir(parents = True, exist_ok = True)
    shutil.copytree(wrapper, out_dir / "component-wrapper")
    # jco also emits TypeScript declarations (`interfaces/*.d.ts`); the page store
    # only serves whitelisted extensions, so the artifact must not carry them.
    pruned: list[str] = []

    for item in sorted((out_dir / "component-wrapper").rglob("*")):
        if item.is_file() and (contract.mime_for(item.name) is None):
            pruned.append(item.name)
            item.unlink()

    if pruned:
        print(f"  pruned {len(pruned)} non-servable helper file(s) from the wrapper")

    shutil.copytree(glue_out, out_dir / "glue")
    (out_dir / "boot.js").write_text(TAIRITSU_BOOT_JS.replace("__STEM__", stem), encoding = "utf-8")
    (out_dir / "page-runtime.js").write_text(PAGE_RUNTIME_JS, encoding = "utf-8")
    (out_dir / "probe.js").write_text(PAGE_PROBE_JS, encoding = "utf-8")
    (out_dir / "index.html").write_text(TAIRITSU_INDEX_HTML.replace("__TITLE__", title).replace("__CORES_JSON__", json.dumps(cores)), encoding = "utf-8")
    return {"lane": "built-tairitsu", "tool": _run(["rustc", "--version"]).strip(), "component_bytes": wasm.stat().st_size, "deps_lock_hash": _read_lock_digest(crate)}

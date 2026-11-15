"""Custom-page tooling: build a page artifact and publish it through chest.

The custom-page lane (chest P1) hosts a *static artifact* — a hikari/Vue build, a
tairitsu (wasm) build, or hand-written HTML — under a capability URL
``/__pages/{page id}/{content hash}/{path}`` and shows it in a panel tab.

This package is the submitter side of that contract:

* :mod:`celestia_devtools.pages.contract` mirrors the server's validation matrix
  (paths, MIME whitelist, caps) and, most importantly, the **canonical manifest
  hash** — the same artifact must hash to the same version directory, and a tab
  reference is the version-pinned path ``{page_id}/{content_hash}/{entry}``.
* :mod:`celestia_devtools.pages.builder` produces an artifact directory plus its
  per-file sha256 manifest for the three provenance lanes.
* :mod:`celestia_devtools.pages.client` walks ``pages.create`` -> ``pages.upload``
  -> ``pages.publish`` over chest's JSON-RPC surface.

Everything is offline-capable: the raw-HTML lane needs nothing but Python, and the
two built lanes shell out to toolchains that must already be on disk (the build
fails with an explicit message rather than reaching for the network).
"""

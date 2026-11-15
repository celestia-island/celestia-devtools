# `celestia-devtools pages`

Submitter-side tooling for chest's custom pages (the hosted-artifact lane): build an
artifact for one of three provenance lanes, check it against the host's matrix, publish it,
and print the version-pinned reference a panel tab pins.

```bash
celestia-devtools pages build   --lane raw-html --title demo --out /tmp/page
celestia-devtools pages verify  --artifact /tmp/page --chest-root ../shittim-chest
celestia-devtools pages publish --artifact /tmp/page --base https://… --token-file ~/.page-token
```

## Honest notes

**Where the bearer comes from.** `--token-file <path>` (0600; a looser mode warns) or
`$CELESTIA_PAGES_TOKEN` (which may itself name a file). There is deliberately **no `--token`
flag and no password flag**: a secret on argv leaks through `ps auxww`, shell history and CI
log echo. A token-shaped value passed to `--token-file` is refused with that explanation.

**A failed publish keeps the row and hands back its id.** `pages.create` runs before
`pages.upload`, so an upload refusal (quota, oversize, or a front that cuts the body) leaves
a `draft` row. The walk prints `{"page_id": …, "error": …, "retry": ["--page-id", …]}` and
does **not** delete the row: a partial upload is a state a retry needs to inspect, and
auto-deleting it could mask one.

**`deps_lock_hash` is an audit record, not a pin.** For the tairitsu lane the crate is
written without a lockfile, so `cargo build` resolves afresh and the `--locked` branch in
`builder.py` is unreachable in practice. The manifest records the digest of whatever lock the
build produced so two runs can be compared; it does **not** guarantee identical dependency
resolution. Treat the lane as reproducible-by-input, not lock-pinned.

**The upload ceiling is a measurement, not a derivation.** chest would accept 32 MiB (its
internal page budget, no chunking in P1), and the proxy front caps bodies at 16 MiB — but the
*public* faces refuse earlier: measured 2026-11-16, a **1.2 MB artifact publishes and a 2 MB one
is answered with nginx's `413 Failed to buffer the request body`**. The hop that decides it is
unidentified (the ruling was to document the measurement, not chase it), so `pages publish`
refuses locally at the measured ceiling — `MEASURED_REQUEST_BODY_CAP` — instead of streaming
bytes that die at the front door. `$CELESTIA_PAGES_BODY_CAP` (request-body bytes) lifts it for a
deployment known to admit more; the artifact cap is that body limit × 3/4, base64 inflation,
bounded by chest's 32 MiB. Always judge success on the parsed body, never the status: a 200 was
observed carrying a front's own HTML.

**`check_contract_drift` pins shapes, not behaviour.** It re-reads chest's
`custom_pages.rs` for the constants and the hash layout; it cannot prove the algorithms agree.
The publish walk's server-side hash cross-check is what actually catches a mismatch — which is
also why that field is now *required*: a reply without it is refused rather than trusted.

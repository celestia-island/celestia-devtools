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

**The upload ceiling is the smaller of two limits.** chest's own page budget is 32 MiB and
P1 has no chunking, but the deployment's front has a body limit too (base64 inflates by
4/3). Measured on the demo face 2026-11-16: a 700 KB artifact published, 900 KB was cut by
the edge (first as a TLS EOF from nginx's default 1m, then — after that was raised — as a
200 carrying the front's own HTML and a truncated stream). Override the assumption with
`$CELESTIA_PAGES_BODY_CAP` when a deployment differs, and check the *body*, not the status.

**`check_contract_drift` pins shapes, not behaviour.** It re-reads chest's
`custom_pages.rs` for the constants and the hash layout; it cannot prove the algorithms agree.
The publish walk's server-side hash cross-check is what actually catches a mismatch — which is
also why that field is now *required*: a reply without it is refused rather than trusted.

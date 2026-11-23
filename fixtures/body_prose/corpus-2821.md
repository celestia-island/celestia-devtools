## What this claims

G5 ④: closing a page tab and reopening it stacks nothing. G5 ⑤: the deployment triple-check is a
**go** for this wave's artefacts, with one recorded gap (no `.wasm` asset exists to check).

One new test, no production code change.

## G5 ④ — teardown, measured

Built chest page, real `HkQrCode`, Chrome `155.0.8059.39`, three mount/unmount cycles:

| round | canvases | timers | listeners | wasm compiles |
|---|---|---|---|---|
| before | 0 | 0 | 0 | 3 |
| mounted | 1 | 0 | 0 | 3 |
| after | 0 | 0 | 0 | 3 |

Every round is identical, and rounds 2–3 are identical to round 1 — nothing accumulates.

**The compile column is the load-bearing one, and it says what this cut does NOT claim.** It sits
at 3 in every row, including `before` round 1: those compiles happen while the module graph
evaluates, and mounting adds none. The host-client surface is a module-level singleton for the
page's lifetime, so "unmounting frees the wasm instance" would be a false statement. What returns
to baseline is what the panel owns — canvas, timers, listeners.

DOM nodes, stated as observed and **without** a mechanism: `before` read **10** in round 1 and
**12** in rounds 2–3, while the **post-unmount** count was **11 in every round**. Rounds 2 and 3
were identical, which is the no-accumulation evidence.

What caused the 10-vs-12 `before` difference was **not established**. Attributing it to the
harness's own bookkeeping is an inference, and an earlier draft asserted it as fact — round-3 review
pointed out that the harness is not in the tree, so that inference is unverifiable by anyone,
including me. Recorded here as unverified rather than dropped, so the reader knows the number moved
and that its cause is unknown.

## G5 ⑤ — deployment triple-check: **go**, one gap recorded

Checked against the existing lane (`build-and-deploy` 三件套), **no deployment performed**:

| check | status for this wave |
|---|---|
| ① 产物 hash == 构建产物 hash | **applies unchanged** — dist is the same content-hashed bundle; nothing wasm-specific to add |
| ② health 200 | **applies unchanged** — no new endpoint |
| ③ epoch changed + new assets resolve | **applies**; the asset walk is by path, extension-agnostic |
| MIME for `.wasm` | **`application/wasm`** (`static_files.rs:463`), pinned by `wasm_is_served_as_application_wasm` (`:1398`) |
| CSP on the production panel face | `script-src 'self' 'wasm-unsafe-eval'` (`api.rs:1607`, the no-GA default) — wasm compilation admitted |
| CSP on the sandboxed page face | `worker-src 'none'` (`api.rs:1371`) — main-thread only, pinned by `the_page_sandbox_tokens_are_load_bearing` |

**The gap: `find dist -name '*.wasm' | wc -l` → `0`.** The four first-party components embed their
wasm as base64 inside JS chunks, so no `.wasm` file reaches the deploy path at all. The MIME arm
and the asset walk are therefore **correct but unexercised** by this wave — they matter the moment a
component ships as a separate `.wasm` (or a third-party page does).

**Verdict: go.** No deployment-side change is required for this wave, so nothing here needs the
node-2/node-3 deploy-exclusive claim. If a later cut starts shipping real `.wasm` files, the asset
walk and the MIME arm both already handle them — but that wave should re-run this table rather
than inherit it.

## Review rounds

**R2 (independent, non-author) — BLOCKED**, one S1 + two S3, all fixed:

- *S1* the DOM-count gloss was wrong: the comment (and this body) explained away a "10→11 step" that
  is absent from the `12→14→11` sequence. Restated from the raw counts: `before` = 10 in round 1 and
  12 in rounds 2–3; post-unmount = 11 every round. Why `before` differs between rounds was **not
  established** — see §G5④ above, where that is recorded as an unverified inference rather than a
  cause.
- *S3* the test claimed to cover "listeners or timers" but counts only listeners. Scoped.
- *S3* the add/remove spies were installed in the file-wide `beforeEach`, swapping `window` methods
  for all 39 tests; now scoped to the one test with a `finally` restore.

R2 independently confirmed the test has teeth on three axes — wholesale removal, a subtle unpaired
listener, and the anti-vacuity guard all go red, and every line reference (`:463`, `:1398`,
`api.rs:1607`, `api.rs:1371`) is exactly right. It also reproduced `find dist -name '*.wasm' → 0`
and found `dist/` genuinely present in the worktree, and verified the base64 embedding by finding
`AGFzbQ` in a built chunk.

**R3 (independent, non-author) — CLEAN**, one S3: I had replaced one mechanism claim with another.
The "10→11 step" was gone, but "the harness appends a host div per round and leaves it" asserts a
cause nobody can check — the harness is scratch and not in the tree. R3 also caught that the implied
rule (`after = before − 1`) fits rounds 2–3 but not round 1, so it was an inference fitted to one
column. The prose now says the difference existed, that it was in the `before` reading only, and that
its cause was not established. R3 verified the mutation on two axes and confirmed the spy restore is
real (after this file's 39 tests, `window.addEventListener` is the native function again).

## Evidence

```
vitest src/components/panels/     Test Files 10 passed | Tests 149 passed
vitest CustomPageHost.test.tsx    Tests 39 passed
vue-tsc --noEmit                  rc=0
comment-gate                      4 claim line(s), all anchored
```

Mutation (`MUT:pages-host-listener-leak`): removing
`window.removeEventListener("message", onMessage)` from `onBeforeUnmount` reddens **exactly** the
new test (1 failed / 38 passed).

## No-test-and-why

The browser numbers above are not re-run by the suite: a unit test cannot mount a real page under a
real browser. The test pins the listener half, which is countable in jsdom; the canvas/timer/wasm
columns are recorded here as a one-off measurement. That is stated in the test's own comment.

verify: rounds=5 clean=3 S0=0 S1=1 S2=0 S3=7 gates=4 hardcap=no budget=exhausted@3 post-cap=0 c3=3



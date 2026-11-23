Stage 3 of the browser-host project (`_reports/tairitsu-hikari-wasi-browser-20261121/PLAN.md` §4), second cut: Mermaid follows QR (#1473) onto the tairitsu host client. Same three moves — side-effect capability module, namespace import, `createSurface` — and one shape difference the adapter owns (below).

> **Review history note.** Head is `17d8513c`. The first head, `d893df10`, was found by all three independent reviewers to call `createSurface` **without** the capability declaration, which made the sync door's refusal unreachable — an S1 that is fixed in `69ad45bf` and re-verified. Every claim below is stated for `17d8513c` and, where behaviour changed, the pre-fix state is named explicitly.

## What changed

| File | Change |
|---|---|
| `packages/vue/src/mermaid/declare-caps.ts` | New side-effect module: publishes the component's **empty** declaration, exports the published value. |
| `packages/vue/src/mermaid/wasmEngine.ts` | Drops the direct `parse` import; static namespace import + `createSurface` **with the declaration handed through** + `surface.call("parse", …)`; adds `mermaidReady` / `MermaidNotReadyError`. |
| `packages/vue/package.json` | `sideEffects` whitelist gains `**/mermaid/declare-caps.ts`. |
| `packages/vue/src/mermaid/hostSurface.test.ts` | New: pins the seam and the declaration wiring. |

Frozen golden (`wasmEngine.test.ts` 8 + `__golden.test.ts` 8, snapshots) is **byte-identical** — `git diff origin/master...HEAD` over those paths and `src/wasm/**` is empty (re-verified independently; blob hashes unchanged).

## The shape difference (why this adapter is not a copy of QR's)

QR's `encode` returns a WIT `result<qr-code, string>`, so its adapter unwraps by letting the wrapper throw. Mermaid's `parse(source) -> parse-result` is a **record**:

```wit
record parse-result { json: option<string> }
```

so this adapter unwraps by reading:

```ts
const result = surface.call<MermaidParseResult | undefined>("parse", normalized);
const json = result?.json;
if (json) { try { return JSON.parse(json) as MermaidDoc; } catch { return null; } }
return null;
```

Verified against the artifact's own decoder: `none` is emitted as `{json: undefined}` (key **present**, value `void 0`), so `result?.json` + `if (json)` is truthiness-identical to the pre-migration `result.json` + `if (result.json)`. No reachable input diverges. An absent `json` keeps the pre-migration meaning — "not recognized" → `null`, callers render a code block. The `wasmHandles` guard still answers *before* the component is consulted (pinned).

## The S1 that the reviewers found (and the fix)

`d893df10` called `createSurface(mermaidWrapper)` with the `capabilities` argument **omitted**. The client is `createSurface(module, capabilities = {}, glue = {}, wasi = {})`, and both `sync-door-cannot-provide` branches test a declaration that therefore defaulted to `{}` — so they were unreachable. The published declaration was decoration, and this PR's earlier claim that "a non-empty declaration would make `createSurface` throw" was **false** for `d893df10` (measured: a non-empty declaration parsed fine; only a literal `toEqual` noticed).

`69ad45bf` passes `MERMAID_CAPABILITIES` into the door (`17d8513c` is whitespace-only on top of it: trailing newlines, as master and the QR precedent have). Re-running the reviewers' mutation now fails structurally:

```
MermaidNotReadyError: the Mermaid component is not loaded; …
Caused by: HostClientError: sync-door-cannot-provide: createSurface cannot provide capabilities …
20 failed / 2 passed
```

The same shape exists in merged QR (`qrEncoder.ts:44`) and is filed as **#1475** rather than widened into this PR.

**One honest limit:** the declaration is **inert** for this artifact today. The artifact never reads `__TAIRITSU_CAPABILITIES__` (0 hits) — it gets WASI from its own `@bytecodealliance/preview2-shim/*` imports (`cli`, `clocks`, `io`, `random`). The wiring is future-proofing so a glue-bearing rewrite inherits a refusal instead of defaulting to everything granted; it is not a capability plane that is enforced today.

## Why a golden-blind seam needed its own test

Mutation: replacing `surface.call("parse", …)` with a direct `mermaidWrapper.parse(…)` (a form that compiles) **kept the frozen 16/16 green** — the bypass produces identical output. Measured again independently by two reviewers: frozen-only run under the mutant = `16 passed (16)`, while the full suite goes `2 failed | 19 passed` with `expected [] to have a length of 1 but got +0`.

`hostSurface.test.ts` observes the **real** surface (wraps the real `createSurface`, keeps the handle, then wraps that surface's `call`; `call` is an own writable property of a plain object literal — not brittle, verified in the client source). The engine code is untouched and the real artifact runs.

The two pins make different claims, deliberately:

- `expect(seam.handedCapabilities).toBe(MERMAID_CAPABILITIES)` is the **regression guard** — verified it is the only case that fails when the argument is dropped again (`expected undefined to be {interfaces: [], wasiModules: []}`);
- the case driving the real client with `{interfaces: ["document"]}` / `{wasiModules: ["random"]}` asserts `HostClientError`. It states the door's contract but passes even on a reverted engine (it calls `createSurface` directly) — that limit is now written in the test, not implied away.

## Claims, artifacts, enforcement

1. **Claims**: the engine reaches Mermaid only through the host call surface; the declaration reaches the door and a non-empty one is refused.
2. **Artifacts**: the pins above, the mutation table below, gate exit codes.
3. **Enforced by**: `gate` (test) for the seam and the declaration wiring. **Not** CI-enforced: `.github/workflows/ci-hosted.yml` is `workflow_dispatch` only, so it never runs vitest/`vue-tsc` on this PR — `gates=` counts local runs, nothing more. Disclosed because "green locally" is not "green in CI".

### `sideEffects` — corrected measurement

Earlier revisions of this body claimed a `2 → 0 → 2` count. **That was wrong**, and two reviewers measured differently again, so here is the reconciliation, all from real `vite 8.2` lib builds of the subpath entry (`src/mermaid/index.ts`, what chest consumes):

- Before the S1 fix, dropping the whitelist entry **did** remove the declaration from the bundle (the module was pure side effect then). The exact integer depended on the metric and on whether the client was external — `__TAIRITSU_CAPABILITIES__` counts, `publishCapabilities(` counts and full-bundle diffs disagreed, so no single number is quotable. **The qualitative claim is the one that holds: before the fix, dropping the entry dropped the declaration.**
- **After** the S1 fix the entry is **redundant**: the engine now consumes `MERMAID_CAPABILITIES` as a value (`createSurface(wrapper, MERMAID_CAPABILITIES)`), so the module is tree-shaken by *use*, not by side effect. Measured post-fix: present **2**, absent **2**, byte-identical bundles (`diff` empty, `sha256` equal — sizes measured `509,492` / `509,524` / `509,525` across different build configs and commits, so **the equality is the claim, not any single integer**).
- The entry is kept anyway: it is the guarantee that survives a refactor which stops reading the constant (which is exactly what the pre-fix shape was).

## Verification

verify: rounds=6 clean=3 S0=0 S1=1 S2=4 S3=10 gates=8 hardcap=yes budget=exhausted@3 c3=3

`c3=3`, reached at the hard cap — the accounting is unusual, so it is stated plainly:

- Rounds 1–3 reviewed `d893df10` and produced the S1 (the omitted `createSurface` declaration argument) and its text S2s; that S1 is fixed in `69ad45bf`. Those rounds **cannot** certify the head, so they do not count toward `c3`.
- **连三 was then re-earned on the head.** `17d8513c` has three consecutive independent CLEAN rounds: r5 (closing/seams — which found the wrong denominators; those were text-only S2, fixed and re-verified in the next round, so under the text-S2 exception they do not break the streak), then r6 (full diff + all six mutation rows reproduced exactly + 46/46 differential parity against the pre-migration engine over adversarial inputs) and r7 (closing/seams against the real client, the real artifact and chest). r6 and r7 are independent fresh-context agents; both ended `CLEAN` on this SHA with the shared worktree left clean.
- `rounds=6` is the hard cap (`hardcap=yes`) and `budget=exhausted@3` records that the 3-round budget was passed. **No further round is available** — every remaining correction here is prose-only, and the two CLEAN rounds above both ended on unchanged code.
- `S2=4` = the false "a non-empty declaration would make `createSurface` throw" claim (fixed by code, not wording) + the wrong mutation denominators/integer (fixed by re-measurement and re-verified by r6/r7). `S3=10` is the residual inventory across the PR's life, not a per-round delta.

Gate evidence on `17d8513c` (all from `packages/vue`; vitest **must** run there — from the repo root vite leaves JSX untransformed):

```
$ npx vitest run src/mermaid/
 ✓ src/mermaid/wasmEngine.test.ts (8 tests)
 ✓ src/mermaid/__golden.test.ts (8 tests)
 ✓ src/mermaid/hostSurface.test.ts (6 tests)
 Test Files  3 passed (3)
      Tests  22 passed (22)          rc=0

$ npx vue-tsc --noEmit               rc=0
  (negative control: a type error injected into wasmEngine.ts → TS2322, rc=2, then restored byte-identical)

$ pnpm install --frozen-lockfile
Lockfile is up to date, resolution step is skipped
Done in 64ms using pnpm v12.5.1      rc=0   (no dependency added: tairitsu-host-client@^0.1 already resolves 0.1.0)
  run by the author in the worktree; reviewers could not execute it under their READ-ONLY
  constraint, so they confirmed it structurally instead (lockfile untouched by the diff;
  the client was already pinned on origin/master)

$ npx vitest run                     # whole package
 Test Files  565 passed (565)
      Tests  6720 passed (6720)      rc=0
```

`6720` (not `6719`) is the whole package with the new pin file included.

Mutations, each restored and re-verified (`git status --porcelain` empty, HEAD unchanged):

| Mutation | Result |
|---|---|
| bypass: direct `mermaidWrapper.parse(…)` | **RED** `2 failed / 20 passed (22)` (`expected [] to have a length of 1 but got +0`); frozen 16 stayed green |
| remove the host loading path | **RED** `22 failed / 22` — no test can reach a component (`MermaidNotReadyError`) |
| drop the `createSurface` declaration argument | **RED** `1 failed / 21 passed` — the only red is the `handedCapabilities` pin (`expected undefined to be {interfaces: [], wasiModules: []}`) |
| declaration made non-empty `{interfaces:["document"]}` | **RED** `20 failed / 2 passed` (`HostClientError: sync-door-cannot-provide`) — pre-fix this was caught only by a literal `toEqual` |
| `wasmHandles` → `true` / `false` | **RED** both ways (`1 failed / 21 passed`, `17 failed / 5 passed`) |
| delete the `stateDiagram` rewrite | **RED** `1 failed / 21 passed` (`expected null not to be null`) — caught only by the new pin |

## chest assessment (criterion ⑧)

Zero direct dependency — no change needed downstream, no chest PR.

- Sole consumer `shittim-chest/packages/webui/src/main.ts:112` imports the **public subpath** `@celestia-island/hikari/mermaid` and registers `mermaidParser` at `:119`. It never touches `wasmEngine`, `src/wasm/**`, or `preview2-shim`.
- chest resolves the **published** `@celestia-island/hikari@0.55.456` (real `.pnpm` store path), declares `^0.55.456`. Its installed copy **predates** this PR (`wasmEngine.ts:14` still imports `parse` directly; no `declare-caps.ts`), so it is on the old path until its next bump — "picked up on the next bump", not "already migrated".
- `rg "wasm/mermaid|hikari_wasm|preview2-shim"` over chest `packages/` (excluding node_modules/dist) → **0 hits**.
- The consumed contract is unchanged: `mermaidParser.parse` (`mermaid/index.ts:50-57`) wraps in try/catch and returns `null` on any throw, so `MermaidNotReadyError` is observed exactly like the old throw. chest sees no behavioural change.

## Residuals

- S3 | `wasmEngine.ts` | `result?.json` optional chaining guards a `null` return the type says cannot happen; kept because the record is `option`-shaped and both reviewers judged it stylistic, not a correctness issue.
- S3 | `hostSurface.test.ts` | the `stateDiagram` rewrite is pinned only by a seam assertion — the frozen golden has no `stateDiagram` case. Accepted (the pin does catch it), noted so a refactor of that file does not silently drop it.
- S3 | `wasmEngine.ts` | `export { HostClientError }` is not re-exported by `mermaid/index.ts`, so it is unreachable from the documented door (the QR precedent has the same shape).
- S3 | upstream | `tairitsu-host-client` `dist/index.d.ts` omits `invoke` which its runtime returns (`call` delegates to it, where `stopped`/`no-export` are enforced). No effect here (we use `call`); another repo.
- S3 | QR (#1475) | merged QR has the identical omitted-declaration shape at `packages/vue/src/utils/qr/qrEncoder.ts:44` — filed as a tracked issue before this merge.
- S3 | PR body | The mutation table previously carried two wrong denominators (`2/19` and `20/1`) measured before the sixth pin existed, and a non-reproducible byte count — all three corrected here by re-measurement at 22 tests. Recorded rather than quietly fixed: the closing round's finding was about the body being the merge record.
- S3 | `declare-caps.ts` / `wasmEngine.ts` / `hostSurface.test.ts` | No trailing newline (fixed in `17d8513c`; master and the QR precedent both end with `\n`).
- out of scope | CI | `ci-hosted.yml` is dispatch-only, so nothing runs these gates automatically (pre-existing).

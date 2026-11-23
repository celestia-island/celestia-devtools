## What this claims

Chest's two QR surfaces run hikari's first-party **wasm** component, and this PR adds the
chest-side proof: the real component paints a real QR matrix, an empty payload degrades honestly,
and a refused encode keeps the card mounted.

**Mock disclosure (exact):** `vi.mock` is module-level, so all six tests import a mocked encoder
module. Five of them delegate straight through to the real `qrMatrix` and therefore run the real
encode; only the mid-life test arms the refusal. The mock's never-settling `qrReady` is also
load-bearing, but for a different reason than I first wrote: it prevents an **infinite redraw**
(the real `qrReady` resolves, so a refused paint would re-call `draw()` forever — measured as a
119 s hang), not a "race". And the mid-life test's discriminator is `refusal.armed`, not
`qrReady`: measured, `armed = false` with hikari's catch removed stays 6/6 green.

It also carries three **measurements** from this cut, two of which contradict the task's
assumptions. Please read §2 and §3 — they change what later cuts can assume.

## 声称绑定三问

1. **Claims**: the QR component encodes and paints inside chest's runtime; an empty payload is a
   card without a canvas; a mid-life refusal does not unmount the card. It does NOT claim the
   wrapper-evaluation failure is absorbed — see §3.
2. **Artifact**: 6 new webui tests (mutation-verified), `vue-tsc` rc=0, real-Chrome screenshots +
   pixel measurements recorded below.
3. **Enforces it**: `gate` — the tests pin each behaviour; each `MUT:` anchor names the mutation
   that reddens its test.

## §1 — What the runtime path actually is

`HkQrCode` → `qrEncoder.ts` → `createSurface` (tairitsu host-client **sync door**) → the jco
wrapper → the wasm component. The wrapper is a **static import**, and it carries the compiled
component as an **embedded base64 payload**.

## §2 — `instantiateStreaming` is UNREACHABLE (acceptance ②, answered)

The task asked to prove the streaming path or record why it was bypassed. Measured:

| component | `instantiateStreaming` | embedded base64 payload |
|---|---|---|
| qr | **0 occurrences** | yes |
| mermaid | 0 | yes |
| scene3d | 0 | yes |
| highlight | 0 | yes |

`grep -c "instantiateStreaming" hikari_wasm_qr.js` → **0**; `.wasm` URL references → **0**. The
wrapper compiles with `WebAssembly.compile(atob(<embedded base64>))`. Confirmed at build level:
**the chest `dist/` contains zero `.wasm` files**.

**Consequence for the MIME fix from the previous cut:** that fix is correct and still needed for
any future `.wasm` served over HTTP, but it has **no consumer in this path** — nothing in the
first-party component lane fetches a `.wasm` over the network today. Streaming would require
hikari to switch to a fetch + `instantiateStreaming` shape. Recorded rather than "fixed", because
changing the packaging is hikari's call, not chest's.

## §3 — The degrade contract has a hole, and it is upstream (acceptance ⑤, partially not met)

> **Filed upstream: hikari #1498** — the static wrapper import makes a wasm load failure blank the
> whole app. Scope, measured: **three of the four** first-party components import their wrapper
> statically — `qr` (`utils/qr/qrEncoder.ts:18`), `mermaid` (`mermaid/wasmEngine.ts:18`) and
> `scene3d` (`scene3d/wasmFleetSpec.ts:15`). `highlight` already uses a **dynamic**
> `loadComponent` (`composables/useHighlight.ts:69`) and is therefore not affected — it is the
> shape the other three should adopt.

Acceptance ⑤ asks: break the host-client load path → the panel falls back to a degraded form, not
a blank/crash. **Measured in real Chrome 155 against the actual built chest panel** (static server
over `dist/webui`, injecting a fault via `Page.addScriptToEvaluateOnNewDocument`):

| scenario | `#app` innerHTML | uncaught |
|---|---|---|
| control | **9041 bytes** — the login screen paints | 0 |
| wrapper evaluation fails | **0 bytes — blank page, no UI at all** | 1 |

Reproduced 3/3 deterministically.

**Root cause, located precisely:** `qrEncoder.ts:18` is a **static** `import * as qrWrapper`. ES
module evaluation happens *before* the module body, so a throw while the wrapper evaluates escapes
before the `try { createSurface(...) }` at line 47 can run. **The try/catch structurally cannot
catch it** — this is not a missing guard, it is an ordering property of static imports. Because
`main.ts` statically imports hikari, the failure takes the whole graph down.

Chest cannot fix this: there is no seam. `main.ts` mounts inside `router.isReady().then(...)`,
which never runs when the module graph fails. The fix is in hikari — put the wrapper behind a
dynamic `import()` so a load failure rejects `qrReady` instead of unwinding the graph.

**What this PR therefore does not claim:** that breaking the load path yields a degraded panel.
It claims the *absorbable* half (a runtime refusal keeps the card mounted) and records the
un-absorbable half with evidence so it can be filed against hikari.

## §4 — Theme judgment (acceptance ③, decided by evidence)

**Judgment: the QR is theme-INDEPENDENT on purpose, and chest must not theme it.**

Basis — `HkQrCode` documents "always dark modules on a white card (independent of the app theme)
so it stays scannable in dark mode"; its SCSS hardcodes `background: #ffffff`; it has no
`useTheme` and reads no theme token. Measured in Chrome with `data-theme=light|dark` applied the
way chest's theme loader does:

| theme | dark px | light px | background RGB |
|---|---|---|---|
| light | 20400 | 23421 | `[255,255,255]` |
| dark | 20400 | 23421 | `[255,255,255]` |

(On a 220×220 = 48400 px canvas, dark+light = 43821. The remaining 4579 are
the module-edge overlap: the component deliberately fills each cell at
`cell + 0.5` to avoid hairline seams on scaled canvases.)

The two screenshots are **byte-identical (same sha256)**. A themed QR would be inverted in dark
mode, which hurts scanner reliability — so "make it follow the theme" would be a regression. The
test pins that both themes still render, so a future change has to argue with a test.

## §5 — Real-browser evidence

Rendered by the wasm component in Chrome 155, 220×220 backing store:

- control: quiet zone + full QR (finders, timing, data modules), `aria-label="QR code"`,
  0 console errors, **0 `.wasm` network requests**;
- empty payload: card with `data-empty="true"` and no canvas.

## Evidence

```
vitest HkQrCode.runtime.test.tsx      Tests 6 passed (6)
vitest src/components/header/         Test Files 30 passed | Tests 209 passed
vue-tsc --noEmit                      rc=0
comment-gate                          29 claim line(s), all anchored
cred_scan                             0 hits
```

Mutation proof (`MUT:qr-draw-disabled`): disabling the recorder's `fillRect` push reddens **two**
tests — `draws a quiet-zone background plus many dark modules` AND the theme test (which also needs
painted fills for its colour assertions): **2 failed / 4 passed**.

## No-test-and-why

The wrapper-evaluation failure (§3) has **no chest test** by construction: it kills the module
graph before any test seam exists, and the honest demonstration is the real-browser measurement
above. Adding a chest test that mocked it would assert a handler chest does not have.

## Follow-ups for other repos

- **hikari #1498**: static wrapper import in `qrEncoder.ts`, `mermaid/wasmEngine.ts` and
  `scene3d/wasmFleetSpec.ts` makes a wasm load failure fatal to the whole app graph. Dynamic
  import + a `qrReady`-style rejection is the fix — `useHighlight.ts` already does exactly that.
- **device-plane `form` parity**: not touched — no consumer reads it on that plane yet, and this
  cut did not need it.

## Review rounds

**R5 (independent, non-author) — BLOCKED**, and it caught me introducing NEW false causalities in
the R4 fix itself:

- *S2* I wrote that the never-settling `qrReady` is "why the mid-life test discriminates". It is
  not. Measured 2×2: `armed=true` + catch removed → mid-life **red**; `armed=false` + catch
  removed → **6/6 green**. Arming is the discriminator; `qrReady` is not.
- *S2* I wrote that `qrReady` "keeps a refused paint from racing a real re-encode". Wrong failure
  mode: it prevents an **infinite redraw** (a resolving `qrReady` re-calls `draw()` forever —
  measured as a 119 s hang vs ~1 s).
- *S2* the body's "21 claim lines" was false at **every** commit (it is 27); my R3 "fix" from 13→21
  substituted one wrong number for another (27 at 68e55da87). Measured per commit: 21 (6c565f74d, 6dab77b3f) →
  27 (68e55da87) → **29 at HEAD**, which is the number the Evidence block above quotes and the
  one the gate reporting 29 agrees with.

All three corrected and re-measured by 2×2. R5 confirmed the substance is intact: all four anchors
red, `armed=false` → 6/6, the non-armed tests genuinely run the real encode (instrumented
`calls=1 armed=[false] dims=[33]`, drawing test records 576 fills = 1 background + 575 modules of a
33×33 version-4 matrix), gates green, and `209 header tests` correct.

**R4 (independent, non-author) — BLOCKED**, again on claims only. It re-verified all four anchors
and the R3 fix as exact (`MUT:qr-draw-disabled` → 2 failed / 4 passed, the two named tests), then
found two claim-accuracy defects:

- *S2* the body sold the suite as "no encoder mock" while the file claimed only the last test was
  mocked — **both wrong**: `vi.mock` is module-level, so all six import the mock (the reviewer
  measured `qrReady=pending`). Corrected to the exact disclosure above.
- *S2* the mid-life test claimed arming the refusal was what made it non-vacuous. Measured: with
  `refusal.armed = false` it **still passes 6/6**. What actually discriminates is the mock's
  never-settling `qrReady`; the test proves the decide-and-degrade *process*, not that a permitted
  refusal stays silent. Both limits are now stated in the test.
- *S3* two comment paragraphs were fused onto one line; reflowed.

**R3 (independent, non-author) — BLOCKED**, on claims only, not on the suite's substance:

- *S2* my R2 fix commit introduced a **new false measurement**: the comment said
  `MUT:qr-draw-disabled` "fails exactly that test and no other". It fails **two** (the drawing test
  and the theme test, which also needs painted fills). I had measured the 2-red result earlier and
  still wrote "no other" — the exact defect class this round exists to catch. Corrected, and the
  corrected wording re-measured.
- *S3* two stale body numbers (comment-gate 13 → **21**; a mutation tally summing to 4 of 6 →
  **2 red / 4 green**).

R3 independently re-verified the R2 fixes **by mutation**, and went further than the fix required:
it instrumented the mocked `qrReady` and measured `thenRegistrations=1, catchRegistrations=0`,
proving the catch branch genuinely executes (not just that the test passes). It also proved the
"does not prove wasm" disclaimer is accurate by measuring `WebAssembly.compile/instantiate` across
a static import with **no** `qrMatrix` call: `afterImport={"compile":3,"instantiate":3}`. And it
confirmed a pure-TS fake `qrMatrix` passes the drawing test — which is exactly what the disclaimer
says. Theme judgment: **correct**, verified against hikari 0.55.463 source.

**R2 (independent, non-author) — BLOCKED. Two anchors were false and one test was vacuous. All
fixed, each proven by mutation.**

1. *S1 — the mid-life test was vacuous.* It mounted a healthy encoder and asserted only
   `classList.contains("hk-qr-card")`, a class the component emits unconditionally. The reviewer
   replaced the whole body with a blank-payload mount and still got 6/6 green; I reproduced that.
   Fixed: the refusal is now forced through the encoder module the component really calls (a path
   built from `import.meta.dirname` so it survives a hikari bump — a pnpm-store literal would not),
   and `vitest`'s hoisting constraints are documented. Removing `HkQrCode`'s `QrNotReadyError`
   catch now reddens **exactly** that test (1 failed / 5 passed).
2. *S2 — the theme test was too weak.* It only asserted "a canvas exists in both themes", so a
   theme-aware QR stayed green (reviewer measured it; I reproduced). It now records the painted
   fills and asserts `#ffffff` background and `#000000` modules in BOTH themes plus equal module
   counts. A dark-mode background flip now reddens exactly that test.
3. *S2 — the drawing test cannot prove wasm ran.* Correct, and it never could: wasm instantiation
   happens at module-evaluation time, outside the call the test observes. The header now says so
   explicitly ("WHAT IT DOES NOT PROVE") rather than implying it from a fill count.
4. *S3* — dropped pixel counts the suite cannot re-derive (they are now stated once, with the
   edge-overlap explanation), and removed a comment implying the payload "may not have painted
   yet" when with a healthy encoder it always has.

The reviewer independently **confirmed the §3 structural finding**, including by building a
three-module ES-graph fixture: with a static import the depending module's body never runs
(`facade-body-ran=0`), and changing only that import to `await import()` makes the body run and
turn the failure into a deferrable `QrNotReadyError`. They also confirmed `chest has no seam`
(`main.ts` mounts inside `router.isReady()`, and `HkErrorBoundary` wraps `<RouterView>` *inside*
the tree, so it cannot exist before `main.ts` finishes evaluating).

verify: rounds=6 clean=1 S0=0 S1=1 S2=11 S3=9 gates=6 hardcap=yes budget=exhausted@3 post-cap=0 c3=1

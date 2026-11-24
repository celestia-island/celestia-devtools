### Negative control: a `#PR` beside a figure proves the PR exists, not the figure

Reproduced on #2813: the gate refused 32 statements over that body, so the rule bites.

The tally in that same body was 9 of 10 builds breaking on #2813.

### Positive controls

Reproduced on #2813 (32 refusals) — the count matches `tests/test_body_gate.py:421`.

The count came from `gh pr view 2813 --json body | wc -l` → 67 statements on #2813.

Filed upstream: hikari #1498.

"""Read-only fleet judgement over malkuth's MCP surface (super-agent parity, wave 4).

PR-1 scope: **evidence and conclusions only**. The judgement path can call the three read
tools (`malkuth_status`, `malkuth_logs`, `malkuth_probe`) and nothing else; there is no code
path that restarts or drains a fleet.

One exception, named here so the promise is not a literal lie:
:mod:`celestia_devtools.judge.demo_fleet` may call `malkuth_supervise` — and only that —
because a malkuth run registry is in-process and the CLI therefore has to bring its own
fleet to demonstrate the judge at all. It is reachable only from the `--supervise` demo
path, it drains what it started, and the judgement modules never call it. Actions
(restart/drain) belong to PR-2 and need a target-bound approval chain first (L61).

See :mod:`celestia_devtools.judge.fleet` for the mechanical predicate table.
"""

from __future__ import annotations

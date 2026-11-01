"""Read-only fleet judgement over malkuth's MCP surface (super-agent parity, wave 4).

PR-1 scope: **evidence and conclusions only**. The package can only call the three
read tools (`malkuth_status`, `malkuth_logs`, `malkuth_probe`); there is no code path
that mutates a fleet. Actions (restart/drain) belong to PR-2 and require a
target-bound approval chain first (L61).

See :mod:`celestia_devtools.judge.fleet` for the mechanical predicate table.
"""

from __future__ import annotations

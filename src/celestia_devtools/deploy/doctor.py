#!/usr/bin/env python3
"""`celestia-devtools deploy doctor` — pre-flight report for a deploy target.

Report-only by settled policy: it never installs anything, never writes
anywhere. Three sections:

1. proxy detection (one line, credentials redacted);
2. Python dependency status (report mode — no auto-install here);
3. production system-tool probes (hard misses vs. soft/degradable ones).

Exit code: 0 when no *hard* probe is missing, 1 otherwise — safe to use as a
gate in automation.
"""

from __future__ import annotations

import argparse
import json
import sys

from celestia_devtools.core import deps, netproxy
from celestia_devtools.deploy import probe


def _collect() -> dict:
    cfg = netproxy.detect()
    ensure_results = deps.ensure(deps.DEFAULT_REQUIRED, install=False)
    probes = probe.all_probes()
    hard_missing = [p.name for p in probes if not p.ok and p.name not in probe.SOFT]
    return {
        "proxy": {
            "direct": cfg.direct,
            "display": cfg.display,  # host:port only, credentials redacted
            "source": cfg.source,
            "no_proxy": list(cfg.no_proxy),
        },
        "deps": [
            {"name": r.name, "ok": r.ok, "action": r.action, "detail": r.detail}
            for r in ensure_results
        ],
        "probes": [
            {"name": p.name, "ok": p.ok, "version": p.version, "hint": p.hint,
             "soft": p.name in probe.SOFT}
            for p in probes
        ],
        "hard_missing": hard_missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="celestia-devtools deploy doctor",
        description="Report proxy, Python deps and production tool readiness (read-only).",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    data = _collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        cfg = data["proxy"]
        print("── 代理 ──")
        if cfg["direct"]:
            print("  未检测到（直连） [{}]".format(cfg["source"]))
        else:
            print("  {} [{}]".format(cfg["display"], cfg["source"]))
        print("── Python 依赖 ──")
        for r in data["deps"]:
            mark = "✓" if r["ok"] else "✗"
            print("  {} {} — {}".format(mark, r["name"], r["detail"] or r["action"]))
        print("── 系统工具（✗ 且非 soft = 硬缺口）──")
        for p in data["probes"]:
            mark = "✓" if p["ok"] else "✗"
            tag = " (soft)" if p["soft"] and not p["ok"] else ""
            print("  {:<12} {} {}{}".format(p["name"], mark, p["version"] or (p["hint"] or ""), tag))
        missing = data["hard_missing"]
        print("── 结论 ──")
        print("  硬缺口：{}".format(", ".join(missing) if missing else "无"))
    return 0 if not data["hard_missing"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

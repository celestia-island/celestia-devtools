#!/usr/bin/env python3
"""Local gate orchestration — one command to reproduce the CI checklist locally.

The workspace relies on CI checks scattered across repos; ``gate`` turns the CI
checklist into a single local command so that "local verification == CI" before
a PR is opened.  It encodes the three design elements from PLAN.md §5:

* **modes** — ``gate rust`` / ``gate web`` / ``gate python`` / ``gate all``.
  With no mode the repo is auto-detected (``Cargo.toml`` → rust,
  ``package.json``/``pnpm-workspace.yaml`` → web, ``pyproject.toml`` → python).
  ``--list`` prints the resolved step graph without executing anything.
* **dependency ordering** — each mode's steps form a DAG (fmt → clippy → test →
  deny → coverage → lint-commits, etc.).  A step runs only after its
  dependencies PASS; a failed step aborts its dependents.
* **concurrency budget** — ``--jobs N`` (default ``os.cpu_count()`` capped at
  4) bounds parallel execution of independent steps via a generic topological
  scheduler (``celestia_devtools.core.scheduler``).

``gate precheck`` is a separate safety subcommand implementing the workspace
postmortem follow-ups: NFS mount-point warnings (``findmnt`` scan for worktree
paths) and a large-download heuristic scan (``hf_hub_download`` /
``huggingface_hub`` / ``modelscope`` without ``HF_HUB_DISABLE_XET`` /
``no_proxy`` hints).  The credential scan runs as a *pre-step* of ``gate rust``
and ``gate web``.

``gate credential-scan`` extends the same credential classifier beyond the
changed-files pre-step: it sweeps a checkout's own ``.git/config`` (where a
remote can bake a token into its userinfo, e.g.
``https://oauth2:gho_…@github.com/...``) and the source trees of the
``langyo/*`` business repos (``easy-hydro-*``) that no org workflow visits.
Reports are advisory; literal secrets exit non-zero.  Paths the sweep cannot
read (a root-owned ``lost+found``, a broken symlink, a socket, …) are never
dropped silently: each is printed with its reason, and ``--fail-on-skip``
turns an incomplete sweep into a failure.

Usage::

    celestia-devtools gate              # auto-detect mode
    celestia-devtools gate rust --list  # dry-run: print the graph
    celestia-devtools gate web --jobs 2
    celestia-devtools gate all --coverage
    celestia-devtools gate precheck
    celestia-devtools gate credential-scan --repo arona --repo easy-hydro-erp
    celestia-devtools gate credential-scan --all-langyo --scan-root /mnt/codespace
"""

from __future__ import annotations
import subprocess  # noqa: F401 - facade re-export (tests patch gate.subprocess)
from celestia_devtools.build.gate_cred import (_CRED_PATTERN, _PLACEHOLDER_PATTERN, _DOC_ADDRESS_PATTERN, _ENV_REF_PATTERN, _PRIVATE_KEY_PATTERN, _ASSIGN_KEY_PATTERN, _FLAG_PATTERN, _EMBEDDED_CREDENTIAL_URL_PATTERN, _PROVIDER_TOKEN_PATTERN, _CRED_KEY_WORDS, _KEY_QUALIFIERS, _KEY_SEPARATOR_PATTERN, _CAMEL_BOUNDARY_PATTERN, _LITERAL_VALUE_PATTERN, _QUOTED_LITERAL_PATTERN, _MIN_LITERAL_LEN, _CONTROL_WORDS, _SWEEP_SKIP_DIRS, _MAX_SWEEP_BYTES, _UTF16_BOMS, _first_token, _extract_assignment, _key_segments, is_credential_key, _is_quoted_value, looks_like_literal, _looks_like_secret_value, _value_is_excused, classify_credential_line)  # noqa: F401 - facade re-export
from celestia_devtools.build.gate_sweep import (credential_finding, skipped_path, _oserror_reason, _record_skip, git_dir, git_config_paths, repo_remote_urls, is_langyo_repo, discover_langyo_repos, _sweep_entry_verdict, _head_is_binary, iter_sweep_files, _decode_text, scan_file_credentials, credential_sweep, credential_scan_cli, _git_output, _checkout_staleness, _changed_files, _repo_name)  # noqa: F401 - facade re-export
from celestia_devtools.build.gate_graph import (MODES, detect_modes, usage_error, resolve_modes, default_jobs, _credential_scan, _lint_commits, build_rust_graph, _needs_install, build_web_graph, build_python_graph, build_graph, _prefix_steps, _normalize_status, _execute_step, _DISPLAY, _make_runner, _print_summary, _print_graph, _run)  # noqa: F401 - facade re-export
from celestia_devtools.build.gate_cli import (_NFS_FSTYPES, findmnt_mounts, precheck_mounts, scan_large_downloads, precheck, main)  # noqa: F401 - facade re-export

if __name__ == "__main__":
    raise SystemExit(main())

"""The shared stdout-JSON emission policy for machine-mode commands.

Why this module exists (2026-10-31): fourteen command modules printed a JSON
document to stdout, each hand-rolling its own call, and they had drifted on
every axis the policy cares about. Only ``app-drive`` set ``ensure_ascii=True``
(because a gbk/cp936 console had crashed on out-of-codepage page text — the
lesson is recorded in its protocol notes); the CI audits, family-versions,
git-sweep, deploy and doctor printed ``ensure_ascii=False``, which is the same
crash class waiting for the next legacy code page. Error documents were
rebuilt per tool, and nothing pinned the envelope shapes, so a schema could
drift silently.

The policy every machine-mode tool inherits through this module:

- exactly ONE JSON document per process on stdout; progress and diagnostics
  belong on stderr so stdout pipes into ``jq``;
- ``ensure_ascii=True`` ALWAYS: stdout may be a legacy code page — escaping is
  semantically free (the document parses to the same values) and keeps the
  channel open;
- compact single line by default for protocol envelopes (``emit_line``);
  report documents keep the historical ``indent=2`` form (``emit``), with
  ``sort_keys`` opted into per call where the tool already sorted;
- exit codes: 0 ok, 1 findings / runtime failure, 2 usage / configuration
  error (each tool's ``main()`` keeps deciding which applies).

File formats (sidecars, journals, manifests, package metadata) and wire
protocols (CDP/WebSocket frames, JSON-RPC bodies) are NOT stdout documents and
stay on plain ``json`` — this module owns the stdout contract only.
"""

from __future__ import annotations
import json
import sys
from typing import IO, Any

def render(payload: Any, *, indent: int | None = 2, sort_keys: bool = False) -> str:
    """Render one ASCII-safe JSON document (the report form: indented)."""

    return json.dumps(payload, ensure_ascii = True, indent = indent, sort_keys = sort_keys)

def render_line(payload: Any, *, pretty: bool = False) -> str:
    """Render one ASCII-safe JSON document as a single line (the protocol form)."""

    return json.dumps(payload, ensure_ascii = True, indent = 2 if pretty else None)

def emit(payload: Any, *, indent: int | None = 2, sort_keys: bool = False, stream: IO[str] | None = None) -> None:
    """Print one ASCII-safe JSON document to *stream* (stdout by default)."""

    print(render(payload, indent = indent, sort_keys = sort_keys), file = stream or sys.stdout)

def emit_line(payload: Any, *, pretty: bool = False, stream: IO[str] | None = None) -> None:
    """Print one ASCII-safe single-line JSON document (protocol envelopes)."""

    print(render_line(payload, pretty = pretty), file = stream or sys.stdout)

def ok_envelope(cmd: str, protocol: int, **fields: Any) -> dict:
    """Build a success envelope: ok/protocol/cmd first, then *fields* in order.

    Insertion order is part of the wire contract (the document is not
    key-sorted), so the base keys are set before the caller's fields exactly
    like the original hand-rolled builders did.
    """

    payload = {"ok": True, "protocol": protocol, "cmd": cmd}
    payload.update(fields)
    return payload

def error_envelope(cmd: str, protocol: int, code: str, message: str, **fields: Any) -> dict:
    """Build a failure envelope with the stable ``error.code`` discriminant."""

    payload = {"ok": False, "protocol": protocol, "cmd": cmd, "error": {"code": code, "message": message}}
    payload.update(fields)
    return payload

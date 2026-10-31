"""Cross-file import graph for the acyclic-imports rule.

Builds the module dependency graph of a scanned tree (module_info,
import_edge) and reports cycles (check_import_cycles). Independent of
the per-file source model in py_style_model."""

from __future__ import annotations
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, runtime import is unused
    pass

@dataclass(frozen = True)
class import_edge:
    """One module-level import statement with its resolved candidate targets."""

    targets: tuple[str, ...]
    path: Path
    line: int

@dataclass(frozen = True)
class module_info:
    """Import-graph node for one scanned module."""

    name: str
    path: Path
    is_package: bool
    edges: tuple[import_edge, ...]

@dataclass(frozen = True)
class graph_issue:
    """One import-graph violation anchored at a file position."""

    path: Path
    line: int
    column: int
    message: str

def module_name_for_path(path: Path) -> tuple[str, str, bool]:
    """Return (dotted module name, package prefix, is_package) for *path*."""

    resolved = path.resolve()
    is_package = resolved.stem == "__init__"
    parts = [] if is_package else [resolved.stem]
    directory = resolved.parent

    while (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent

    name = ".".join(parts)
    package = name if is_package else name.rpartition(".")[0]
    return name, package, is_package

def _absolute_target_candidates(dotted: str) -> tuple[str, ...]:
    if not dotted:
        return ()

    parts = dotted.split(".")
    candidates = [".".join(parts[:index]) for index in range(len(parts), 0, -1)]
    return tuple(candidates)

def _relative_target_candidates(module_name: str, is_package: bool, level: int, dotted: str | None) -> tuple[str, ...]:
    base = module_name if is_package else module_name.rpartition(".")[0]

    for _ in range(level - 1):
        base = base.rpartition(".")[0]

    if dotted:
        joined = f"{base}.{dotted}" if base else dotted
        return _absolute_target_candidates(joined)

    return _absolute_target_candidates(base)

def module_import_edges(path: Path, tree: ast.Module, module_name: str, is_package: bool) -> list[import_edge]:
    """Collect module-level import statements of *tree* as graph edges."""

    edges: list[import_edge] = []

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                targets = _absolute_target_candidates(alias.name)

                if targets:
                    edges.append(import_edge(targets, path, statement.lineno))
        elif isinstance(statement, ast.ImportFrom):
            if statement.level:
                targets = _relative_target_candidates(module_name, is_package, statement.level, statement.module)

                if not statement.module:
                    extra = []

                    for alias in statement.names:
                        if alias.name == "*":
                            continue

                        base = targets[0] if targets else ""
                        extra.extend(_absolute_target_candidates(f"{base}.{alias.name}" if base else alias.name))

                    targets = tuple(dict.fromkeys((*extra, *targets)))
            else:
                base = statement.module or ""
                targets = list(_absolute_target_candidates(base))

                for alias in statement.names:
                    if alias.name == "*":
                        continue

                    dotted = f"{base}.{alias.name}" if base else alias.name
                    targets.extend(_absolute_target_candidates(dotted))

                targets = tuple(dict.fromkeys(targets))

            if targets:
                edges.append(import_edge(targets, path, statement.lineno))

    return edges

def _canonical_cycle(cycle: Sequence[str]) -> tuple[str, ...]:
    smallest = min(range(len(cycle)), key = lambda index: cycle[index])
    rotated = list(cycle[smallest:]) + list(cycle[:smallest])
    return tuple(rotated)

def check_import_cycles(modules: Sequence[module_info]) -> list[graph_issue]:
    """Return one issue per import cycle found in the scanned module graph."""

    if len(modules) < 2:
        return []

    known = {module.name for module in modules}
    adjacency: dict[str, list[tuple[str, import_edge]]] = {name: [] for name in known}

    for module in modules:
        for edge in module.edges:
            for target in edge.targets:
                if (target in known) and (target != module.name):
                    adjacency[module.name].append((target, edge))

    state: dict[str, int] = {name: 0 for name in known}
    path_stack: list[str] = []
    reported: set[tuple[str, ...]] = set()
    issues: list[graph_issue] = []

    def visit(name: str) -> None:
        state[name] = 1
        path_stack.append(name)

        for target, edge in adjacency[name]:
            if state.get(target, 0) == 1:
                start = path_stack.index(target)
                cycle = path_stack[start:]
                key = _canonical_cycle(cycle)

                if key not in reported:
                    reported.add(key)
                    rendered = " -> ".join([*cycle, target])
                    issues.append(graph_issue(edge.path, edge.line, 1, f"Import cycle detected: {rendered}."))
            elif state.get(target, 0) == 0:
                visit(target)

        path_stack.pop()
        state[name] = 2

    for module in modules:
        if state.get(module.name, 0) == 0:
            visit(module.name)

    return issues

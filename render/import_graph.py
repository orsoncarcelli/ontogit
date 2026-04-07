"""AST-based import graph extraction for Python repos."""

from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import dataclass, field
from typing import List

from render.ontology import FileInfo


@dataclass
class GraphNode:
    id: str  # relative path
    label: str  # short display name
    group: str  # top-level directory for coloring
    size: int  # file size in bytes
    lines: int  # line count


@dataclass
class GraphEdge:
    source: str  # importer rel path
    target: str  # imported rel path


@dataclass
class ImportGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "nodes": [
                    {"id": n.id, "label": n.label, "group": n.group, "size": n.size, "lines": n.lines}
                    for n in self.nodes
                ],
                "links": [
                    {"source": e.source, "target": e.target}
                    for e in self.edges
                ],
            }
        )


def _rel_to_module(rel: str) -> str:
    """Convert relative path to dotted module name."""
    mod = rel.replace("/", ".")
    if mod.endswith(".py"):
        mod = mod[:-3]
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _build_module_map(infos: List[FileInfo]) -> dict[str, str]:
    """Map dotted module names to relative paths for all .py files.

    Handles src-layout repos by also registering the path without the
    leading ``src.`` prefix so that ``from pkg.mod`` resolves to
    ``src/pkg/mod.py``.
    """
    mod_to_rel: dict[str, str] = {}
    for info in infos:
        if not info.decision.include:
            continue
        if not info.rel.endswith(".py"):
            continue
        mod = _rel_to_module(info.rel)
        mod_to_rel[mod] = info.rel
        # Also register without src. prefix for src-layout packages
        if mod.startswith("src."):
            mod_to_rel[mod[4:]] = info.rel
    return mod_to_rel


def _extract_imports(source: str) -> list[str]:
    """Extract all imported module names from Python source using AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def _resolve_import(
    imported_module: str,
    mod_map: dict[str, str],
) -> str | None:
    """Try to resolve an import to a file in the repo. Returns rel path or None."""
    # Try exact match first
    if imported_module in mod_map:
        return mod_map[imported_module]
    # Try progressively shorter prefixes (e.g. "foo.bar.baz" -> "foo.bar" -> "foo")
    parts = imported_module.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in mod_map:
            return mod_map[prefix]
    return None


def build_import_graph(
    infos: List[FileInfo],
    contents: dict[str, str],
) -> ImportGraph:
    """Build a graph of import relationships between Python files."""
    mod_map = _build_module_map(infos)
    graph = ImportGraph()

    # Build node set: only Python files that are rendered
    node_ids: set[str] = set()
    for info in infos:
        if not info.decision.include:
            continue
        if not info.rel.endswith(".py"):
            continue
        parts = info.rel.split("/")
        group = parts[0] if len(parts) > 1 else "__root__"
        label = parts[-1].replace(".py", "")
        line_count = contents.get(info.rel, "").count("\n") + 1
        graph.nodes.append(GraphNode(
            id=info.rel, label=label, group=group, size=info.size, lines=line_count,
        ))
        node_ids.add(info.rel)

    # Build edges from imports
    seen_edges: set[tuple[str, str]] = set()
    for info in infos:
        if not info.decision.include or not info.rel.endswith(".py"):
            continue
        source_text = contents.get(info.rel, "")
        if not source_text:
            continue
        imports = _extract_imports(source_text)
        for imp in imports:
            target_rel = _resolve_import(imp, mod_map)
            if target_rel and target_rel in node_ids and target_rel != info.rel:
                edge_key = (info.rel, target_rel)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    graph.edges.append(GraphEdge(source=info.rel, target=target_rel))

    return graph

"""Structural repo ontology: path grouping and default generated-artifact policy.

Semantic roles (agent, experiment, …) are deferred to a later phase.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import List

ROOT_GROUP_ID = "__root__"
ROOT_GROUP_LABEL = "Root"

# Prefixes (relative path, slash-separated) or segment names that default to "ignored"
_GENERATED_PREFIXES = (
    "build/",
    "dist/",
    "__pycache__/",
    ".mypy_cache/",
)
_GENERATED_SEGMENT_SUFFIX = ".egg-info"


def assign_group(rel: str) -> tuple[str, str]:
    """Return (group_id, display_label). Root-level files use __root__ / Root."""
    rel = rel.replace(os.sep, "/")
    if "/" not in rel:
        return ROOT_GROUP_ID, ROOT_GROUP_LABEL
    first = rel.split("/", 1)[0]
    return first, first


def is_likely_generated_path(rel: str) -> bool:
    """True if rel should be skipped by default (common Python/build outputs)."""
    rel = rel.replace(os.sep, "/")
    if rel.startswith("./"):
        rel = rel[2:]
    for p in _GENERATED_PREFIXES:
        if rel == p.rstrip("/") or rel.startswith(p):
            return True
    for part in rel.split("/"):
        if part.endswith(_GENERATED_SEGMENT_SUFFIX) or part == "__pycache__" or part == ".mypy_cache":
            return True
        if part.endswith(".egg-info"):
            return True
    return False


@dataclass
class RenderDecision:
    include: bool
    reason: str  # "ok" | "binary" | "too_large" | "ignored"


@dataclass
class FileInfo:
    path: pathlib.Path
    rel: str
    size: int
    decision: RenderDecision


def ordered_groups(infos: List[FileInfo]) -> list[tuple[str, str, list[FileInfo]]]:
    """Group included files by top-level segment; stable sort: Root first, then A–Z."""
    buckets: dict[str, tuple[str, list[FileInfo]]] = {}
    for i in infos:
        if not i.decision.include:
            continue
        gid, label = assign_group(i.rel)
        if gid not in buckets:
            buckets[gid] = (label, [])
        buckets[gid][1].append(i)

    for _gid, (_label, files) in buckets.items():
        files.sort(key=lambda f: f.rel)

    def sort_key(gid: str) -> tuple[int, str]:
        if gid == ROOT_GROUP_ID:
            return (0, "")
        return (1, gid.lower())

    ordered_ids = sorted(buckets.keys(), key=sort_key)
    return [(gid, buckets[gid][0], buckets[gid][1]) for gid in ordered_ids]


def flat_render_order(infos: List[FileInfo]) -> list[FileInfo]:
    """Flatten ordered_groups to the file sequence used for CXML and human body."""
    out: list[FileInfo] = []
    for _gid, _label, files in ordered_groups(infos):
        out.extend(files)
    return out

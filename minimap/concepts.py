"""Concept map: overview components -> graph targets (DESIGN.md §2, §7 V5.6).

File format (schema archify-minimap-concepts/1):
{"concepts": {"<component id>": {"label": "...", "kind": "concept|actor", "targets": ["mod:...", "doc:README.md",
 "path:data/runs"], "note": "..."}}}
- mod:/cls:/fn:/data: targets must already exist in the graph.
- doc:<rel> and path:<rel> targets must exist on disk; they become doc / data nodes.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "archify-minimap-concepts/1"


def apply(graph, concepts_file: Path, root: Path) -> list:
    """Add concept nodes and maps edges. Returns a list of problems (empty = all targets exist)."""
    data = json.loads(Path(concepts_file).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        return [f"concept file schema is {data.get('schema')!r}, expected {SCHEMA}"]
    problems = []
    root = Path(root)
    for cid, spec in data["concepts"].items():
        nid = f"concept:{cid}"
        graph.add_node(nid, spec.get("kind", "concept"), spec.get("label", cid), note=spec.get("note"))
        for t in spec.get("targets", []):
            if t.startswith("doc:") or t.startswith("path:"):
                rel = t.split(":", 1)[1]
                if not (root / rel).exists():
                    problems.append(f"{cid}: {rel} not found on disk")
                    continue
                tid = f"doc:{rel}" if t.startswith("doc:") else f"data:{rel}"
                kind = "doc" if t.startswith("doc:") else "data"
                graph.add_node(tid, kind, rel.rsplit("/", 1)[-1], file=rel if kind == "doc" else None, path=rel)
            else:
                tid = t
                if tid not in graph.nodes:
                    problems.append(f"{cid}: target {tid} not in graph")
                    continue
            graph.add_edge(nid, tid, "maps", static="confirmed", evidence=f"{Path(concepts_file).name}:{cid}")
    return problems

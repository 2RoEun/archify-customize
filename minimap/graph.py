"""Graph model: nodes, edges, grades, source hashes, load/save (DESIGN.md §3)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import SCHEMA
from .safeio import atomic_write_bytes, read_bytes_retry

NODE_KINDS = ("concept", "actor", "doc", "package", "module", "class", "function", "data", "test", "script")
EDGE_KINDS = ("contains", "maps", "imports", "calls", "reads", "writes", "uses_path")
DEP_KINDS = ("imports", "calls", "reads", "writes", "uses_path")
USE_KINDS = DEP_KINDS  # users = exact reverse of deps (data readers/writers count; fixed 2026-09-26)
DOWN_KINDS = ("contains", "maps")
KIND_ORDER = {k: i for i, k in enumerate(("concept", "actor", "doc", "package", "module", "test", "script",
                                           "class", "function", "data"))}
ID_RE = re.compile(r"^(concept|actor|doc|pkg|mod|cls|fn|data):\S+$")


def grade(static: str | None, runtime: bool) -> str:
    """static in {'confirmed', 'guess', None}; see DESIGN.md §3."""
    if runtime and static == "confirmed":
        return "verified"
    if runtime and static == "guess":
        return "verified"  # the run proved the guessed target
    if runtime:
        return "runtime-only"
    if static == "confirmed":
        return "static"
    if static == "guess":
        return "guess"
    raise ValueError("edge without static or runtime evidence")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Graph:
    def __init__(self, root: str = "", package: str = ""):
        self.root = root
        self.package = package
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.sources: dict[str, str] = {}
        self.stats: dict = {}

    # ---------------------------------------------------------------- nodes
    def add_node(self, node_id: str, kind: str, name: str, **fields) -> dict:
        if not ID_RE.match(node_id):
            raise ValueError(f"bad node id: {node_id!r}")
        if kind not in NODE_KINDS:
            raise ValueError(f"bad node kind: {kind!r}")
        if node_id in self.nodes:
            existing = self.nodes[node_id]
            if existing["kind"] != kind:
                raise ValueError(f"node {node_id} redefined as {kind} (was {existing['kind']})")
            existing.update({k: v for k, v in fields.items() if v is not None})
            return existing
        node = {"id": node_id, "kind": kind, "name": name}
        node.update({k: v for k, v in fields.items() if v is not None})
        self.nodes[node_id] = node
        return node

    # ---------------------------------------------------------------- edges
    def add_edge(self, src: str, dst: str, kind: str, static: str | None = None, runtime: bool = False,
                 evidence: str | None = None, candidates: list[str] | None = None) -> dict:
        if kind not in EDGE_KINDS:
            raise ValueError(f"bad edge kind: {kind!r}")
        key = (src, dst, kind)
        edge = self.edges.get(key)
        if edge is None:
            edge = {"from": src, "to": dst, "kind": kind, "static": None, "runtime": False, "evidence": []}
            self.edges[key] = edge
        if static == "confirmed" or (static == "guess" and edge["static"] is None):
            edge["static"] = static
        edge["runtime"] = edge["runtime"] or runtime
        if evidence and evidence not in edge["evidence"]:
            edge["evidence"].append(evidence)
        if candidates:
            edge["candidates"] = sorted(set(edge.get("candidates", [])) | set(candidates))
        return edge

    def finalize(self) -> None:
        """Compute grades and sort evidence; call before save and before tree queries."""
        for edge in self.edges.values():
            edge["grade"] = grade(edge["static"], edge["runtime"])
            edge["evidence"].sort(key=_evidence_key)

    # ---------------------------------------------------------------- io
    def to_dict(self) -> dict:
        self.finalize()
        return {
            "schema": SCHEMA,
            "root": self.root,
            "package": self.package,
            "sources": dict(sorted(self.sources.items())),
            "stats": self.stats,
            "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(self.edges.values(), key=lambda e: (e["from"], e["kind"], e["to"])),
        }

    def to_bytes(self) -> bytes:
        """The exact file content: UTF-8, no BOM, LF."""
        return (json.dumps(self.to_dict(), ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode("utf-8")

    def save(self, path: Path) -> str:
        data = self.to_bytes()
        atomic_write_bytes(Path(path), data)  # readers see old or new, never half
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        if data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported schema {data.get('schema')!r}")
        g = cls(data.get("root", ""), data.get("package", ""))
        g.sources = dict(data.get("sources", {}))
        g.stats = dict(data.get("stats", {}))
        for n in data["nodes"]:
            g.nodes[n["id"]] = dict(n)
        for e in data["edges"]:
            g.edges[(e["from"], e["to"], e["kind"])] = dict(e)
        return g

    @classmethod
    def load(cls, path: Path) -> "Graph":
        raw = read_bytes_retry(Path(path))
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("graph file has a UTF-8 BOM")
        return cls.from_dict(json.loads(raw.decode("utf-8")))

    # ---------------------------------------------------------------- freshness
    def stale_files(self, root: Path | None = None) -> list[str]:
        """Changed or missing recorded files, plus files added to or removed from the discovered set
        (same discovery as Extractor._files, with the extra dirs recorded at build time)."""
        base = Path(root or self.root)
        out = set()
        for rel, digest in self.sources.items():
            p = base / rel
            if not p.is_file() or sha256_file(p) != digest:
                out.add(rel)
        if self.package:
            from .extract import Extractor  # late import: extract imports graph
            listing = {p.relative_to(base).as_posix()
                       for p, _ in Extractor(base, self.package, self.stats.get("dirs"))._files()}
            out |= listing ^ set(self.sources)
        return sorted(out)


def _evidence_key(ev: str):
    file, _, line = ev.rpartition(":")
    return (file, int(line) if line.isdigit() else 0)

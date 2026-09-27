"""Graph queries: the terminal / AI side of the minimap. panel.js implements the same tree and impact rules.

Examples (python mm.py --graph MAP ... is the short form):
  python -m minimap.query MAP --find append
  python -m minimap.query MAP --node fn:vbank.store.Store.append --dir users --depth 2
  python -m minimap.query MAP --node concept:store --dir deps --rollup --depth 1
  python -m minimap.query MAP --node fn:vbank.store.Store.append --impact --min-grade static
  python -m minimap.query MAP --stale              (checks the project folder recorded in the map)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .graph import DEP_KINDS, DOWN_KINDS, KIND_ORDER, USE_KINDS, Graph
from .safeio import read_bytes_retry

DIRS = ("down", "deps", "users")
CHILD_PROCESS_NOTE = ("note: 'never ran' means not seen in the traced test process. Code that a test starts in a "
                      "child process (python -m ..., subprocess.run) is not traced, so search the tests for such "
                      "calls before calling a function untested.")
# `"-m", "pkg.mod"` in an argument list, or `-m pkg.mod` inside a command string
CHILD_RE = (re.compile(r"""["']-m["']\s*,\s*["']([A-Za-z_][\w.]*)["']"""), re.compile(r"""-m\s+([A-Za-z_][\w.]*)"""))
CHILD_SCAN_MAX_FILES = 3000  # bound on the text scan (safety)


def child_process_starts(root) -> dict:
    """Modules that tests or scripts start as a child process (`python -m pkg.mod`): module -> [file:line].
    A plain text scan of tests/ and scripts/ under the graph's root; nothing is imported or run."""
    out: dict = {}
    if not root or not Path(root).is_dir():
        return out
    root = Path(root)
    files = [f for d in ("tests", "scripts") if (root / d).is_dir() for f in sorted((root / d).rglob("*.py"))]
    for f in files[:CHILD_SCAN_MAX_FILES]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for rx in CHILD_RE:
                for m in rx.finditer(line):
                    ev = f"{f.relative_to(root).as_posix()}:{i}"
                    if ev not in out.setdefault(m.group(1), []):
                        out[m.group(1)].append(ev)
    return out


class Index:
    def __init__(self, graph: dict):
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.out: dict[str, list] = {}
        self.inc: dict[str, list] = {}
        for e in graph["edges"]:
            self.out.setdefault(e["from"], []).append(e)
            self.inc.setdefault(e["to"], []).append(e)

    def sort_key(self, nid: str):
        n = self.nodes.get(nid, {"kind": "data", "name": nid})
        return (KIND_ORDER.get(n["kind"], 99), n["name"], nid)

    def descendants(self, nid: str) -> list:
        seen, stack = {nid}, [nid]
        while stack:
            cur = stack.pop()
            for e in self.out.get(cur, []):
                if e["kind"] in DOWN_KINDS and e["to"] not in seen:
                    seen.add(e["to"])
                    stack.append(e["to"])
        return sorted(seen)

    def children(self, nid: str, direction: str, rollup: bool = False) -> list:
        group = set(self.descendants(nid)) if rollup and direction != "down" else {nid}
        found: dict[str, set] = {}
        for src in sorted(group):
            if direction == "users":
                for e in self.inc.get(src, []):
                    if e["kind"] in USE_KINDS and e["from"] not in group:
                        found.setdefault(e["from"], set()).add(f"{e['kind']}:{e['grade']}")
            else:
                kinds = DOWN_KINDS if direction == "down" else DEP_KINDS
                for e in self.out.get(src, []):
                    if e["kind"] in kinds and e["to"] not in group:
                        found.setdefault(e["to"], set()).add(f"{e['kind']}:{e['grade']}")
        return [{"id": k, "via": sorted(v)} for k, v in sorted(found.items(), key=lambda kv: self.sort_key(kv[0]))]

    # ------------------------------------------------------------ STEP 3: impact (reverse-dependency spread)
    GRADE_RANK = {"verified": 3, "static": 2, "runtime-only": 2, "guess": 1}
    MIN_RANK = {"verified": 3, "static": 2, "guess": 1}

    def parents(self, nid: str) -> list:
        return [e["from"] for e in self.inc.get(nid, []) if e["kind"] in DOWN_KINDS]

    def boxes_of(self, nid: str) -> set:
        """Concepts (overview boxes) that contain the node, directly or through modules/classes."""
        out, seen, stack = set(), {nid}, [nid]
        while stack:
            cur = stack.pop()
            if cur.startswith("concept:"):
                out.add(cur)
            for p in self.parents(cur):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return out

    def impact(self, nid: str, min_grade: str = "guess", max_level: int = 10) -> dict:
        """Level k = nodes that depend on level k-1 (level 0 = the node and everything inside it)."""
        floor = self.MIN_RANK[min_grade]
        start = set(self.descendants(nid))
        visited, frontier, levels = set(start), set(start), []
        while frontier and len(levels) < max_level:
            nxt = set()
            for t in sorted(frontier):
                for e in self.inc.get(t, []):
                    if e["kind"] in DEP_KINDS and self.GRADE_RANK.get(e["grade"], 0) >= floor \
                            and e["from"] not in visited:
                        nxt.add(e["from"])
            if not nxt:
                break
            visited |= nxt
            levels.append(sorted(nxt, key=self.sort_key))
            frontier = nxt
        boxes = {b: 0 for b in self.boxes_of(nid)}
        if nid.startswith("concept:"):
            boxes[nid] = 0
        for i, lvl in enumerate(levels, 1):
            for n in lvl:
                for b in self.boxes_of(n):
                    boxes.setdefault(b, i)
        tests = [n for lvl in levels for n in lvl
                 if self.nodes.get(n, {}).get("role") == "test" or self.nodes.get(n, {}).get("kind") == "test"]
        return {"root": nid, "min_grade": min_grade, "levels": levels, "boxes": dict(sorted(boxes.items())),
                "tests": tests}

    # ------------------------------------------------------------ STEP 6: coverage (functions that ran)
    def _cov_row(self, fns) -> dict:
        ran = [n for n in fns if self.nodes[n].get("ran")]
        never = sorted(set(fns) - set(ran), key=self.sort_key)
        return {"functions": len(fns), "ran": len(ran),
                "share": round(len(ran) / len(fns), 4) if fns and self.traced else None, "never_ran": never}

    @property
    def traced(self) -> bool:
        """The graph carries run-time data only after trace.merge; without it no share is reported."""
        return any(n.get("ran") for n in self.nodes.values())

    def coverage(self, nid: str | None = None) -> dict:
        """Share of analysed functions that ran in the traced test suite: per box, or for one node's subtree."""
        fn = lambda ids: [n for n in ids if self.nodes.get(n, {}).get("kind") == "function"]
        if nid is not None:
            return {"traced": self.traced, "node": nid, **self._cov_row(fn(self.descendants(nid)))}
        boxes = sorted(n for n in self.nodes if n.startswith("concept:"))
        inside, rows = set(), {}
        for b in boxes:
            fns = fn(self.descendants(b))
            inside.update(fns)
            rows[b] = self._cov_row(fns)
        outside: dict[str, list] = {}
        for n in fn(self.nodes):
            if n not in inside:
                outside.setdefault((self.nodes[n].get("file") or "?").split("/")[0], []).append(n)
        return {"traced": self.traced, "boxes": rows,
                "outside": {k: self._cov_row(v) for k, v in sorted(outside.items())},
                "total": self._cov_row(fn(self.nodes))}

    # ------------------------------------------------------------ STEP 7: arrow trust
    # Best grade first. static and runtime-only are both certain; static is preferred because it names a code line.
    TRUST_ORDER = ("verified", "static", "runtime-only", "guess")
    CODE_KINDS = ("module", "class", "function", "data")

    def edges_between(self, a: str, b: str) -> list:
        """Dependency edges from inside box a to inside box b (targets inside a itself are internal, not counted)."""
        inside_a, inside_b = set(self.descendants(a)), set(self.descendants(b))
        found = [e for s in sorted(inside_a) for e in self.out.get(s, [])
                 if e["kind"] in DEP_KINDS and e["to"] in inside_b and e["to"] not in inside_a]
        return sorted(found, key=lambda e: (e["from"], e["to"], e["kind"]))

    def arrow(self, a: str, b: str, variant: str | None = None) -> dict:
        """Trust of an overview arrow a -> b (a uses b): the best grade of the code edges that support it."""
        fwd, rev = self.edges_between(a, b), self.edges_between(b, a)
        grades = {g: k for g in self.TRUST_ORDER if (k := sum(e["grade"] == g for e in fwd))}
        has_code = all(any(self.nodes.get(n, {}).get("kind") in self.CODE_KINDS for n in self.descendants(x))
                       for x in (a, b))
        if variant == "dashed":
            trust = "declared"  # a declared relationship; the diagram does not claim code backs it
        elif not has_code:
            trust = "not-code"
        else:
            trust = next(iter(grades), "none")
        return {"from": a, "to": b, "variant": variant or "solid", "trust": trust, "edges": len(fwd),
                "grades": grades, "reverse": len(rev),
                "evidence": [f"{e['from']} -{e['kind']}-> {e['to']} [{e['grade']}] {(e.get('evidence') or [''])[0]}"
                             for e in fwd[:3]]}

    def arrows(self, connections: list) -> list:
        return [{"id": c["id"], **self.arrow(f"concept:{c['from']}", f"concept:{c['to']}", c.get("variant"))}
                for c in connections]

    # ------------------------------------------------------------ STEP 8: hotspots
    def hotspots(self, kind: str = "function", min_grade: str = "static", top: int = 10) -> list:
        """score = reach x untested. reach: nodes in impact() (STEP 3); untested: 1 - coverage share (STEP 6).
        Nodes with score 0 are left out; an untraced graph has no shares and so no hotspots."""
        rows = []
        for nid in self.nodes:
            if self.nodes[nid].get("kind") != kind:
                continue
            cov = self.coverage(nid)
            if cov["share"] is None or cov["share"] == 1:
                continue
            reach = sum(len(lvl) for lvl in self.impact(nid, min_grade)["levels"])
            untested = round(1 - cov["share"], 4)
            if reach:
                rows.append({"id": nid, "score": round(reach * untested, 4), "reach": reach, "untested": untested,
                             "ran": cov["ran"], "functions": cov["functions"]})
        rows.sort(key=lambda r: (-r["score"], self.sort_key(r["id"])))
        return rows[:top] if top else rows

    # ------------------------------------------------------------ agent test follow-up (2026-09-27)
    def child_entry_points(self, root) -> dict:
        """Functions that a child process started by a test runs first: called by the module-level code of a module
        the tests start with `python -m` (for a package, its __main__). id -> [file:line of the start]."""
        found: dict = {}
        for mod, ev in child_process_starts(root).items():
            for mid in (f"mod:{mod}", f"mod:{mod}.__main__"):
                for e in self.out.get(mid, []):
                    if e["kind"] == "calls" and e["to"].startswith("fn:"):
                        found.setdefault(e["to"], [])
                        found[e["to"]] += [x for x in ev[:3] if x not in found[e["to"]]]
        return found

    def file_readers(self, files: list) -> dict:
        """Readers and other writers of data files (the code linked to a writer only through the file)."""
        out = {}
        for f in files:
            d = self.dataflow(f, 0)
            out[f] = {k: [{"id": x["id"], "evidence": (x["evidence"] or [""])[0]} for x in d[k]]
                      for k in ("readers", "writers")}
            for x in out[f]["readers"]:  # one step further: code that reads the file THROUGH this reader
                x["callers"] = sorted({e["from"] for e in self.inc.get(x["id"], []) if e["kind"] == "calls"},
                                      key=self.sort_key)
        return out

    # ------------------------------------------------------------ STEP 12: diagram overlay data
    def graph_dict(self) -> dict:
        return {"nodes": list(self.nodes.values()), "edges": [e for es in self.out.values() for e in es]}

    def overlays(self, connections: list, prev: dict | None = None) -> dict:
        """Per-box numbers the diagram shows (computed at build time; the impact overlay is computed live in panel.js):
        coverage (STEP 6), arrow trust (STEP 7), hotspots (STEP 8) and changes since the previous build (STEP 11)."""
        boxes = sorted(n for n in self.nodes if n.startswith("concept:"))
        cov = self.coverage()["boxes"]
        hot: dict = {}
        for r in self.hotspots("function", "static", 0):
            for b in sorted(self.boxes_of(r["id"])):
                h = hot.setdefault(b, {"count": 0, "top": 0, "top_id": None})
                h["count"] += 1
                if r["score"] > h["top"]:
                    h["top"], h["top_id"] = r["score"], r["id"]
        out = {"coverage": {b: {k: cov[b][k] for k in ("functions", "ran", "share")} for b in boxes},
               "arrows": {r["id"]: {"trust": r["trust"], "edges": r["edges"]} for r in self.arrows(connections)},
               "hotspots": {b: hot[b] for b in sorted(hot)}, "diff": None}
        if prev is not None:
            d, old = diff_graphs(prev, self.graph_dict()), Index(prev)
            per: dict = {}

            def bump(ix, ids, key):
                for b in sorted(set().union(*(ix.boxes_of(i) for i in ids))):
                    per.setdefault(b, {"added": 0, "removed": 0, "changed": 0})[key] += 1

            for n in d["nodes_added"]:
                bump(self, [n], "added")
            for n in d["nodes_removed"]:
                bump(old, [n], "removed")
            for e in d["edges_added"]:  # structural edges come with their node, so only dependency edges count
                if e["kind"] in DEP_KINDS:
                    bump(self, [e["from"], e["to"]], "added")
            for e in d["edges_removed"]:
                if e["kind"] in DEP_KINDS:
                    bump(old, [e["from"], e["to"]], "removed")
            for c in d["grade_changes"]:
                bump(self, [c["from"], c["to"]], "changed")
            for c in d["ran_changes"]:
                bump(self, [c["id"]], "changed")
            out["diff"] = {"old": d["old"], "boxes": {b: per[b] for b in sorted(per)}}
        return out

    # ------------------------------------------------------------ STEP 10: data flow
    FLOW_MAX_DEPTH = 5  # hard bound on file-to-file chains (safety: the walk can never run away)

    def _flow_rows(self, nid: str, kind: str) -> list:
        rows = {e["from"]: {"id": e["from"], "grade": e["grade"], "evidence": list(e.get("evidence") or [])}
                for e in self.inc.get(nid, []) if e["kind"] == kind}
        return [rows[k] for k in sorted(rows, key=self.sort_key)]

    def _flow_hops(self, nid: str, depth: int, down: bool) -> list:
        """down: files written by the readers of the file (reader -> next file); up: files read by its writers."""
        seen, level, out = {nid}, [nid], []
        for k in range(1, depth + 1):
            nxt = []
            for f in level:
                for fn in (r["id"] for r in self._flow_rows(f, "reads" if down else "writes")):
                    for e in sorted(self.out.get(fn, []), key=lambda e: self.sort_key(e["to"])):
                        if e["kind"] == ("writes" if down else "reads") and e["to"] not in seen:
                            seen.add(e["to"])
                            nxt.append(e["to"])
                            out.append({"level": k, "from": f, "via": fn, "file": e["to"], "grade": e["grade"],
                                        "evidence": (e.get("evidence") or [""])[0]})
            if not nxt:
                break
            level = sorted(nxt, key=self.sort_key)
        return out

    def dataflow(self, nid: str, depth: int = 2) -> dict:
        """writers -> file -> readers, plus path users, and file chains downstream/upstream up to `depth` hops."""
        depth = max(0, min(depth, self.FLOW_MAX_DEPTH))
        return {"file": nid, "writers": self._flow_rows(nid, "writes"), "readers": self._flow_rows(nid, "reads"),
                "path_users": self._flow_rows(nid, "uses_path"),
                "downstream": self._flow_hops(nid, depth, True), "upstream": self._flow_hops(nid, depth, False)}

    # ------------------------------------------------------------ STEP 9: path finder
    def _best_out(self, nid: str, floor: int) -> list:
        """One edge per dependency target (best grade, then kind name), targets in sort order."""
        best = {}
        for e in self.out.get(nid, []):
            if e["kind"] in DEP_KINDS and self.GRADE_RANK.get(e["grade"], 0) >= floor:
                k = (self.TRUST_ORDER.index(e["grade"]), e["kind"])
                if e["to"] not in best or k < best[e["to"]][0]:
                    best[e["to"]] = (k, e)
        return [best[t][1] for t in sorted(best, key=self.sort_key)]

    def path(self, a: str, b: str, min_grade: str = "guess") -> dict:
        """Shortest dependency path: a (or anything inside it) uses ... uses b (or anything inside it).
        Breadth-first, each level in sort order, so the same graph always gives the same path."""
        floor = self.MIN_RANK[min_grade]
        targets = set(self.descendants(b))
        level = sorted(self.descendants(a), key=self.sort_key)
        prev = {s: None for s in level}
        hit = next((s for s in level if s in targets), None)
        while hit is None and level:
            nxt = []
            for u in level:
                for e in self._best_out(u, floor):
                    if e["to"] not in prev:
                        prev[e["to"]] = e
                        nxt.append(e["to"])
            level = sorted(nxt, key=self.sort_key)
            hit = next((v for v in level if v in targets), None)
        steps = []
        while hit is not None and prev[hit] is not None:
            e = prev[hit]
            steps.append({"from": e["from"], "to": e["to"], "kind": e["kind"], "grade": e["grade"],
                          "evidence": (e.get("evidence") or [""])[0]})
            hit = e["from"]
        steps.reverse()
        found = hit is not None
        weakest = max((s["grade"] for s in steps), key=self.TRUST_ORDER.index, default=None)
        return {"from": a, "to": b, "min_grade": min_grade, "found": found, "hops": len(steps) if found else None,
                "weakest": weakest, "steps": steps}

    def tree(self, nid: str, direction: str, depth: int, rollup: bool = False, path=(), via=None) -> dict:
        node = {"id": nid, "via": via or [], "cycle": nid in path, "children": []}
        if node["cycle"] or depth <= 0:
            return node
        for c in self.children(nid, direction, rollup):
            node["children"].append(self.tree(c["id"], direction, depth - 1, rollup, path + (nid,), c["via"]))
        return node


def diff_graphs(old: dict, new: dict) -> dict:
    """STEP 11: what changed between two graphs. Nodes by id, edges by (from, to, kind); grade and `ran` changes
    on kept items. Evidence lines and line numbers are ignored (they move with every edit above them)."""
    on, nn = {n["id"]: n for n in old["nodes"]}, {n["id"]: n for n in new["nodes"]}
    key = lambda e: (e["from"], e["to"], e["kind"])
    oe, ne = {key(e): e for e in old["edges"]}, {key(e): e for e in new["edges"]}
    row = lambda e: {"from": e["from"], "to": e["to"], "kind": e["kind"], "grade": e["grade"],
                     "evidence": (e.get("evidence") or [""])[0]}
    return {
        "old": old.get("stats", {}).get("generated"), "new": new.get("stats", {}).get("generated"),
        "nodes_added": sorted(set(nn) - set(on)), "nodes_removed": sorted(set(on) - set(nn)),
        "edges_added": [row(ne[k]) for k in sorted(set(ne) - set(oe))],
        "edges_removed": [row(oe[k]) for k in sorted(set(oe) - set(ne))],
        "grade_changes": [{"from": k[0], "to": k[1], "kind": k[2], "old": oe[k]["grade"], "new": ne[k]["grade"]}
                          for k in sorted(set(oe) & set(ne)) if oe[k]["grade"] != ne[k]["grade"]],
        "ran_changes": [{"id": n, "old": bool(on[n].get("ran")), "new": bool(nn[n].get("ran"))}
                        for n in sorted(set(on) & set(nn)) if bool(on[n].get("ran")) != bool(nn[n].get("ran"))],
    }


def diff_summary(d: dict) -> str:
    parts = [f"{len(d[k])} {k.replace('_', ' ')}" for k in ("nodes_added", "nodes_removed", "edges_added",
                                                              "edges_removed", "grade_changes", "ran_changes")]
    return "no changes" if not any(d[k] for k in d if isinstance(d[k], list)) else ", ".join(parts)


def spec_for(graph_path: Path) -> Path:
    """Overview spec next to the graph: NAME.minimap.json -> NAME.architecture.json."""
    name = graph_path.name[:-len(".minimap.json")] if graph_path.name.endswith(".minimap.json") else graph_path.stem
    return graph_path.with_name(f"{name}.architecture.json")


def render_text(ix: Index, tree: dict, indent: int = 0, out=None) -> list:
    out = [] if out is None else out
    n = ix.nodes.get(tree["id"], {"kind": "?", "name": tree["id"]})
    loc = f" {n['file']}:{n['line']}" if n.get("file") and n.get("line") else ""
    via = f"  <{', '.join(tree['via'])}>" if tree["via"] else ""
    cyc = "  (cycle)" if tree["cycle"] else ""
    out.append(f"{'  ' * indent}{n['name']} [{n['kind']}] {tree['id']}{loc}{via}{cyc}")
    for c in tree["children"]:
        render_text(ix, c, indent + 1, out)
    return out


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("graph")
    ap.add_argument("--node")
    ap.add_argument("--find")
    ap.add_argument("--dir", choices=DIRS, default="down")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--rollup", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--impact", action="store_true", help="reverse-dependency spread by level (STEP 3)")
    ap.add_argument("--min-grade", choices=("verified", "static", "guess"), default=None,
                    help="edge floor; default guess for --impact, static for --hotspots")
    ap.add_argument("--max-level", type=int, default=10)
    ap.add_argument("--coverage", action="store_true",
                    help="share of functions that ran in the traced suite, per box (or for --node) (STEP 6)")
    ap.add_argument("--arrows", action="store_true", help="trust of every overview arrow (STEP 7)")
    ap.add_argument("--hotspots", action="store_true", help="rank by impact reach x untested share (STEP 8)")
    ap.add_argument("--diff", metavar="OLD_GRAPH", help="changes from OLD_GRAPH to this graph (STEP 11)")
    ap.add_argument("--flow", action="store_true", help="data flow: with --node <data id> writers -> file -> "
                                                         "readers and file chains (--depth, max 5); alone: all files "
                                                         "(STEP 10)")
    ap.add_argument("--path", metavar="TARGET", help="with --node: shortest dependency path node -> TARGET "
                                                      "(STEP 9); exit 1 when there is none")
    ap.add_argument("--kind", choices=("function", "class", "module"), default="function", help="for --hotspots")
    ap.add_argument("--top", type=int, default=10, help="for --hotspots; 0 = all")
    ap.add_argument("--spec", help="overview spec for --arrows (default: <name>.architecture.json next to the graph)")
    ap.add_argument("--stale", metavar="ROOT", nargs="?", const="",
                    help="exit 1 if the map is out of date; ROOT defaults to the root recorded in the graph")
    a = ap.parse_args(argv)
    data = json.loads(read_bytes_retry(Path(a.graph)).decode("utf-8"))
    if a.stale is not None:
        stale = Graph.from_dict(data).stale_files(Path(a.stale) if a.stale else None)
        print(json.dumps({"stale": bool(stale), "files": stale}, ensure_ascii=False))
        return 1 if stale else 0
    ix = Index(data)
    if a.find:
        q = a.find.lower()
        hits = [n for n in data["nodes"] if q in n["id"].lower() or q in n["name"].lower()]
        for n in sorted(hits, key=lambda n: ix.sort_key(n["id"]))[:50]:
            print(f"{n['id']}  [{n['kind']}]  {n.get('file', '')}:{n.get('line', '')}")
        return 0
    if a.arrows:
        spec = Path(a.spec) if a.spec else spec_for(Path(a.graph))
        if not spec.is_file():
            print(f"no overview spec: {spec} (use --spec)", file=sys.stderr)
            return 2
        rows = ix.arrows(json.loads(read_bytes_retry(spec).decode("utf-8")).get("connections", []))
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        print(f"arrow trust ({spec.name}): best grade of the code edges behind each arrow; "
              "dashed = declared, not claimed by code")
        for r in rows:
            g = ", ".join(f"{k} {v}" for k, v in r["grades"].items()) or "-"
            rev = f"; reverse {r['reverse']}" if r["reverse"] else ""
            print(f"  {r['id']:<24} {r['trust']:<12} edges {r['edges']} ({g}){rev}")
        return 0
    if a.hotspots:
        a.min_grade = a.min_grade or "static"
        rows = ix.hotspots(a.kind, a.min_grade, a.top)
        child = ix.child_entry_points(data.get("root"))
        for r in rows:
            if r["id"] in child:
                r["child_process"] = child[r["id"]]
        if a.json:
            print(json.dumps({"traced": ix.traced, "rows": rows}, ensure_ascii=False))
            return 0
        print(f"hotspots ({a.kind}, edges >= {a.min_grade}): score = reach x untested share"
              + ("" if ix.traced else " (graph not traced: no run-time data, nothing to rank)"))
        for i, r in enumerate(rows, 1):
            n = ix.nodes[r["id"]]
            loc = f" {n['file']}:{n['line']}" if n.get("file") and n.get("line") else ""
            tag = f"  [the tests DO run this, in a child process started at {', '.join(r['child_process'])}; the tracer cannot see it]" \
                if r.get("child_process") else ""
            print(f"  {i:>2}. {r['score']:>8g}  reach {r['reach']:<4} untested {r['untested']:.2f} "
                  f"({r['ran']}/{r['functions']} ran)  {r['id']}{loc}{tag}")
        if rows:
            print(CHILD_PROCESS_NOTE)
        return 0
    if a.diff:
        if not Path(a.diff).is_file():
            print(f"no previous graph: {a.diff} (it is kept when a build replaces a published map)",
                  file=sys.stderr)
            return 2
        d = diff_graphs(json.loads(read_bytes_retry(Path(a.diff)).decode("utf-8")), data)
        if a.json:
            print(json.dumps(d, ensure_ascii=False))
            return 0
        print(f"changes from {d['old']} to {d['new']}: {diff_summary(d)}")
        for k, fmt in (("nodes_added", lambda x: f"+ {x}"), ("nodes_removed", lambda x: f"- {x}"),
                       ("edges_added", lambda x: f"+ {x['from']} -{x['kind']}[{x['grade']}]-> {x['to']}  "
                                                 f"{x['evidence']}"),
                       ("edges_removed", lambda x: f"- {x['from']} -{x['kind']}[{x['grade']}]-> {x['to']}  "
                                                   f"(was {x['evidence']})"),
                       ("grade_changes", lambda x: f"~ {x['from']} -{x['kind']}-> {x['to']}: {x['old']} -> "
                                                   f"{x['new']}"),
                       ("ran_changes", lambda x: f"~ {x['id']}: ran {x['old']} -> {x['new']}")):
            if d[k]:
                print(f"{k.replace('_', ' ')} ({len(d[k])})")
                for x in d[k][:50]:
                    print("  " + fmt(x))
                if len(d[k]) > 50:
                    print(f"  ... +{len(d[k]) - 50} more (use --json)")
        return 0
    if a.flow:
        if a.node and a.node not in ix.nodes:
            print(f"unknown node: {a.node!r} (use --find)", file=sys.stderr)
            return 2
        if not a.node:
            rows = [(n, len(ix._flow_rows(n, "writes")), len(ix._flow_rows(n, "reads")),
                     len(ix._flow_rows(n, "uses_path"))) for n in ix.nodes if ix.nodes[n].get("kind") == "data"]
            rows = sorted((r for r in rows if r[1] or r[2]), key=lambda r: (-(r[1] + r[2]), ix.sort_key(r[0])))
            if a.json:
                print(json.dumps([{"file": n, "writers": w, "readers": rd, "path_users": u} for n, w, rd, u in rows]))
                return 0
            print(f"data files with a writer or reader ({len(rows)}); use --node <id> --flow for one file")
            for n, w, rd, u in rows[:30]:
                print(f"  {n:<48} writers {w:<3} readers {rd:<3} path users {u}")
            if len(rows) > 30:
                print(f"  ... +{len(rows) - 30} more (use --json)")
            return 0
        r = ix.dataflow(a.node, a.depth)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
            return 0
        print(f"data flow of {a.node}: writers -> file -> readers (grades as extracted; file access is only seen "
              "statically)")
        for title, key in (("writers", "writers"), ("readers", "readers"), ("path users", "path_users")):
            print(f"{title} ({len(r[key])})")
            for x in r[key]:
                print(f"  {x['id']} [{x['grade']}] {', '.join(x['evidence'])}")
        for title, key, arrow in (("downstream", "downstream", "reads it, writes"), ("upstream", "upstream",
                                                                                    "writes it, reads")):
            print(f"{title} ({len(r[key])})")
            for x in r[key]:
                print(f"  L{x['level']} {x['from']} -> {x['via']} {arrow} {x['file']} [{x['grade']}] {x['evidence']}")
        return 0
    if a.coverage:
        if a.node and a.node not in ix.nodes:
            print(f"unknown node: {a.node!r} (use --find)", file=sys.stderr)
            return 2
        r = ix.coverage(a.node)
        child = ix.child_entry_points(data.get("root"))
        unrun = set(r["never_ran"] if a.node else r["total"]["never_ran"])
        r["child_process_entry_points"] = {k: v for k, v in sorted(child.items()) if k in unrun}
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
            return 0
        pct = lambda row: "-" if row["share"] is None else f"{row['share'] * 100:.1f}%"
        line = lambda name, row: f"  {name:<34} {row['ran']:>4}/{row['functions']:<4} {pct(row)}"
        print("coverage: functions that ran in the traced test suite"
              + ("" if r["traced"] else " (graph not traced: no run-time data, shares not reported)"))
        rows = {a.node: r} if a.node else r["boxes"]
        for name, row in rows.items():
            print(line(name, row))
        if not a.node:
            for name, row in r["outside"].items():
                print(line(f"outside boxes: {name}/", row))
            print(line("total", r["total"]))
        never = [n for row in rows.values() for n in row["never_ran"]]
        print(f"never ran{'' if a.node else ' (inside boxes)'}: {len(never)}")
        for nid in never[:50]:
            n = ix.nodes[nid]
            loc = f" {n['file']}:{n['line']}" if n.get("file") and n.get("line") else ""
            tag = f"  [the tests DO run this, in a child process started at {', '.join(child[nid])}; the tracer cannot see it]" \
                if nid in child else ""
            print(f"  {n['name']} {nid}{loc}{tag}")
        outside_child = {k: v for k, v in r["child_process_entry_points"].items() if k not in never}
        for nid, ev in outside_child.items():  # functions outside boxes (scripts, tests) that tests start
            print(f"  also never ran but started as a child process: {nid} ({', '.join(ev)})")
        if len(never) > 50:
            print(f"  ... +{len(never) - 50} more (use --json)")
        if (r["never_ran"] if a.node else r["total"]["never_ran"]):  # anything unrun, in boxes or not
            print(CHILD_PROCESS_NOTE)
        return 0
    if not a.node or a.node not in ix.nodes:
        print(f"unknown node: {a.node!r} (use --find)", file=sys.stderr)
        return 2
    if a.path is not None:
        if a.path not in ix.nodes:
            print(f"unknown node: {a.path!r} (use --find)", file=sys.stderr)
            return 2
        r = ix.path(a.node, a.path, a.min_grade or "guess")
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif not r["found"]:
            print(f"no path from {a.node} to {a.path} (edges >= {r['min_grade']})")
        else:
            print(f"path {a.node} -> {a.path} (edges >= {r['min_grade']}): {r['hops']} hops, "
                  f"weakest {r['weakest'] or '-'}")
            name = lambda nid: ix.nodes.get(nid, {}).get("name", nid)
            for i, s in enumerate(r["steps"], 1):
                print(f"  {i}. {name(s['from'])} -{s['kind']}[{s['grade']}]-> {name(s['to'])}  {s['evidence']}"
                      f"  ({s['from']} -> {s['to']})")
            if any(s["grade"] == "runtime-only" for s in r["steps"]):
                print("note: a runtime-only step was seen while the tests ran but is not tied to this caller; a "
                      "shared helper that runs callbacks links every caller to every callback. Confirm the chain "
                      "in the code before relying on it.")
        return 0 if r["found"] else 1
    if a.impact:
        a.min_grade = a.min_grade or "guess"
        r = ix.impact(a.node, a.min_grade, a.max_level)
        # data files the node (or anything inside it) writes: their readers are linked only through the file
        r["writes_files"] = sorted({e["to"] for n in ix.descendants(a.node) for e in ix.out.get(n, [])
                                    if e["kind"] == "writes" and e["to"].startswith("data:")})
        r["file_readers"] = ix.file_readers(r["writes_files"])
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
            return 0
        print(f"impact of {a.node} (edges >= {a.min_grade}); level 0 = the node and everything inside it")
        for i, lvl in enumerate(r["levels"], 1):
            print(f"L{i} ({len(lvl)})")
            for nid in lvl[:30]:
                n = ix.nodes.get(nid, {"name": nid, "kind": "?"})
                loc = f" {n['file']}:{n['line']}" if n.get("file") and n.get("line") else ""
                print(f"  {n['name']} [{n['kind']}] {nid}{loc}")
            if len(lvl) > 30:
                print(f"  ... +{len(lvl) - 30} more (use --json)")
        print("boxes:", ", ".join(f"{b} L{l}" for b, l in r["boxes"].items()) or "none")
        print(f"tests reaching it: {len(r['tests'])}")
        if r["writes_files"]:
            print(f"writes files: {', '.join(r['writes_files'])}. Code that READS them is not in the list above "
                  "(it is linked through the file, not by calls). If you change what is written, open each reader "
                  "below and check that it still accepts the new format:")
            for f, rw in r["file_readers"].items():
                print(f"  {f}: {len(rw['readers'])} readers")
                for x in rw["readers"][:30]:
                    print(f"    {x['id']}  {x['evidence']}")
                    if x["callers"]:
                        more = f" (+{len(x['callers']) - 10} more)" if len(x["callers"]) > 10 else ""
                        print(f"      read through it by: {', '.join(x['callers'][:10])}{more}")
                others = [x["id"] for x in rw["writers"] if x["id"] not in ix.descendants(a.node)]
                if others:
                    print(f"    also written by: {', '.join(others[:10])}")
        return 0
    t = ix.tree(a.node, a.dir, a.depth, a.rollup)
    print(json.dumps(t, ensure_ascii=False) if a.json else "\n".join(render_text(ix, t)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

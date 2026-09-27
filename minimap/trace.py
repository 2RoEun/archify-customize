"""Runtime call tracing: run a unittest suite in-process and record caller -> callee pairs (DESIGN.md §4, §7 V3).

CLI: python -m minimap.trace --root R --package P [--tests tests] --graph G.json --out observed.json
The graph file supplies the function nodes used to map code objects to node ids.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ANON = ("<lambda>", "<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>")
MAX_EVIDENCE = 3


class CodeMap:
    """Map code objects to graph node ids: exact def match, or innermost enclosing function for anonymous code."""

    def __init__(self, nodes: list, root: Path):
        self.by_file: dict[str, list] = {}
        self.modules: dict[str, str] = {}
        for n in nodes:
            f = n.get("file")
            if not f:
                continue
            key = os.path.normcase(str((root / f).resolve()))
            if n["kind"] == "function":
                self.by_file.setdefault(key, []).append(n)
            elif n["kind"] in ("module", "test", "script") and n["id"].startswith("mod:"):
                self.modules[key] = n["id"]
        self.cache: dict = {}

    def _key(self, code) -> str:
        return os.path.normcase(os.path.abspath(code.co_filename))

    def exact(self, code) -> str | None:
        hit = self.cache.get(("x", code))
        if hit is not None or ("x", code) in self.cache:
            return hit
        out = None
        for n in self.by_file.get(self._key(code), ()):
            if n["name"] == code.co_name and code.co_firstlineno in (n.get("first", n["line"]), n["line"]):
                out = n["id"]
                break
        self.cache[("x", code)] = out
        return out

    def owner(self, code) -> str | None:
        """Node that a frame's activity belongs to (caller side)."""
        hit = self.cache.get(("o", code))
        if hit is not None or ("o", code) in self.cache:
            return hit
        out = self.exact(code)
        key = self._key(code)
        if out is None and code.co_name in ANON:
            best = None
            for n in self.by_file.get(key, ()):
                lo, hi = n.get("first", n["line"]), n.get("end", n["line"])
                if lo <= code.co_firstlineno <= hi and (best is None or (hi - lo) < (best[1] - best[0])):
                    best = (lo, hi, n["id"])
            out = best[2] if best else None
        if out is None and code.co_name == "<module>":
            out = self.modules.get(key)
        self.cache[("o", code)] = out
        return out


class Recorder:
    def __init__(self, cmap: CodeMap, root: Path):
        self.cmap = cmap
        self.root = root
        self.pairs: dict[tuple, list] = {}
        self.executed: set = set()  # STEP 2: every analysed function that ran
        self.lock = threading.Lock()

    def profile(self, frame, event, arg):
        if event != "call":
            return
        code = frame.f_code
        if code.co_name in ANON:
            return
        callee = self.cmap.exact(code)
        if callee is None:
            return
        self.executed.add(callee)
        f = frame.f_back
        caller = None
        while f is not None:
            caller = self.cmap.owner(f.f_code)
            if caller is not None or f.f_code.co_name not in ANON:
                break
            f = f.f_back
        if caller is None or f is None:
            return
        try:
            rel = Path(f.f_code.co_filename).resolve().relative_to(self.root).as_posix()
        except ValueError:
            rel = f.f_code.co_filename
        ev = f"{rel}:{f.f_lineno}"
        with self.lock:
            lines = self.pairs.setdefault((caller, callee), [])
            if ev not in lines and len(lines) < MAX_EVIDENCE:
                lines.append(ev)


def run(root: Path, graph: dict, tests: str = "tests") -> dict:
    root = Path(root).resolve()
    rec = Recorder(CodeMap(graph["nodes"], root), root)
    old_cwd = os.getcwd()
    os.chdir(root)
    sys.path.insert(0, str(root))
    stream = io.StringIO()
    started = time.perf_counter()
    try:
        # STEP 2: profile from before discovery, so import-time code (decorators, module setup) is seen too
        threading.setprofile(rec.profile)
        sys.setprofile(rec.profile)
        suite = unittest.TestLoader().discover(str(root / tests))
        try:
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        finally:
            sys.setprofile(None)
            threading.setprofile(None)
    finally:
        os.chdir(old_cwd)
    return {
        "suite": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                  "skipped": len(result.skipped), "seconds": round(time.perf_counter() - started, 2),
                  "problems": [str(t) for t, _ in result.failures + result.errors][:20]},
        "pairs": [{"from": a, "to": b, "evidence": ev} for (a, b), ev in sorted(rec.pairs.items())],
        "executed": sorted(rec.executed),
    }


def merge(graph_obj, observed: dict) -> dict:
    """Merge runtime pairs into a Graph (DESIGN.md §3 grades). Returns counts."""
    counts = {"verified_existing": 0, "constructor": 0, "runtime_only": 0}
    for p in observed["pairs"]:
        a, b = p["from"], p["to"]
        ev = p["evidence"]
        cls_edge = None
        if b.endswith(".__init__"):
            cls_id = "cls:" + b[3:-len(".__init__")]
            cls_edge = graph_obj.edges.get((a, cls_id, "calls"))
        if cls_edge is not None:
            graph_obj.add_edge(a, cls_edge["to"], "calls", runtime=True, evidence=ev[0])
            counts["constructor"] += 1
            continue
        existed = (a, b, "calls") in graph_obj.edges
        for e in ev:
            graph_obj.add_edge(a, b, "calls", runtime=True, evidence=e)
        counts["verified_existing" if existed else "runtime_only"] += 1
    ran = set(observed.get("executed", []))
    for nid in ran:
        if nid in graph_obj.nodes:
            graph_obj.nodes[nid]["ran"] = True
    counts["executed"] = len(ran)
    graph_obj.finalize()
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--graph", required=True)
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    graph = json.loads(Path(a.graph).read_text(encoding="utf-8"))
    observed = run(Path(a.root), graph, a.tests)
    Path(a.out).write_bytes((json.dumps(observed, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    s = observed["suite"]
    print(json.dumps({"run": s["run"], "failures": s["failures"], "errors": s["errors"], "skipped": s["skipped"],
                      "seconds": s["seconds"], "pairs": len(observed["pairs"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

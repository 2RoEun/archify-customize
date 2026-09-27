"""STEP 9 (Phase 4) — path finder: shortest dependency path with evidence lines (hand-derived answers)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.graph import Graph
from minimap.query import Index

HERE = Path(__file__).resolve().parent
E = "fn:fixpkg.core.Engine"
H = "fn:fixpkg.util.helper"


def hops(r):
    return [(s["from"], s["to"], s["kind"], s["grade"], s["evidence"]) for s in r["steps"]]


class Step9Path(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = extract(HERE / "fixtures", "fixpkg", extra_dirs={})
        for c, m in (("core", "mod:fixpkg.core"), ("util", "mod:fixpkg.util")):
            g.add_node(f"concept:{c}", "concept", c)
            g.add_edge(f"concept:{c}", m, "maps", static="confirmed")
        cls.data = g.to_dict()
        cls.ix = Index(cls.data)

    def test_s9_1_known_path(self):
        # core.py: aio awaits self.run() (line 55); run calls top() in a comprehension (line 37); top calls hp (line 10)
        r = self.ix.path(f"{E}.aio", H)
        self.assertEqual((r["found"], r["hops"], r["weakest"]), (True, 3, "static"))
        self.assertEqual(hops(r), [
            (f"{E}.aio", f"{E}.run", "calls", "static", "fixpkg/core.py:55"),
            (f"{E}.run", "fn:fixpkg.core.top", "calls", "static", "fixpkg/core.py:37"),
            ("fn:fixpkg.core.top", H, "calls", "static", "fixpkg/core.py:10"),
        ])
        # Engine.step contains inner, so the walk starts from inner as well (level 0 = the node and what is inside,
        # as in impact): inner -> plain (line 41) -> helper (line 15); the step -> inner call is not a hop
        r = self.ix.path(f"{E}.step", H)
        self.assertEqual([(s["from"], s["evidence"]) for s in r["steps"]],
                         [(f"{E}.step.inner", "fixpkg/core.py:41"), ("fn:fixpkg.core.plain", "fixpkg/core.py:15")])

    def test_s9_2_boxes_and_self(self):
        # box to box: the module import on core.py line 4 is the first 1-hop edge in sort order
        r = self.ix.path("concept:core", "concept:util")
        self.assertEqual(hops(r), [("mod:fixpkg.core", "mod:fixpkg.util", "imports", "static", "fixpkg/core.py:4")])
        r = self.ix.path("concept:core", f"{E}.run")  # run is inside core: nothing to walk
        self.assertEqual((r["found"], r["hops"], r["steps"], r["weakest"]), (True, 0, [], None))

    def test_s9_3_no_path_and_grade_floor(self):
        r = self.ix.path(H, f"{E}.aio")  # helper uses nothing
        self.assertEqual((r["found"], r["hops"], r["steps"]), (False, None, []))
        d = "data:data/events.jsonl"
        r = self.ix.path(f"{E}.__init__", d)  # line 28 builds the path: only a guess edge
        self.assertEqual(hops(r), [(f"{E}.__init__", d, "uses_path", "guess", "fixpkg/core.py:28")])
        self.assertEqual(r["weakest"], "guess")
        self.assertFalse(self.ix.path(f"{E}.__init__", d, "static")["found"])

    def test_s9_4_cycle(self):
        g = Graph()
        for n in ("a", "b", "c", "d"):
            g.add_node(f"fn:{n}", "function", n)
        g.add_edge("fn:a", "fn:b", "calls", static="confirmed", evidence="x.py:1")
        g.add_edge("fn:b", "fn:a", "calls", static="confirmed", evidence="x.py:2")
        g.add_edge("fn:b", "fn:c", "calls", static="confirmed", evidence="x.py:3")
        ix = Index(g.to_dict())
        self.assertEqual([(s["from"], s["to"]) for s in ix.path("fn:a", "fn:c")["steps"]],
                         [("fn:a", "fn:b"), ("fn:b", "fn:c")])
        self.assertFalse(ix.path("fn:a", "fn:d")["found"])  # the a <-> b loop ends without a target
        self.assertFalse(ix.path("fn:c", "fn:a")["found"])
        self.assertEqual(ix.path("fn:b", "fn:b")["hops"], 0)

    def test_s9_5_best_edge_per_hop(self):
        g = Graph()
        for n in ("a", "b"):
            g.add_node(f"fn:{n}", "function", n)
        g.add_edge("fn:a", "fn:b", "calls", static="guess", evidence="x.py:1")
        g.add_edge("fn:a", "fn:b", "imports", static="confirmed", evidence="x.py:9")
        self.assertEqual(hops(Index(g.to_dict()).path("fn:a", "fn:b")),
                         [("fn:a", "fn:b", "imports", "static", "x.py:9")])

    def test_s9_6_cli(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "g.json"
            p.write_text(json.dumps(self.data), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
            run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(p), *a],
                                            capture_output=True, text=True, encoding="utf-8", env=env)
            r = run("--node", f"{E}.aio", "--path", H)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("(edges >= guess): 3 hops, weakest static", r.stdout)
            self.assertIn("  3. top -calls[static]-> helper  fixpkg/core.py:10", r.stdout)
            r = run("--node", H, "--path", f"{E}.aio")
            self.assertEqual((r.returncode, r.stdout.strip()),
                             (1, f"no path from {H} to {E}.aio (edges >= guess)"))
            self.assertEqual(json.loads(run("--node", f"{E}.aio", "--path", H, "--json").stdout)["hops"], 3)
            self.assertEqual(run("--node", H, "--path", "fn:nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()

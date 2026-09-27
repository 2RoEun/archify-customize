"""STEP 8 (Phase 4) — hotspots: score = impact reach (STEP 3) x untested share (STEP 6), hand-computed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.graph import Graph
from minimap.query import Index

HERE = Path(__file__).resolve().parent


def small_graph(traced: bool = True) -> dict:
    """a, d, e, f, K.m1 ran; b, c, K.m2 did not.
    a->b, d->b, d->a, e->K, f->K.m2 static calls; a->c a guess call."""
    g = Graph()
    for n in ("a", "b", "c", "d", "e", "f"):
        g.add_node(f"fn:{n}", "function", n)
    g.add_node("cls:K", "class", "K")
    for m in ("m1", "m2"):
        g.add_node(f"fn:K.{m}", "function", f"K.{m}")
        g.add_edge("cls:K", f"fn:K.{m}", "contains", static="confirmed")
    for s, t in (("a", "b"), ("d", "b"), ("d", "a"), ("f", "K.m2")):
        g.add_edge(f"fn:{s}", f"fn:{t}", "calls", static="confirmed")
    g.add_edge("fn:e", "cls:K", "calls", static="confirmed")
    g.add_edge("fn:a", "fn:c", "calls", static="guess")
    data = g.to_dict()
    for n in data["nodes"]:
        if traced and n["id"] in ("fn:a", "fn:d", "fn:e", "fn:f", "fn:K.m1"):
            n["ran"] = True
    return data


class Step8Hotspots(unittest.TestCase):
    def test_s8_1_formula_functions(self):
        ix = Index(small_graph())
        # b: reach {a, d} = 2, never ran -> 2. K.m2: reach {f} = 1 -> 1. c: only a guess edge -> reach 0 at static.
        # a and the other run functions: untested 0 -> left out.
        self.assertEqual([(r["id"], r["score"], r["reach"], r["untested"]) for r in ix.hotspots()],
                         [("fn:b", 2, 2, 1.0), ("fn:K.m2", 1, 1, 1.0)])
        # at guess, c reaches a then d (reach 2) and ties with b; ties go by name
        self.assertEqual([(r["id"], r["score"]) for r in ix.hotspots(min_grade="guess")],
                         [("fn:b", 2), ("fn:c", 2), ("fn:K.m2", 1)])
        self.assertEqual([r["id"] for r in ix.hotspots(top=1)], ["fn:b"])

    def test_s8_2_formula_class_share(self):
        # K: level 0 = {K, K.m1, K.m2}; users e (constructor) and f -> reach 2; 1 of 2 methods ran -> 2 x 0.5
        self.assertEqual(Index(small_graph()).hotspots(kind="class"),
                         [{"id": "cls:K", "score": 1.0, "reach": 2, "untested": 0.5, "ran": 1, "functions": 2}])

    def test_s8_3_untraced_graph_has_no_hotspots(self):
        self.assertEqual(Index(small_graph(traced=False)).hotspots(), [])

    def test_s8_4_cli(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "g.json"
            p.write_text(json.dumps(small_graph()), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
            run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(p), "--hotspots", *a],
                                            capture_output=True, text=True, encoding="utf-8", env=env)
            txt = run()
            self.assertEqual(txt.returncode, 0, txt.stderr)
            self.assertIn("   1.        2  reach 2    untested 1.00 (0/1 ran)  fn:b", txt.stdout)
            # the CLI default floor must equal the Python default (static): c is reached only by a guess edge
            self.assertIn("edges >= static", txt.stdout)
            self.assertNotIn("fn:c", txt.stdout)
            self.assertIn("fn:c", run("--min-grade", "guess").stdout)
            self.assertEqual([r["id"] for r in json.loads(run("--kind", "class", "--json").stdout)["rows"]], ["cls:K"])


if __name__ == "__main__":
    unittest.main()

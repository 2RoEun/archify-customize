"""STEP 6 (Phase 4) — coverage: share of analysed functions that ran, per box and per node (hand-derived answers)."""
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
from minimap.trace import merge

HERE = Path(__file__).resolve().parent
RT = HERE / "fixtures" / "rt"
M = "fn:rtpkg.mod"
# rtpkg/mod.py defines 14 functions (deco.wrapper and the 4 Box methods included); the suite calls all but one
MOD_FUNCS = 14


def traced_rt(g: Graph, tmp: Path) -> Graph:
    """Trace the rt fixture suite into graph g (saved as tmp/g.json) and return the merged graph."""
    gp, op = tmp / "g.json", tmp / "obs.json"
    g.save(gp)
    env = dict(os.environ, PYTHONPATH=str(HERE.parent), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-B", "-m", "minimap.trace", "--root", str(RT), "--graph", str(gp),
                           "--out", str(op)], capture_output=True, text=True, env=env, cwd=str(HERE.parent))
    if proc.returncode:
        raise AssertionError(proc.stderr)
    traced = Graph.load(gp)
    merge(traced, json.loads(op.read_text(encoding="utf-8")))
    traced.save(gp)
    return traced


class Step6Coverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = extract(RT, "rtpkg", extra_dirs={"tests": "test"})
        g.add_node("concept:mod", "concept", "mod")
        g.add_edge("concept:mod", "mod:rtpkg.mod", "maps", static="confirmed")
        g.add_node("concept:empty", "concept", "empty")
        cls.untraced = g.to_dict()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.gp = Path(cls.tmp.name) / "g.json"
        cls.data = traced_rt(g, Path(cls.tmp.name)).to_dict()
        cls.ix = Index(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_s6_1_per_box(self):
        r = self.ix.coverage()
        self.assertTrue(r["traced"])
        self.assertEqual(r["boxes"]["concept:mod"], {"functions": MOD_FUNCS, "ran": MOD_FUNCS - 1,
                                                     "share": round(13 / 14, 4), "never_ran": [f"{M}.never_called"]})
        self.assertEqual(r["boxes"]["concept:empty"], {"functions": 0, "ran": 0, "share": None, "never_ran": []})
        self.assertEqual(r["outside"], {"tests": {"functions": 1, "ran": 1, "share": 1.0, "never_ran": []}})
        self.assertEqual((r["total"]["functions"], r["total"]["ran"]), (MOD_FUNCS + 1, MOD_FUNCS))

    def test_s6_2_node_subtree(self):
        self.assertEqual(self.ix.coverage("cls:rtpkg.mod.Box"),
                         {"traced": True, "node": "cls:rtpkg.mod.Box", "functions": 4, "ran": 4, "share": 1.0,
                          "never_ran": []})
        self.assertEqual(self.ix.coverage(f"{M}.never_called")["share"], 0.0)

    def test_s6_3_untraced_graph_reports_no_share(self):
        r = Index(self.untraced).coverage()
        self.assertFalse(r["traced"])
        self.assertEqual(r["boxes"]["concept:mod"]["share"], None)
        self.assertEqual(r["boxes"]["concept:mod"]["functions"], MOD_FUNCS)

    def test_s6_4_cli(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
        run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(self.gp), *a],
                                        capture_output=True, text=True, encoding="utf-8", env=env)
        txt = run("--coverage")
        self.assertEqual(txt.returncode, 0, txt.stderr)
        self.assertIn("concept:mod", txt.stdout)
        self.assertIn("  13/14   92.9%", txt.stdout)
        self.assertIn("never ran (inside boxes): 1", txt.stdout)
        self.assertIn("never_called fn:rtpkg.mod.never_called rtpkg/mod.py:59", txt.stdout)
        j = run("--coverage", "--node", "cls:rtpkg.mod.Box", "--json")
        self.assertEqual(json.loads(j.stdout)["ran"], 4)
        self.assertEqual(run("--coverage", "--node", "fn:nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()

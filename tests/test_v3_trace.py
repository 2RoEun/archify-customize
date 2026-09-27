"""V3 Tracer units on a runnable fixture with hand-derived answers (DESIGN.md §7)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.graph import Graph
from minimap.trace import merge

HERE = Path(__file__).resolve().parent
RT = HERE / "fixtures" / "rt"
M, T = "fn:rtpkg.mod", "fn:tests.test_rt.RT.test_all"
BOX = "cls:rtpkg.mod.Box"

EXPECTED = {
    (T, f"{M}.decorated"): "static",          # run time goes through the decorator's wrapper instead
    (T, f"{M}.use_box"): "verified",
    (T, f"{M}.via_lambda"): "verified",
    (T, f"{M}.via_genexpr"): "verified",
    (T, f"{M}.dynamic"): "verified",
    (T, f"{M}.threaded"): "verified",
    (T, f"{M}.deco.wrapper"): "runtime-only",
    (f"{M}.deco.wrapper", f"{M}.decorated"): "runtime-only",
    (f"{M}.decorated", f"{M}.leaf"): "verified",
    (f"{M}.Box.__init__", f"{M}.leaf"): "verified",
    (f"{M}.use_box", BOX): "verified",                      # constructor: __init__ observed
    (f"{M}.use_box", f"{M}.Box.get_value"): "verified",     # guess proven at run time
    (f"{M}.use_box", f"{M}.Box.__enter__"): "runtime-only",
    (f"{M}.use_box", f"{M}.Box.__exit__"): "runtime-only",
    (f"{M}.via_lambda", f"{M}.leaf"): "verified",           # call inside a lambda
    (f"{M}.via_genexpr", f"{M}.leaf"): "verified",          # call inside a generator expression
    (f"{M}.dynamic", BOX): "verified",
    (f"{M}.dynamic", f"{M}.Box.get_value"): "runtime-only",  # getattr(...)()
    (f"{M}.threaded", f"{M}.leaf"): "verified",             # lambda run in another thread
    (f"{M}.never_called", f"{M}.leaf"): "static",
    ("mod:rtpkg.mod", f"{M}.deco"): "runtime-only",   # STEP 2: @deco applied at import time
}


class V3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = extract(RT, "rtpkg", extra_dirs={"tests": "test"})
        cls.tmp = tempfile.TemporaryDirectory()
        gp = Path(cls.tmp.name) / "g.json"
        op = Path(cls.tmp.name) / "obs.json"
        g.save(gp)
        env = dict(os.environ, PYTHONPATH=str(HERE.parent), PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, "-B", "-m", "minimap.trace", "--root", str(RT), "--graph", str(gp),
                               "--out", str(op)], capture_output=True, text=True, env=env, cwd=str(HERE.parent))
        if proc.returncode:
            raise AssertionError(proc.stderr)
        cls.observed = json.loads(op.read_text(encoding="utf-8"))
        cls.graph = Graph.load(gp)
        cls.counts = merge(cls.graph, cls.observed)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_v3_0_suite_ran_clean(self):
        s = self.observed["suite"]
        self.assertEqual((s["run"], s["failures"], s["errors"]), (1, 0, 0), s)

    def test_v3_1_and_2_pairs_and_attribution(self):
        got = {(e["from"], e["to"]): e["grade"] for e in self.graph.edges.values() if e["kind"] == "calls"}
        self.assertEqual(got, EXPECTED)
        thr = self.graph.edges[(f"{M}.threaded", f"{M}.leaf", "calls")]
        self.assertIn("rtpkg/mod.py:53", thr["evidence"])

    def test_v3_3_merge_counts(self):
        self.assertEqual(self.counts, {"verified_existing": 11, "constructor": 2, "runtime_only": 6, "executed": 14})

    def test_v3_4_executed_set(self):  # STEP 2
        pkg = {n["id"] for n in self.graph.nodes.values() if n["kind"] == "function" and n["id"].startswith(M + ".")}
        ran = {n for n in pkg if self.graph.nodes[n].get("ran")}
        self.assertEqual(pkg - ran, {f"{M}.never_called"})
        self.assertTrue(self.graph.nodes[T].get("ran"))


if __name__ == "__main__":
    unittest.main()

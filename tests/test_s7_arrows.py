"""STEP 7 (Phase 4) — arrow trust: best grade of the code edges behind each overview arrow (hand-derived answers)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.query import Index
from tests.test_s6_coverage import RT, traced_rt

HERE = Path(__file__).resolve().parent
U = "fn:fixpkg.util.helper"
CONNECTIONS = [
    {"id": "core-to-util", "from": "core", "to": "util"},
    {"id": "util-to-core", "from": "util", "to": "core"},                    # drawn against the code
    {"id": "core-to-sub", "from": "core", "to": "sub", "variant": "dashed"},  # declared only
    {"id": "sub-to-util", "from": "sub", "to": "util", "variant": "emphasis"},
    {"id": "owner-to-core", "from": "owner", "to": "core"},                   # owner has no code
]


class Step7Arrows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = extract(HERE / "fixtures", "fixpkg", extra_dirs={})
        for c, m in (("core", "mod:fixpkg.core"), ("util", "mod:fixpkg.util"), ("sub", "mod:fixpkg.sub.deep")):
            g.add_node(f"concept:{c}", "concept", c)
            g.add_edge(f"concept:{c}", m, "maps", static="confirmed")
        g.add_node("concept:owner", "concept", "owner")
        g.add_node("concept:all", "concept", "all")  # overlaps core: the whole package
        g.add_edge("concept:all", "mod:fixpkg", "maps", static="confirmed")
        cls.data = g.to_dict()
        cls.rows = {r["id"]: r for r in Index(cls.data).arrows(CONNECTIONS)}

    def test_s7_1_static_arrow(self):
        # fixpkg/core.py: line 4 imports util, line 5 imports helper, top (line 10) and plain (line 15) call helper
        r = self.rows["core-to-util"]
        self.assertEqual((r["trust"], r["edges"], r["grades"], r["reverse"]), ("static", 4, {"static": 4}, 0))
        self.assertEqual(r["evidence"][0], f"fn:fixpkg.core.plain -calls-> {U} [static] fixpkg/core.py:15")
        self.assertEqual(r["variant"], "solid")
        r = self.rows["sub-to-util"]  # deep.py line 1 import, line 5 call; emphasis is still a claim
        self.assertEqual((r["trust"], r["edges"], r["variant"]), ("static", 2, "emphasis"))

    def test_s7_2_backwards_dashed_and_not_code(self):
        r = self.rows["util-to-core"]
        self.assertEqual((r["trust"], r["edges"], r["grades"], r["reverse"]), ("none", 0, {}, 4))
        r = self.rows["core-to-sub"]  # plain calls deep_fn (line 16), core imports it (line 6): shown, not claimed
        self.assertEqual((r["trust"], r["edges"], r["grades"]), ("declared", 2, {"static": 2}))
        self.assertEqual((self.rows["owner-to-core"]["trust"], self.rows["owner-to-core"]["edges"]), ("not-code", 0))

    def test_s7_2b_overlapping_boxes_skip_internal_edges(self):
        # core -> all: only core's edges leaving core count (4 into util + 2 into sub), not calls inside core
        r = Index(self.data).arrow("concept:core", "concept:all")
        self.assertEqual((r["trust"], r["edges"], r["grades"]), ("static", 6, {"static": 6}))

    def test_s7_3_runtime_grades(self):
        g = extract(RT, "rtpkg", extra_dirs={"tests": "test"})
        for c, m in (("mod", "mod:rtpkg.mod"), ("tests", "mod:tests.test_rt")):
            g.add_node(f"concept:{c}", "concept", c)
            g.add_edge(f"concept:{c}", m, "maps", static="confirmed")
        with tempfile.TemporaryDirectory() as t:
            ix = Index(traced_rt(g, Path(t)).to_dict())
        r = ix.arrow("concept:tests", "concept:mod")
        # test_all -> use_box, via_lambda, via_genexpr, dynamic, threaded observed (verified); -> decorated static only
        # (run time goes through the wrapper, runtime-only); the module import is static
        self.assertEqual((r["trust"], r["grades"]), ("verified", {"verified": 5, "static": 2, "runtime-only": 1}))
        self.assertEqual((ix.arrow("concept:mod", "concept:tests")["trust"],
                          ix.arrow("concept:mod", "concept:tests")["reverse"]), ("none", 8))

    def test_s7_4_cli(self):
        with tempfile.TemporaryDirectory() as t:
            gp = Path(t) / "fx.minimap.json"
            gp.write_text(json.dumps(self.data), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
            run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(gp), "--arrows", *a],
                                            capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(run().returncode, 2)  # no fx.architecture.json yet
            (Path(t) / "fx.architecture.json").write_text(json.dumps({"connections": CONNECTIONS}), encoding="utf-8")
            txt = run()
            self.assertEqual(txt.returncode, 0, txt.stderr)
            self.assertIn("core-to-util             static       edges 4 (static 4)", txt.stdout)
            self.assertIn("util-to-core             none         edges 0 (-); reverse 4", txt.stdout)
            self.assertEqual([r["trust"] for r in json.loads(run("--json").stdout)],
                             ["static", "none", "declared", "static", "not-code"])


if __name__ == "__main__":
    unittest.main()

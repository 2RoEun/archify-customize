"""STEP 11 (Phase 4) — change diff: planted mutations (add a function, remove a call) appear exactly in the diff."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.query import diff_graphs, diff_summary
from tests.test_v7_concurrency import make_project

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
EMPTY = {"nodes_added": [], "nodes_removed": [], "edges_added": [], "edges_removed": [], "grade_changes": [],
         "ran_changes": []}


def edges(rows):
    return [(r["from"], r["to"], r["kind"], r["grade"]) for r in rows]


def mutate(path: Path, old: str, new: str, append: str = "") -> None:
    src = path.read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    path.write_text(src.replace(old, new) + append, encoding="utf-8")


class Step11Diff(unittest.TestCase):
    def test_s11_1_add_function_remove_call(self):
        with tempfile.TemporaryDirectory() as t:
            shutil.copytree(HERE / "fixtures" / "fixpkg", Path(t) / "fixpkg",
                            ignore=shutil.ignore_patterns("__pycache__"))
            g0 = extract(Path(t), "fixpkg", extra_dirs={}).to_dict()
            # util.py: append added() (lines 21-22) calling helper; core.py: plain stops calling deep_fn (line 16)
            mutate(Path(t) / "fixpkg" / "util.py", 'LOCK_FILE = ".state.lock"\n', 'LOCK_FILE = ".state.lock"\n',
                   "\n\ndef added():\n    return helper(5)\n")
            mutate(Path(t) / "fixpkg" / "core.py", "    deep_fn()\n", "    pass\n")
            g1 = extract(Path(t), "fixpkg", extra_dirs={}).to_dict()
        d = diff_graphs(g0, g1)
        self.assertEqual(d["nodes_added"], ["fn:fixpkg.util.added"])
        self.assertEqual(edges(d["edges_added"]), [("fn:fixpkg.util.added", "fn:fixpkg.util.helper", "calls", "static"),
                                                   ("mod:fixpkg.util", "fn:fixpkg.util.added", "contains", "static")])
        self.assertEqual(d["edges_added"][0]["evidence"], "fixpkg/util.py:22")
        self.assertEqual(edges(d["edges_removed"]),
                         [("fn:fixpkg.core.plain", "fn:fixpkg.sub.deep.deep_fn", "calls", "static")])
        self.assertEqual(d["edges_removed"][0]["evidence"], "fixpkg/core.py:16")
        self.assertEqual({k: d[k] for k in ("nodes_removed", "grade_changes", "ran_changes")},
                         {"nodes_removed": [], "grade_changes": [], "ran_changes": []})

    def test_s11_2_grade_and_ran_changes_and_identity(self):
        g0 = extract(HERE / "fixtures", "fixpkg", extra_dirs={}).to_dict()
        self.assertEqual({k: v for k, v in diff_graphs(g0, g0).items() if k in EMPTY}, EMPTY)
        self.assertEqual(diff_summary(diff_graphs(g0, g0)), "no changes")
        g1 = copy.deepcopy(g0)
        e = next(e for e in g1["edges"] if e["grade"] == "guess")
        e["grade"] = "verified"
        n = next(n for n in g1["nodes"] if n["id"] == "fn:fixpkg.util.helper")
        n["ran"] = True
        d = diff_graphs(g0, g1)
        self.assertEqual(d["grade_changes"], [{"from": e["from"], "to": e["to"], "kind": e["kind"],
                                               "old": "guess", "new": "verified"}])
        self.assertEqual(d["ran_changes"], [{"id": "fn:fixpkg.util.helper", "old": False, "new": True}])
        self.assertEqual(d["edges_added"] + d["edges_removed"] + d["nodes_added"] + d["nodes_removed"], [])

    def test_s11_3_run_py_keeps_previous_graph_and_mm_diff_reports_it(self):
        """End to end: build, mutate the project, build again, then `mm.py --diff` (bare) shows exactly the change."""
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as state:
            root = Path(t)
            d = make_project(root)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(TOOL), MINIMAP_STATE_DIR=state)
            build = lambda: subprocess.run(
                [sys.executable, "-B", str(TOOL / "run.py"), "--root", str(root), "--package", "rtpkg", "--name", "rt",
                 "--expect-run", "1", "--expect-skip", "0"],
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
            first = build()
            self.assertEqual(first.returncode, 0, first.stdout[-2000:])
            self.assertFalse((Path(state) / "prev" / "rt.minimap.json").exists())  # nothing was replaced yet
            self.assertIn("peak memory of the build:", first.stdout)
            mod = root / "rtpkg" / "mod.py"  # never_called stops calling leaf; a new function added() calls it
            mutate(mod, "def never_called():\n    return leaf()\n", "def never_called():\n    return 1\n",
                   "\n\ndef added():\n    return leaf()\n")
            second = build()
            self.assertEqual(second.returncode, 0, second.stdout[-2000:])
            self.assertTrue((Path(state) / "prev" / "rt.minimap.json").is_file())
            self.assertIn("changes vs previous", second.stdout)
            out = subprocess.run([sys.executable, "-B", str(TOOL / "mm.py"), "--graph", str(d / "rt.minimap.json"),
                                  "--diff", "--json"], capture_output=True, text=True, encoding="utf-8", env=env,
                                 timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            r = json.loads(out.stdout)
            M = "fn:rtpkg.mod"
            self.assertEqual(r["nodes_added"], [f"{M}.added"])
            self.assertEqual(edges(r["edges_added"]), [(f"{M}.added", f"{M}.leaf", "calls", "static"),
                                                       ("mod:rtpkg.mod", f"{M}.added", "contains", "static")])
            self.assertEqual(edges(r["edges_removed"]), [(f"{M}.never_called", f"{M}.leaf", "calls", "static")])
            self.assertEqual((r["nodes_removed"], r["grade_changes"], r["ran_changes"]), ([], [], []))
            missing = subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(d / "rt.minimap.json"), "--diff",
                                      str(root / "nope.json")], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()

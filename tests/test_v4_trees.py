"""V4 Tree rules and human = AI equivalence (DESIGN.md §6, §7)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.graph import Graph
from minimap.query import Index

HERE = Path(__file__).resolve().parent
NODE = os.environ.get("MINIMAP_NODE") or shutil.which("node") or "node"
E = "fn:fixpkg.core.Engine"


def ids(tree):
    return [c["id"] for c in tree["children"]]


class V4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = extract(HERE / "fixtures", "fixpkg", extra_dirs={}).to_dict()
        cls.ix = Index(cls.data)

    def test_v4_1_down_deps_users(self):
        t = self.ix.tree("mod:fixpkg.util", "down", 2)
        self.assertEqual(ids(t), ["cls:fixpkg.util.Worker", "fn:fixpkg.util.helper", "fn:fixpkg.util.save_cache"])
        self.assertEqual(ids(t["children"][0]), ["fn:fixpkg.util.Worker.step"])
        t = self.ix.tree("fn:fixpkg.core.plain", "deps", 1)
        self.assertEqual(ids(t), ["fn:fixpkg.sub.deep.deep_fn", "fn:fixpkg.util.helper", "fn:fixpkg.core.top"])
        self.assertEqual(t["children"][0]["via"], ["calls:static"])
        t = self.ix.tree("fn:fixpkg.util.helper", "users", 1)
        self.assertEqual(ids(t), ["mod:fixpkg.core", "mod:fixpkg.sub.deep", "fn:fixpkg.sub.deep.deep_fn",
                                  "fn:fixpkg.core.plain", "fn:fixpkg.util.Worker.step", "fn:fixpkg.core.top"])

    def test_v4_2_rollup_excludes_internal(self):
        t = self.ix.tree("cls:fixpkg.core.Engine", "deps", 1, rollup=True)
        self.assertEqual(ids(t), ["fn:fixpkg.core.Base.ping", "fn:fixpkg.core.plain",
                                  "fn:fixpkg.core.top", "data:data/events.jsonl"])
        self.assertEqual(t["children"][-1]["via"], ["reads:guess", "uses_path:guess", "writes:guess"])
        self.assertEqual(ids(self.ix.tree("cls:fixpkg.core.Engine", "users", 1, rollup=True)), ["mod:fixpkg"])
        self.assertEqual(ids(self.ix.tree("cls:fixpkg.core.Engine", "users", 1)), ["mod:fixpkg", f"{E}.run"])

    def test_v4_2b_data_users_are_readers_and_writers(self):
        t = self.ix.tree("data:data/events.jsonl", "users", 1)
        self.assertEqual(ids(t), [f"{E}.__init__", f"{E}.load", f"{E}.save"])
        self.assertEqual([c["via"] for c in t["children"]], [["uses_path:guess"], ["reads:guess"], ["writes:guess"]])

    def test_v4_3_cycle_and_depth(self):
        g = Graph()
        for n in ("fn:a", "fn:b", "fn:c"):
            g.add_node(n, "function", n[3:])
        g.add_edge("fn:a", "fn:b", "calls", static="confirmed")
        g.add_edge("fn:b", "fn:a", "calls", static="confirmed")
        g.add_edge("fn:b", "fn:c", "calls", static="confirmed")
        ix = Index(g.to_dict())
        t = ix.tree("fn:a", "deps", 5)
        b = t["children"][0]
        self.assertEqual(ids(b), ["fn:a", "fn:c"])
        self.assertTrue(b["children"][0]["cycle"])
        self.assertEqual(b["children"][0]["children"], [])
        self.assertEqual(ix.tree("fn:a", "deps", 1)["children"][0]["children"], [])

    def test_v4_4_panel_js_equals_query_py(self):
        if not Path(NODE).is_file():
            self.fail(f"node.exe not found at {NODE}")
        graphs = [self.data]
        # also the runtime-merged fixture, so all four grades take part
        rt = extract(HERE / "fixtures" / "rt", "rtpkg", extra_dirs={"tests": "test"})
        rt.add_edge("fn:rtpkg.mod.use_box", "fn:rtpkg.mod.Box.__enter__", "calls", runtime=True, evidence="x:1")
        rt.add_edge("fn:rtpkg.mod.use_box", "fn:rtpkg.mod.Box.get_value", "calls", runtime=True, evidence="x:2")
        graphs.append(rt.to_dict())
        with tempfile.TemporaryDirectory() as tmp:
            for k, data in enumerate(graphs):
                p = Path(tmp) / f"g{k}.json"
                p.write_text(json.dumps(data), encoding="utf-8")
                proc = subprocess.run([NODE, str(HERE / "run_panel.js"), str(p), "3"], capture_output=True,
                                      text=True, encoding="utf-8")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                js = json.loads(proc.stdout)
                ix = Index(data)
                py = {f"{n['id']}|{d}|{str(r).lower()}": ix.tree(n["id"], d, 3, r)
                      for n in data["nodes"] for d in ("down", "deps", "users") for r in (False, True)}
                self.assertEqual(set(js), set(py))
                diff = [key for key in py if py[key] != js[key]]
                self.assertEqual(diff, [], f"graph {k}: {len(diff)} trees differ, first {diff[:3]}")
                self.assertGreater(len(py), 100)


if __name__ == "__main__":
    unittest.main()

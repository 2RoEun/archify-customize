"""STEP 10 (Phase 4) — data flow: writers -> file -> readers, file chains, bounded walk (hand-derived answers)."""
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
W = "fn:fixpkg.core.writer"


def row(nid, line):
    return {"id": nid, "grade": "guess", "evidence": [f"fixpkg/core.py:{line}"]}


def cycle_graph() -> dict:
    """f1 reads a, writes b; f2 reads b, writes c; f3 reads c, writes a (a file cycle)."""
    g = Graph()
    for f in ("a", "b", "c"):
        g.add_node(f"data:{f}", "data", f)
    for fn, src, dst in (("f1", "a", "b"), ("f2", "b", "c"), ("f3", "c", "a")):
        g.add_node(f"fn:{fn}", "function", fn)
        g.add_edge(f"fn:{fn}", f"data:{src}", "reads", static="guess", evidence=f"x.py:{fn}r")
        g.add_edge(f"fn:{fn}", f"data:{dst}", "writes", static="guess", evidence=f"x.py:{fn}w")
    return g.to_dict()


class Step10DataFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = extract(HERE / "fixtures", "fixpkg", extra_dirs={}).to_dict()
        cls.ix = Index(cls.data)

    def test_s10_1_writers_readers_users(self):
        # core.py: Engine.__init__ builds the path (line 28), load reads it (45), save writes it (48)
        r = self.ix.dataflow("data:data/events.jsonl")
        self.assertEqual(r["writers"], [row(f"{E}.save", 48)])
        self.assertEqual(r["readers"], [row(f"{E}.load", 45)])
        self.assertEqual(r["path_users"], [row(f"{E}.__init__", 28)])
        self.assertEqual((r["downstream"], r["upstream"]), ([], []))  # load writes nothing, save reads nothing

    def test_s10_2_file_chain(self):
        # writer() writes out.json (line 59) and reads in.txt (line 61): in.txt -> writer -> out.json
        down = self.ix.dataflow("data:in.txt")["downstream"]
        self.assertEqual(down, [{"level": 1, "from": "data:in.txt", "via": W, "file": "data:out.json",
                                 "grade": "guess", "evidence": "fixpkg/core.py:59"}])
        up = self.ix.dataflow("data:out.json")["upstream"]
        self.assertEqual([(x["from"], x["via"], x["file"], x["evidence"]) for x in up],
                         [("data:out.json", W, "data:in.txt", "fixpkg/core.py:61")])

    def test_s10_2b_joined_paths_and_local_names(self):
        # fixtures/flow/flowpkg/io.py, hand-read line by line
        ix = Index(extract(HERE / "fixtures" / "flow", "flowpkg", extra_dirs={}).to_dict())
        F = "fn:flowpkg.io"
        ev = lambda rows: [(x["id"][len(F) + 1:], x["evidence"]) for x in rows]
        r = ix.dataflow("data:data/log.jsonl")  # / chain (line 6, 11, 23) and os.path.join (line 16) give one file
        self.assertEqual(ev(r["writers"]), [("write_log", ["flowpkg/io.py:6"])])
        self.assertEqual(ev(r["readers"]), [("copy_log", ["flowpkg/io.py:18"]), ("read_log", ["flowpkg/io.py:12"])])
        self.assertEqual(ev(r["path_users"]), [("copy_log", ["flowpkg/io.py:16"]), ("read_log", ["flowpkg/io.py:11"]),
                                               ("rebinds", ["flowpkg/io.py:23"])])  # p bound twice: line 25 not a read
        self.assertEqual([(x["via"][len(F) + 1:], x["file"], x["evidence"]) for x in r["downstream"]],
                         [("copy_log", "data:out/copy.json", "flowpkg/io.py:19")])
        # `run / "log.jsonl"`: only the last part is known, so it stays a different file
        self.assertEqual(ev(ix.dataflow("data:log.jsonl")["readers"]), [("other_folder", ["flowpkg/io.py:29"])])

    def test_s10_3_cycle_and_depth_bound(self):
        ix = Index(cycle_graph())
        chain = lambda r: [(x["level"], x["via"], x["file"]) for x in r]
        self.assertEqual(chain(ix.dataflow("data:a", 99)["downstream"]),
                         [(1, "fn:f1", "data:b"), (2, "fn:f2", "data:c")])  # stops: f3 writes a, already seen
        self.assertEqual(chain(ix.dataflow("data:a", 99)["upstream"]),
                         [(1, "fn:f3", "data:c"), (2, "fn:f2", "data:b")])
        self.assertEqual(chain(ix.dataflow("data:a", 1)["downstream"]), [(1, "fn:f1", "data:b")])
        self.assertEqual(ix.dataflow("data:a", 0)["downstream"], [])
        self.assertEqual(Index.FLOW_MAX_DEPTH, 5)

    def test_s10_3b_long_chain_stops_at_the_bound(self):
        g = Graph()
        for i in range(8):
            g.add_node(f"data:d{i}", "data", f"d{i}")
        for i in range(7):  # g_i reads d_i and writes d_(i+1): a straight chain of 7 hops
            g.add_node(f"fn:g{i}", "function", f"g{i}")
            g.add_edge(f"fn:g{i}", f"data:d{i}", "reads", static="guess")
            g.add_edge(f"fn:g{i}", f"data:d{i + 1}", "writes", static="guess")
        down = Index(g.to_dict()).dataflow("data:d0", 99)["downstream"]
        self.assertEqual([(x["level"], x["file"]) for x in down], [(k, f"data:d{k}") for k in range(1, 6)])

    def test_s10_4_cli(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "g.json"
            p.write_text(json.dumps(self.data), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(HERE.parent))
            run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "minimap.query", str(p), "--flow", *a],
                                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
            txt = run("--node", "data:data/events.jsonl")
            self.assertEqual(txt.returncode, 0, txt.stderr)
            self.assertIn(f"writers (1)\n  {E}.save [guess] fixpkg/core.py:48", txt.stdout)
            self.assertIn(f"  L1 data:in.txt -> {W} reads it, writes data:out.json [guess] fixpkg/core.py:59",
                          run("--node", "data:in.txt").stdout)
            files = [r["file"] for r in json.loads(run("--json").stdout)]
            # every file with a writer or reader; .state.lock only has a path user, so it is left out
            self.assertEqual(sorted(files), ["data:data/events.jsonl", "data:in.txt", "data:out.json",
                                             "data:state/cache.json"])
            self.assertEqual(run("--node", "data:nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()

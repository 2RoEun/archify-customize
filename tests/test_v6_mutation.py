"""V6 Mutation tests on a copy of the example package (examples/vbank). The example folder is only read."""
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from minimap.extract import extract
from minimap.verify import Report, v5_1_fresh

SRC = Path(__file__).resolve().parents[1] / "examples" / "vbank" / "vbank"
PKG = "vbank"
TARGET = "vbank/accounts.py"


def keyset(g):
    return {k for k in g.edges}, set(g.nodes)


class V6(unittest.TestCase):
    def setUp(self):
        if not SRC.is_dir():
            self.skipTest(f"{SRC} not found")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(SRC, self.root / PKG, ignore=shutil.ignore_patterns("__pycache__"))
        self.base = extract(self.root, PKG, extra_dirs={})
        self.target = self.root / TARGET

    def tearDown(self):
        self.tmp.cleanup()

    def mutate(self, fn):
        text = self.target.read_text(encoding="utf-8")
        self.target.write_bytes(fn(text).encode("utf-8"))
        return extract(self.root, PKG, extra_dirs={})

    def test_v6_0_copy_matches_project(self):
        orig = extract(SRC.parent, PKG, extra_dirs={})
        self.assertEqual(keyset(orig), keyset(self.base))

    def test_v6_1_added_function_appears(self):
        n_lines = len(self.target.read_text(encoding="utf-8").splitlines())
        g = self.mutate(lambda t: t.rstrip("\n") + "\n\n\ndef _mm_probe():\n    return 1\n")
        node = g.nodes.get(f"fn:{PKG}.accounts._mm_probe")
        self.assertIsNotNone(node)
        self.assertEqual(node["line"], n_lines + 3)
        self.assertEqual(set(g.nodes) - set(self.base.nodes), {f"fn:{PKG}.accounts._mm_probe"})

    def test_v6_2_removed_call_disappears(self):
        lines = self.target.read_text(encoding="utf-8").splitlines()
        pick = None
        for (a, b, k), e in sorted(self.base.edges.items()):
            if k != "calls" or e["static"] != "confirmed" or len(e["evidence"]) != 1:
                continue
            if b.startswith("cls:"):  # a constructor call also sets variable types, so more edges would move
                continue
            rel, _, ln = e["evidence"][0].rpartition(":")
            name = self.base.nodes[b]["name"]
            if rel == TARGET and lines[int(ln) - 1].count(f"{name}(") == 1:
                pick = (a, b, k, int(ln), name)
                break
        self.assertIsNotNone(pick, "no suitable single-evidence call in accounts.py")
        a, b, k, ln, name = pick

        def cut(t):
            ls = t.splitlines(keepends=True)
            ls[ln - 1] = ls[ln - 1].replace(f"{name}(", f"{name}_mm_removed(")
            return "".join(ls)
        g = self.mutate(cut)
        self.assertNotIn((a, b, k), g.edges)
        self.assertEqual(set(self.base.edges) - set(g.edges), {(a, b, k)})

    def test_v6_3_added_import_appears(self):
        g = self.mutate(lambda t: t.rstrip("\n") + "\n\nfrom .reports import summary  # mm mutation\n")
        key = (f"mod:{PKG}.accounts", f"fn:{PKG}.reports.summary", "imports")
        self.assertIn(key, g.edges)
        self.assertEqual(set(g.edges) - set(self.base.edges), {key})

    def test_v6_4_edit_marks_stale(self):
        self.assertEqual(self.base.stale_files(self.root), [])
        self.target.write_bytes(self.target.read_bytes() + b"\n# touched\n")
        self.assertEqual(self.base.stale_files(self.root), [TARGET])

    def test_v6_5_added_and_broken_files_are_tracked(self):
        rel = f"{PKG}/_mm_half_saved.py"
        new = self.root / rel
        new.write_text("def half_saved(:\n", encoding="utf-8")
        self.assertEqual(self.base.stale_files(self.root), [rel])  # a file added after the build: stale
        g = extract(self.root, PKG, extra_dirs={})
        self.assertIn(rel, g.sources)  # hashed even though it does not parse
        self.assertEqual([e.split(":")[0] for e in g.stats["parse_errors"]], [rel])
        self.assertEqual(g.stale_files(self.root), [])
        rep = Report()
        v5_1_fresh(g, self.root, rep)
        self.assertEqual([r["step"] for r in rep.failed()], ["V5.1b"])  # verify refuses a map with parse errors
        new.write_text("def half_saved():\n    return 1\n", encoding="utf-8")
        self.assertEqual(g.stale_files(self.root), [rel])  # fixing it later is noticed
        new.unlink()
        self.assertEqual(g.stale_files(self.root), [rel])  # so is removing it


if __name__ == "__main__":
    unittest.main()

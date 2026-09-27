"""V7 Concurrency safety (DESIGN.md §9). Runs on a throwaway copy of the rt fixture, never on the real project map."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from minimap.build_html import embedded_graph, graph_from_html
from minimap.graph import Graph
from minimap.safeio import PAUSE, RETRIES, Busy, atomic_write_bytes, read_bytes_retry, run_lock

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(TOOL))
SPEC = {
    "schema_version": 1, "diagram_type": "architecture",
    "meta": {"title": "rt fixture", "output": "rt-minimap.html", "quality_profile": "showcase"},
    "components": [
        {"id": "suite", "type": "external", "label": "tests", "pos": [40, 150], "size": [140, 60]},
        {"id": "core", "type": "backend", "label": "rtpkg.mod", "pos": [320, 150], "size": [140, 60]}],
    "connections": [{"id": "suite-to-core", "from": "suite", "to": "core", "label": "calls"}],
}
CONCEPTS = {"schema": "archify-minimap-concepts/1", "concepts": {
    "suite": {"label": "tests", "targets": ["mod:tests"]},
    "core": {"label": "rtpkg.mod", "targets": ["mod:rtpkg.mod"]}}}


def make_project(root: Path) -> Path:
    shutil.copytree(HERE / "fixtures" / "rt", root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    d = root / "docs" / "diagrams"
    d.mkdir(parents=True)
    (d / "rt.architecture.json").write_text(json.dumps(SPEC), encoding="utf-8")
    (d / "rt.concepts.json").write_text(json.dumps(CONCEPTS), encoding="utf-8")
    return d


def run_py(root, *extra, wait=True, cwd=None):
    cmd = [sys.executable, "-B", str(TOOL / "run.py"), "--root", str(root), "--package", "rtpkg", "--name", "rt",
           "--expect-run", "1", "--expect-skip", "0", *extra]
    if wait:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=300, cwd=cwd)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)


def lock_path_for(gfile):
    gfile = Path(gfile)
    return gfile.with_name(f".{gfile.name}.lock")  # next to the output (run.py lock_path_for)


def reap(*procs):
    """Kill any child still running (a failed assert must not leave a build or query behind)."""
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=30)
        for s in (p.stdin, p.stdout, p.stderr):
            if s:
                s.close()


def procs_with(marker: str) -> list:
    """Process listing (CIM): (pid, command line) of every process whose command line contains marker."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120).stdout
    rows = (line.partition("\t") for line in out.splitlines())
    return [(int(pid), cmd) for pid, _, cmd in rows if pid.strip().isdigit() and marker.lower() in cmd.lower()]


def hold_like_a_replace(path):
    """Windows: open path with DELETE access, as os.replace does for an instant. Until the returned close() runs,
    every open that does not share delete (any plain Python open) fails with a sharing violation."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.CreateFileW(str(path), 0x00010000, 0x7, None, 3, 0, None)  # DELETE, share all, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"cannot open {path} with DELETE access")
    box = [h]

    def close():
        if box:
            k32.CloseHandle(box.pop())
    return close


def load_run_module():
    spec = importlib.util.spec_from_file_location("mm_run_under_test", TOOL / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class V7(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = tempfile.TemporaryDirectory()  # run records go here, not into the tool folder
        ENV["MINIMAP_STATE_DIR"] = cls.state.name
        cls.root = Path(cls.tmp.name)
        d = make_project(cls.root)
        cls.gfile, cls.hfile, cls.diagrams = d / "rt.minimap.json", d / "rt-minimap.html", d
        first = run_py(cls.root)
        if first.returncode != 0:
            raise AssertionError(f"initial build failed ({first.returncode}):\n{first.stdout[-3000:]}\n{first.stderr[-2000:]}")

    @classmethod
    def tearDownClass(cls):
        ENV.pop("MINIMAP_STATE_DIR", None)
        cls.tmp.cleanup()
        cls.state.cleanup()

    def assert_outputs_whole(self):
        g = Graph.load(self.gfile)
        self.assertGreater(len(g.nodes), 5)
        self.assertEqual(embedded_graph(self.hfile), json.loads(self.gfile.read_text(encoding="utf-8")))

    def read_pair(self):
        """Graph and HTML as a reader sees them; the HTML must be whole, not just its JSON block."""
        g = json.loads(read_bytes_retry(self.gfile).decode("utf-8"))
        raw = read_bytes_retry(self.hfile)
        if not raw.rstrip().endswith(b"</html>") or raw.count(b'id="mm-panel-js"') != 1:
            raise ValueError(f"HTML is not whole ({len(raw)} bytes)")
        return g, graph_from_html(raw.decode("utf-8"))

    # V7.1a — the reader loop can tell a naive write from an atomic one (proves the test is sensitive)
    def test_v7_1a_atomic_write_never_torn_naive_write_is(self):
        big = [json.dumps({"n": i, "pad": "x" * 4_000_000}).encode() for i in range(2)]

        def hammer(path, writer, stop, box):
            try:
                while not stop.is_set():
                    writer(path, big[box["writes"] % 2])
                    box["writes"] += 1
            except Exception as exc:  # noqa: BLE001 - reported by the asserts below
                box["exc"] = exc

        results = {}
        for label, writer in (("naive", lambda p, b: p.write_bytes(b)), ("atomic", atomic_write_bytes)):
            p = Path(self.tmp.name) / f"{label}.json"
            p.write_bytes(big[0])
            stop, box = threading.Event(), {"writes": 0, "exc": None}
            t = threading.Thread(target=hammer, args=(p, writer, stop, box))
            t.start()
            reads = torn = 0
            seen = set()
            start = time.time()
            try:
                # 4 s; the atomic arm may run on (up to 30 s) until it has 5 writes and has seen both payloads
                while time.time() < start + 4 or (label == "atomic" and time.time() < start + 30 and box["exc"] is None
                                                  and (box["writes"] < 5 or len(seen) < 2)):
                    try:
                        seen.add(json.loads(read_bytes_retry(p))["n"])
                    except (ValueError, KeyError, TypeError, PermissionError, FileNotFoundError):
                        torn += 1
                    reads += 1
            finally:
                stop.set()
                t.join()
            results[label] = {"reads": reads, "torn": torn, "writes": box["writes"], "exc": repr(box["exc"]),
                              "seen": sorted(seen)}
        naive, atomic = results["naive"], results["atomic"]
        self.assertGreater(naive["torn"], 0, f"reader never saw a torn naive write: {results}")
        self.assertGreater(naive["writes"], 0, results)
        self.assertEqual(atomic["torn"], 0, f"torn reads with atomic write: {results}")
        self.assertEqual(atomic["exc"], "None", f"atomic writer failed: {results}")
        self.assertGreaterEqual(atomic["writes"], 5, results)
        self.assertEqual(atomic["seen"], [0, 1], f"reader did not see both payloads: {results}")
        self.assertGreater(atomic["reads"], 20, results)

    # V7.1b — readers during a real rebuild always get whole files, and graph and HTML come back in step. They may
    # differ only while publish() is between its two renames, and that window lasts as long as the HTML rename
    # retries (RETRIES x PAUSE, about 5 s) when something (a browser, a scanner, this very loop) holds the HTML open.
    # So a mismatch is re-read until the pair is in step again, and it fails only if it is still out of step after
    # that retry budget plus a margin. Both outputs must also be new files (new file IDs): an in-place write keeps the
    # ID, a rename of a temp file does not, so an in-place publish is caught every time, not only when a read happens
    # to land inside the ~1 ms write.
    def test_v7_1b_readers_during_rebuild(self):
        bound = RETRIES * PAUSE + 3.0  # the HTML rename's retry budget, plus a margin for the attempts and our reads
        ids_before = [os.stat(p).st_ino for p in (self.gfile, self.hfile)]
        self.assertNotIn(0, ids_before, "this file system reports no file IDs")
        proc = run_py(self.root, "--hold", "1", wait=False)
        torn, lasting, gaps, reads, gens = [], [], [], 0, set()
        try:
            while proc.poll() is None:
                reads += 1
                try:
                    g, h = self.read_pair()
                except Exception as exc:  # noqa: BLE001 - any unreadable file is a failure
                    torn.append(repr(exc)[:200])
                    continue
                gens.add(g["stats"].get("generated"))
                if g == h:
                    continue
                t0 = time.time()
                while True:  # out of step: re-read until in step again, or until the bound has passed
                    time.sleep(0.03)
                    try:
                        g2, h2 = self.read_pair()
                    except Exception as exc:  # noqa: BLE001
                        torn.append(repr(exc)[:200])
                        break
                    if g2 == h2:
                        gaps.append(round(time.time() - t0, 3))
                        break
                    if time.time() - t0 > bound:
                        lasting.append(f"read {reads}: {g['stats'].get('generated')} vs "
                                       f"{h['stats'].get('generated')}, still out of step after {bound:.1f} s")
                        break
            out, err = proc.communicate(timeout=300)
        finally:
            reap(proc)
        self.assertEqual(proc.returncode, 0, out[-2000:] + err[-1000:])
        self.assertEqual(torn, [], f"{len(torn)} unreadable of {reads}")
        self.assertEqual(lasting, [], f"graph/HTML never came back in step: {lasting[:3]} (in-step again after {gaps} s)")
        self.assertGreaterEqual(len(gens), 2, f"reads never spanned the publish: {sorted(gens)} in {reads} reads")
        ids_after = [os.stat(p).st_ino for p in (self.gfile, self.hfile)]
        for name, before, after in zip(("graph", "HTML"), ids_before, ids_after):
            self.assertNotEqual(before, after, f"{name} was rewritten in place (same file ID), not replaced")

    # V7.2 — a second build while one runs is refused cleanly, whatever the path spelling; the first finishes whole
    def test_v7_2_second_build_is_refused(self):
        a = run_py(self.root, "--hold", "4", wait=False)
        try:
            lock = lock_path_for(self.gfile)
            deadline = time.time() + 60
            held = False
            while time.time() < deadline and a.poll() is None:
                try:
                    with run_lock(lock):
                        pass
                except Busy:
                    held = True
                    break
                time.sleep(0.05)
            self.assertTrue(held, "first build never took the lock")
            b = run_py(self.root)
            self.assertEqual(b.returncode, 4, b.stdout[-1500:])
            self.assertIn("busy", b.stdout)
            if os.name == "nt":  # another spelling of the same folder reaches the same lock file
                c = run_py("\\\\?\\" + str(self.root.resolve()))
                self.assertEqual(c.returncode, 4, c.stdout[-1500:])
            out, err = a.communicate(timeout=300)
        finally:
            reap(a)
        self.assertEqual(a.returncode, 0, out[-2000:] + err[-1000:])
        self.assert_outputs_whole()

    # V7.3 — queries during graph swaps: every answer is one of the two graphs' answers, never an error
    def test_v7_3_queries_during_graph_swaps(self):
        base = json.loads(self.gfile.read_text(encoding="utf-8"))
        alt = dict(base, edges=[e for e in base["edges"] if e["from"] != "fn:tests.test_rt.RT.test_all"])
        pad = "x" * 1_000_000  # longer reads, so readers and the writer really collide
        blobs = [json.dumps(dict(d, stats=dict(d["stats"], pad=pad))).encode("utf-8") for d in (base, alt)]
        swap = Path(self.tmp.name) / "swap.minimap.json"
        cmd = [sys.executable, "-B", str(TOOL / "mm.py"), "--graph", str(swap), "--node", "concept:core",
               "--dir", "users", "--rollup", "--depth", "3", "--json"]
        answers = []
        for blob in blobs:
            swap.write_bytes(blob)
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            answers.append(r.stdout)
        self.assertIn("fn:tests.test_rt.RT.test_all", answers[0])
        self.assertNotEqual(answers[0], answers[1])

        stop, box = threading.Event(), {"writes": 0, "exc": None}

        def swapper():
            try:
                while not stop.is_set():
                    atomic_write_bytes(swap, blobs[box["writes"] % 2])
                    box["writes"] += 1
                    time.sleep(0.01)
            except Exception as exc:  # noqa: BLE001 - reported by the asserts below
                box["exc"] = exc

        t = threading.Thread(target=swapper)
        t.start()
        results, procs = [], []
        try:
            start, rounds = time.time(), 0
            while rounds < 2 or time.time() < start + 3:
                procs = []
                for _ in range(8):
                    procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                                  encoding="utf-8", env=ENV))
                for p in procs:
                    out, err = p.communicate(timeout=120)
                    results.append((p.returncode, out, err))
                rounds += 1
        finally:
            stop.set()
            t.join()
            reap(*procs)
        bad = [(code, out[:80], err[-300:]) for code, out, err in results if code != 0 or out not in answers]
        self.assertEqual(bad, [], f"{len(bad)} bad of {len(results)} queries")
        self.assertIsNone(box["exc"], f"writer failed: {box['exc']!r}")
        self.assertGreaterEqual(box["writes"], 2, "writer made no progress")
        if os.name != "nt":
            return
        # Deterministic arm: the graph is held the way a replace holds it (DELETE access) for 2.5 s while a query
        # starts. A query that retries waits and then answers; one that reads once fails at once (sharing violation).
        atomic_write_bytes(swap, blobs[0])
        close = hold_like_a_replace(swap)
        q = None
        try:
            q = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                 env=ENV)
            deadline = time.time() + 2.5
            while time.time() < deadline and q.poll() is None:
                time.sleep(0.05)
            ended_while_held = q.poll() is not None
            close()
            out, err = q.communicate(timeout=120)
        finally:
            close()
            if q:
                reap(q)
        self.assertFalse(ended_while_held, f"query ended while the graph was held (exit {q.returncode}): {err[-300:]}")
        self.assertEqual((q.returncode, out), (0, answers[0]), err[-300:])

    # V7.4 — killing the lock holder releases the lock
    def test_v7_4_killed_holder_releases_lock(self):
        lock = Path(self.tmp.name) / "probe.lock"
        code = ("import sys,time; sys.path.insert(0, sys.argv[2]); from minimap.safeio import run_lock; "
                "cm = run_lock(sys.argv[1]); cm.__enter__(); print('READY', flush=True); time.sleep(60)")
        p = subprocess.Popen([sys.executable, "-c", code, str(lock), str(TOOL)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "READY")
            with self.assertRaises(Busy):
                with run_lock(lock):
                    pass
        finally:
            reap(p)
        with run_lock(lock):
            pass  # acquired after the holder died

    # V7.5 — no temp files left behind next to the outputs (the lock file is the only allowed dotfile)
    def test_v7_5_no_leftover_temp_files(self):
        lock = lock_path_for(self.gfile).name
        left = [p.name for p in self.diagrams.iterdir() if p.name.startswith(".") and p.name != lock]
        self.assertEqual(left, [])

    # V7.6a — a publish that cannot finish rolls back: exit 5, previous graph restored, pair still in step
    def test_v7_6a_failed_publish_rolls_back(self):
        before = self.gfile.read_bytes()
        with open(self.hfile, "rb"):  # a reader holding the HTML longer than the 5 s replace budget
            r = run_py(self.root)
        self.assertEqual(r.returncode, 5, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn("publish failed", r.stdout)
        self.assertEqual(self.gfile.read_bytes(), before, "previous graph not restored")
        self.assert_outputs_whole()

    # V7.6c — an unexpected error before publish (here: a concepts file that is not JSON) exits 3, not 1, and
    # publishes nothing
    def test_v7_6c_build_error_exits_3(self):
        cfile = self.diagrams / "rt.concepts.json"
        before, good = (self.gfile.read_bytes(), self.hfile.read_bytes()), cfile.read_bytes()
        cfile.write_text("{not json", encoding="utf-8")
        try:
            r = run_py(self.root, "--no-trace")
        finally:
            cfile.write_bytes(good)
        self.assertEqual(r.returncode, 3, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn("build failed", r.stdout)
        self.assertIn("JSONDecodeError", r.stderr)
        self.assertEqual((self.gfile.read_bytes(), self.hfile.read_bytes()), before, "outputs changed")

    # V7.6b — recovery helpers: leftover temps are swept (never the lock or another map's files), an out-of-step
    # pair is reported, and only the newest run folders are kept
    def test_v7_6b_sweep_warn_prune(self):
        run_mod = load_run_module()
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            g, h = d / "rt.minimap.json", d / "rt-minimap.html"
            shutil.copy(self.gfile, g)
            shutil.copy(self.hfile, h)
            leftovers = [".rt.minimap.json.1.aa.tmp", "..rt.minimap.json.1.aa.tmp.1.bb.tmp",
                         ".rt-minimap.html.1.aa.html.tmp", "..rt-minimap.html.1.aa.html.1.bb.archify.html"]
            keep = [".rt.minimap.json.lock", ".xrt.minimap.json.1.aa.tmp", ".other.tmp", ".rt.minimap.json.bak"]
            for n in leftovers + keep:
                (d / n).write_bytes(b"x")
            buf = io.StringIO()
            with redirect_stdout(buf):
                run_mod.sweep_temps(g, h)
                run_mod.warn_if_out_of_step(g, h)
            self.assertEqual(sorted(p.name for p in d.iterdir()), sorted(keep + [g.name, h.name]))
            self.assertNotIn("out of step", buf.getvalue())
            data = json.loads(g.read_text(encoding="utf-8"))
            data["stats"]["generated"] = "an-older-build"
            g.write_text(json.dumps(data), encoding="utf-8")
            with redirect_stdout(buf):
                run_mod.warn_if_out_of_step(g, h)
            self.assertIn("out of step", buf.getvalue())
            runs = d / "runs"
            names = [f"20260101-0000{i:02d}-{100 + i}" for i in range(25)]
            for n in names:
                (runs / n).mkdir(parents=True)
            run_mod.prune_runs(runs)
            self.assertEqual(sorted(p.name for p in runs.iterdir()), names[-run_mod.KEEP_RUNS:])

    # V7.7 — killing a build also kills its children (tracer, archify): nothing keeps working after the lock is free
    @unittest.skipUnless(os.name == "nt", "job objects are Windows-only")
    def test_v7_7_killed_build_kills_its_children(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            root = Path(t) / "slowproj"
            make_project(root)
            (root / "tests" / "test_slow.py").write_text(
                "import time\nimport unittest\n\n\nclass Slow(unittest.TestCase):\n"
                "    def test_slow(self):\n        time.sleep(120)\n", encoding="utf-8")
            proc = run_py(root, wait=False)
            orphans = []
            try:
                tracer, deadline = None, time.time() + 90
                while tracer is None and time.time() < deadline and proc.poll() is None:
                    tracer = next((pid for pid, cmd in procs_with(str(root)) if "minimap.trace" in cmd), None)
                self.assertIsNotNone(tracer, f"tracer child never seen (build exit {proc.poll()})")
                proc.kill()
                proc.wait(timeout=30)
                deadline = time.time() + 15
                while True:
                    orphans = procs_with(str(root))
                    if not orphans or time.time() > deadline:
                        break
                    time.sleep(0.5)
            finally:
                reap(proc)
                for pid, _ in orphans:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
            self.assertEqual(orphans, [], "children of the killed build are still running")
            with run_lock(lock_path_for(root / "docs" / "diagrams" / "rt.minimap.json")):
                pass  # and the lock is free

    # V7.8 — mm.py --stale with no ROOT checks the graph's own root and sees added files; a folder fails fast
    def test_v7_8_stale_default_root_and_fail_fast(self):
        cmd = [sys.executable, "-B", str(TOOL / "mm.py"), "--graph", str(self.gfile), "--stale"]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120)
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"stale": False, "files": []}), r.stderr[-500:])
        new = self.root / "rtpkg" / "newmod.py"
        new.write_text("def brand_new():\n    return 1\n", encoding="utf-8")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120)
        finally:
            new.unlink()
        self.assertEqual((r.returncode, json.loads(r.stdout)), (1, {"stale": True, "files": ["rtpkg/newmod.py"]}))
        t0 = time.time()
        with self.assertRaises(OSError):
            read_bytes_retry(self.diagrams)
        self.assertLess(time.time() - t0, 1.0, "a folder path was retried like a busy file")

    # V7.9 — a relative --root builds and publishes (the tracer, verify and archify run in other working folders),
    # and the graph records the absolute root, so `mm.py --stale` without ROOT works from any folder
    def test_v7_9_relative_root(self):
        r = run_py(self.root.name, cwd=str(self.root.parent))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn("published", r.stdout)
        self.assert_outputs_whole()
        recorded = json.loads(self.gfile.read_text(encoding="utf-8"))["root"]
        self.assertTrue(Path(recorded).is_absolute(), recorded)
        s = subprocess.run([sys.executable, "-B", str(TOOL / "mm.py"), "--graph", str(self.gfile), "--stale"],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120, cwd=str(HERE))
        self.assertEqual((s.returncode, json.loads(s.stdout)), (0, {"stale": False, "files": []}), s.stderr[-500:])


if __name__ == "__main__":
    unittest.main()

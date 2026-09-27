"""One command: extract -> trace the tests -> merge -> boxes -> build temp outputs -> verify them -> publish.

python run.py --root PROJECT --package PACKAGE --name NAME [--expect-run N --expect-skip N]
It reads PROJECT/docs/diagrams/NAME.architecture.json (the overview picture) and NAME.concepts.json (which code
belongs to which box), and writes NAME.minimap.json (the graph) and NAME-minimap.html (the viewer) next to them.
Only one build per map runs at a time (an OS lock next to the output); a second build exits with code 4 ("busy").
The graph and the HTML are built as temp files and verified; only a passing pair is published. Run records go to
<state>/runs/<stamp>-<pid>/ (<state> = MINIMAP_STATE_DIR or this folder; the newest 20 are kept). On Windows the
build and its children share a job object with a memory cap (MINIMAP_MEMORY_MB, default 8192), so a runaway build
fails cleanly and killing the build kills its children.
Exit codes: 0 published, 1 verify did not pass, 2 the test run failed, 3 build failed before verify, 4 busy,
5 publish failed (previous outputs kept).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("MINIMAP_STATE_DIR") or HERE).absolute()  # children run in other working folders
KEEP_RUNS = 20
MEMORY_MB = int(os.environ.get("MINIMAP_MEMORY_MB") or 8192)  # cap for the whole build tree (Windows job object)
PREV = STATE / "prev"  # STEP 11: the graph that the last publish replaced, for `mm.py --diff`


def job_peak_mb() -> int | None:  # replaced by kill_children_with_build() when the job exists
    return None
sys.path.insert(0, str(HERE))

from minimap import concepts as concepts_mod  # noqa: E402
from minimap.build_html import build, embedded_graph  # noqa: E402
from minimap.extract import extract  # noqa: E402
from minimap.safeio import (Busy, atomic_write_bytes, read_bytes_retry, replace_retry, run_lock,  # noqa: E402
                            temp_sibling)
from minimap.query import Index, diff_graphs, diff_summary  # noqa: E402
from minimap.trace import merge  # noqa: E402

_JOB = None


def lock_path_for(gfile: Path) -> Path:
    return gfile.with_name(f".{gfile.name}.lock")


def kill_children_with_build() -> None:
    """Windows: put this process in a job object with KILL_ON_JOB_CLOSE. Children inherit the job, and the OS closes
    the only job handle when this process ends (even when killed), which kills every child still running.
    The job also caps the memory of the whole build (MEMORY_MB): a runaway fails with MemoryError instead of
    freezing the PC (2026-09-26: a runaway test process once took ~150 GB and froze a whole machine)."""
    global _JOB
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
    info.JobMemoryLimit = MEMORY_MB * 1024 * 1024
    ok = bool(job) and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
        and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())  # 9 = JobObjectExtendedLimitInformation
    if not ok:
        print(f"warning: no child-process job (WinError {ctypes.get_last_error()}); "
              "a killed build may leave its children running, and memory is not capped")
        return
    _JOB = job  # never closed here: the OS closes it when this process ends

    k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p)

    def peak_mb() -> int:
        q = Extended()
        k32.QueryInformationJobObject(job, 9, ctypes.byref(q), ctypes.sizeof(q), None)
        return q.PeakJobMemoryUsed // (1024 * 1024)
    globals()["job_peak_mb"] = peak_mb


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    kill_children_with_build()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="your project folder (it contains the package and tests/)")
    ap.add_argument("--package", required=True, help="the package folder name inside --root, e.g. vbank")
    ap.add_argument("--name", required=True, help="map name: uses docs/diagrams/<name>.architecture.json and <name>.concepts.json")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--expect-run", type=int, default=-1, help="tests your suite runs; -1 = accept any count (failures must still be 0)")
    ap.add_argument("--expect-skip", type=int, default=-1, help="tests your suite skips; -1 = accept any count")
    ap.add_argument("--hold", type=float, default=0.0, help="test aid: keep the lock this many seconds after the build")
    a = ap.parse_args(argv)
    # The tracer and verify run with cwd=HERE and archify with cwd=its own home, so a relative root must be made
    # absolute here; it is also the root recorded in the graph, which `--stale` uses by default.
    a.root = str(Path(a.root).absolute())
    gfile = Path(a.root) / "docs" / "diagrams" / f"{a.name}.minimap.json"
    if not gfile.with_name(f"{a.name}.architecture.json").is_file():
        print(f"no overview spec: {gfile.with_name(f'{a.name}.architecture.json')} (create it and <name>.concepts.json; see README \"Use it on your own project\" and examples/vbank)")
        return 3
    try:
        with run_lock(lock_path_for(gfile)):
            try:
                code = pipeline(a)
            except Exception:  # noqa: BLE001 - publish() catches its own errors, so this is always before publish
                traceback.print_exc()
                print("build failed: unexpected error before publish; nothing published, the previous outputs stay live")
                code = 3
            if a.hold:
                time.sleep(a.hold)
            peak = job_peak_mb()
            print(f"peak memory of the build: {peak if peak is not None else '?'} MB (cap {MEMORY_MB} MB)")
            return code
    except Busy as exc:
        print(f"busy: {exc}")
        return 4


def sweep_temps(gfile: Path, hfile: Path) -> None:
    """Delete temp files a killed build left next to the outputs. Runs under the lock, so none of them is live."""
    lock = lock_path_for(gfile).name
    for p in gfile.parent.iterdir():
        n = p.name
        if n == lock or not n.startswith(".") or not n.endswith((".tmp", ".archify.html")):
            continue
        if n.lstrip(".").startswith((gfile.name + ".", hfile.name + ".")) and p.is_file():
            try:
                p.unlink()
                print(f"    removed leftover {n}")
            except OSError:
                pass


def warn_if_out_of_step(gfile: Path, hfile: Path) -> None:
    """A publish cut short by a kill or power loss can leave a new graph next to an old HTML; say so."""
    if not (gfile.is_file() and hfile.is_file()):
        return
    try:
        g_gen = json.loads(read_bytes_retry(gfile).decode("utf-8"))["stats"].get("generated")
        h_gen = embedded_graph(hfile)["stats"].get("generated")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"warning: could not compare the live graph and HTML: {exc!r}")
        return
    if g_gen != h_gen:
        print(f"warning: live graph (generated {g_gen}) and HTML (generated {h_gen}) are out of step; "
              "this build republishes both")


def prune_runs(runs: Path, keep: int = KEEP_RUNS) -> None:
    for p in sorted(p for p in runs.iterdir() if p.is_dir())[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


def publish(gtmp: Path, gfile: Path, htmp: Path, hfile: Path) -> int:
    """Two renames, all or nothing: if the HTML rename fails, the previous graph is put back (or removed).
    Every error in here returns 5, so an exception that reaches main() always happened before publish (exit 3)."""
    try:
        old = read_bytes_retry(gfile) if gfile.exists() else None
        replace_retry(gtmp, gfile)
    except Exception as exc:  # noqa: BLE001
        print(f"publish failed: {exc} (previous outputs kept)")
        return 5
    try:
        replace_retry(htmp, hfile)
    except Exception as exc:  # noqa: BLE001
        try:
            if old is None:
                gfile.unlink()
            else:
                atomic_write_bytes(gfile, old)
            print(f"publish failed: {exc} (previous graph restored)")
        except Exception as exc2:  # noqa: BLE001
            print(f"publish failed: {exc}; restoring the previous graph also failed: {exc2}")
        return 5
    print(f"    published {gfile.name} and {hfile.name}")
    if old is not None:  # STEP 11: keep the replaced graph for `mm.py --diff`; a failure here does not undo publish
        try:
            PREV.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(PREV / gfile.name, old)
            prev_data = json.loads(old.decode("utf-8"))
            d = diff_graphs(prev_data, json.loads(read_bytes_retry(gfile).decode("utf-8")))
            print(f"    changes vs previous ({prev_data['stats'].get('generated')}): {diff_summary(d)}")
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not keep the previous graph for --diff: {exc!r}")
    return 0


def pipeline(a) -> int:
    root = Path(a.root)
    diagrams = root / "docs" / "diagrams"
    spec, cfile = diagrams / f"{a.name}.architecture.json", diagrams / f"{a.name}.concepts.json"
    gfile, hfile = diagrams / f"{a.name}.minimap.json", diagrams / f"{a.name}-minimap.html"
    sweep_temps(gfile, hfile)
    warn_if_out_of_step(gfile, hfile)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rundir = STATE / "runs" / f"{stamp}-{os.getpid()}"
    rundir.mkdir(parents=True)
    prune_runs(rundir.parent)
    env = dict(os.environ, PYTHONPATH=str(HERE), PYTHONIOENCODING="utf-8")

    print("[1] extract")
    g = extract(root, a.package)
    g.save(rundir / "static.json")
    observed = {"suite": {"run": 0, "failures": 0, "errors": 0, "skipped": 0, "seconds": 0, "problems": []}, "pairs": []}
    if not a.no_trace:
        print("[2] trace test suite (in-process, profiled)")
        p = subprocess.run([sys.executable, "-B", "-m", "minimap.trace", "--root", str(root), "--graph",
                            str(rundir / "static.json"), "--out", str(rundir / "observed.json")],
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(HERE))
        print("    ", p.stdout.strip() or p.stderr.strip()[-800:])
        if p.returncode:
            return 2
        observed = json.loads((rundir / "observed.json").read_text(encoding="utf-8"))
    else:
        (rundir / "observed.json").write_text(json.dumps(observed), encoding="utf-8")
    print("[3] merge runtime", merge(g, observed))
    print("[4] concepts")
    problems = concepts_mod.apply(g, cfile, root)
    (rundir / "problems.json").write_text(json.dumps(problems, ensure_ascii=False), encoding="utf-8")
    g.stats["generated"] = f"{stamp}-{os.getpid()}"
    g.stats["suite"] = observed["suite"]
    print("[4b] overlay data (STEP 12)")
    try:
        prev = json.loads(read_bytes_retry(gfile).decode("utf-8")) if gfile.exists() else None
    except (OSError, ValueError) as exc:
        print(f"    warning: previous graph unreadable, no change overlay: {exc!r}")
        prev = None
    connections = json.loads(spec.read_text(encoding="utf-8")).get("connections", [])
    g.stats["overlays"] = Index(g.to_dict()).overlays(connections, prev)
    # Build both outputs as temp files, verify the temp pair, and publish only a passing pair (DESIGN.md §9).
    gtmp, htmp = temp_sibling(gfile), temp_sibling(hfile, "html.tmp")
    try:
        data = g.to_bytes()
        with open(gtmp, "wb") as f:  # a plain write: the temp file needs no temp file of its own
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        print(f"    graph {len(g.nodes)} nodes, {len(g.edges)} edges sha256 {hashlib.sha256(data).hexdigest()[:16]}")
        print("[5] build html")
        res = build(spec, gtmp, htmp)
        print("    ", json.dumps(res, ensure_ascii=False)[:400])
        if not res.get("ok"):
            return 3
        print("[6] verify the unpublished pair")
        v = subprocess.run([sys.executable, "-B", "-m", "minimap.verify", "--root", str(root), "--graph", str(gtmp),
                            "--html", str(htmp), "--spec", str(spec), "--observed", str(rundir / "observed.json"),
                            "--problems", str(rundir / "problems.json"), "--json-out", str(rundir / "verify.json"),
                            "--expect-run", str(a.expect_run), "--expect-skip", str(a.expect_skip)],
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(HERE))
        print(v.stdout)
        if v.stderr.strip():
            print(v.stderr[-1500:])
        print(f"run record: {rundir}")
        if v.returncode:
            print("not published: verify failed, the previous outputs stay live")
            return 1
        print("[7] publish")
        return publish(gtmp, gfile, htmp, hfile)
    finally:
        for t in (gtmp, htmp):
            try:
                if t.exists():
                    t.unlink()
            except OSError as exc:  # must not replace the exit code; the next build sweeps it
                print(f"warning: could not remove {t.name}: {exc}")


if __name__ == "__main__":
    sys.exit(main())

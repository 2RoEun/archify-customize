"""Step-by-step verification of a built map on the real project (DESIGN.md §7 V5).

Each step returns checks (PASS/FAIL + evidence). A FAIL stops the run unless --keep-going.
python -m minimap.verify --root PROJECT --graph G.json --html H.html --spec S.json --concepts C.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from .build_html import embedded_graph
from .graph import Graph
from .query import Index

DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
CLASS_RE = re.compile(r"^\s*class\s+(\w+)")


class Report:
    def __init__(self):
        self.rows = []

    def check(self, step, name, ok, detail=""):
        self.rows.append({"step": step, "check": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        return ok

    def failed(self, step=None):
        return [r for r in self.rows if r["status"] == "FAIL" and (step is None or r["step"] == step)]


def _code_nodes(g: Graph, kinds=("function", "class")):
    return [n for n in g.nodes.values() if n["kind"] in kinds and n.get("file")]


def v5_1_fresh(g, root, rep):
    stale = g.stale_files(root)
    rep.check("V5.1", f"recorded hashes match {len(g.sources)} files on disk, none added or removed", not stale,
              f"stale: {stale[:10]}")
    errs = g.stats.get("parse_errors", [])
    rep.check("V5.1b", "every discovered file parsed (stats.parse_errors empty)", not errs, str(errs[:10]))


def v5_2_existence(g, root, rep):
    by_file = {}
    for n in _code_nodes(g):
        by_file.setdefault(n["file"], []).append(n)
    missing = []
    for rel, nodes in by_file.items():
        tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8-sig"))
        found = {(x.name, x.lineno) for x in ast.walk(tree)
                 if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        missing += [f"{n['id']}@{rel}:{n['line']}" for n in nodes if (n["name"], n["line"]) not in found]
    total = sum(len(v) for v in by_file.values())
    rep.check("V5.2", f"every class/function node re-found at file:line ({total} nodes)", not missing, str(missing[:10]))


def v5_3_completeness(g, root, rep):
    """Independent line scan (regex, not ast) of def/class per file vs graph node counts."""
    counts = {}
    for n in _code_nodes(g):
        counts.setdefault(n["file"], [0, 0])[0 if n["kind"] == "function" else 1] += 1
    bad = []
    for rel in g.sources:
        text = (Path(root) / rel).read_text(encoding="utf-8-sig").splitlines()
        defs, classes = _scan_defs(text)
        got = counts.get(rel, [0, 0])
        if [defs, classes] != got:
            bad.append(f"{rel}: scan def={defs} class={classes} vs graph {got}")
    rep.check("V5.3a", f"def/class counts agree for {len(g.sources)} files (regex scan vs graph)", not bad, str(bad[:10]))
    # every import line that names an analysed module has an imports edge at that line
    ev = {x for e in g.edges.values() if e["kind"] == "imports" for x in e["evidence"]}
    pkg = g.package
    miss = []
    for rel in g.sources:
        for i, line in enumerate((Path(root) / rel).read_text(encoding="utf-8-sig").splitlines(), 1):
            s = line.strip()
            if re.match(rf"^(from\s+(\.|{pkg}\b)|import\s+{pkg}\b)", s) and f"{rel}:{i}" not in ev \
                    and not _in_string_block(rel, i, root):
                miss.append(f"{rel}:{i}: {s[:60]}")
    rep.check("V5.3b", "every import of an analysed module has an imports edge", not miss, str(miss[:10]))


_STRING_CACHE = {}


def _in_string_block(rel, line, root):
    """True if the line is inside a multi-line string literal (e.g. child-process code in a test)."""
    if rel not in _STRING_CACHE:
        tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8-sig"))
        spans = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.end_lineno and n.end_lineno > n.lineno]
        _STRING_CACHE[rel] = spans
    return any(a < line <= b or a <= line < b for a, b in _STRING_CACHE[rel])


def _scan_defs(lines):
    """Count def/class statements by regex, skipping lines inside triple-quoted strings."""
    defs = classes = 0
    in_str = None
    for line in lines:
        s = line
        if in_str:
            if in_str in s:
                in_str = None
            continue
        for q in ('"""', "'''"):
            if s.count(q) % 2 == 1 and not s.lstrip().startswith("#"):
                in_str = q
                break
        if in_str and not (DEF_RE.match(s) or CLASS_RE.match(s)):
            continue
        if DEF_RE.match(s):
            defs += 1
        elif CLASS_RE.match(s):
            classes += 1
    return defs, classes


def v5_4_evidence(g, root, rep):
    cache = {}
    bad = []
    checked = 0
    for e in g.edges.values():
        if e["kind"] not in ("calls", "imports") or e.get("static") != "confirmed":
            continue
        target = g.nodes.get(e["to"], {})
        name = target.get("name", e["to"].rsplit(".", 1)[-1])
        for ev in e["evidence"]:
            rel, _, line = ev.rpartition(":")
            if rel not in g.sources:
                continue  # runtime evidence from files outside the analysed set
            lines = cache.setdefault(rel, (Path(root) / rel).read_text(encoding="utf-8-sig").splitlines())
            text = lines[int(line) - 1] if 0 < int(line) <= len(lines) else ""
            ok = ("import" in text) if e["kind"] == "imports" else (name in text)
            if e["kind"] == "calls" and not ok and target.get("kind") == "class":
                ok = name in text
            checked += 1
            if not ok:
                bad.append(f"{e['from']} -> {e['to']} @ {ev}: {text.strip()[:60]}")
    rep.check("V5.4", f"static edge evidence lines name their target ({checked} lines)", not bad, str(bad[:10]))


def v5_5_runtime(observed, rep, expect_run, expect_skip):
    s = observed["suite"]
    # -1 = accept any count (a first build of a new project); failures and errors must be 0 either way
    ok = ((expect_run < 0 or s["run"] == expect_run) and (expect_skip < 0 or s["skipped"] == expect_skip)
          and s["failures"] == 0 and s["errors"] == 0)
    rep.check("V5.5", f"traced suite: run={s['run']} skipped={s['skipped']} failures={s['failures']} "
                      f"errors={s['errors']} ({s['seconds']} s)", ok, str(s.get("problems")))


def v5_6_concepts(g, problems, rep):
    rep.check("V5.6a", "every concept target exists", not problems, str(problems))
    ix = Index(g.to_dict())
    reach = set()
    for n in g.nodes.values():
        if n["id"].startswith("concept:"):
            reach |= set(ix.descendants(n["id"]))
    pkg_root = f"mod:{g.package}"
    unmapped = [n["id"] for n in g.nodes.values() if n["kind"] == "module" and n["id"].startswith(pkg_root + ".")
                and n["id"] not in reach]
    rep.check("V5.6b", "every package module is reachable from a concept", not unmapped, str(unmapped))


def v5_7_html(g_file, html, spec, rep):
    emb = embedded_graph(html)
    file_data = json.loads(Path(g_file).read_text(encoding="utf-8"))
    rep.check("V5.7a", "HTML embeds exactly the graph file (human = AI data)", emb == file_data)
    spec_ids = {c["id"] for c in json.loads(Path(spec).read_text(encoding="utf-8"))["components"]}
    html_text = Path(html).read_text(encoding="utf-8")
    svg = html_text[html_text.index("<svg"): html_text.index("</svg>")]
    svg_ids = set(re.findall(r'data-node-id="([^"]+)"', svg))
    concept_ids = {n["id"][8:] for n in file_data["nodes"] if n["id"].startswith("concept:")}
    rep.check("V5.7b", f"overview nodes = concepts ({len(spec_ids)})", spec_ids == svg_ids == concept_ids,
              f"spec-only {sorted(spec_ids - concept_ids)} concept-only {sorted(concept_ids - spec_ids)} "
              f"svg {sorted(svg_ids ^ spec_ids)}")
    rep.check("V5.7c", "panel script and mount call present once",
              html_text.count('id="mm-panel-js"') == 1 and html_text.count("ArchifyMinimap.mount(") == 1)


def v5_8_arrows(g, spec, rep):
    """Every overview arrow between two code concepts must be backed by a code dependency in that direction."""
    ix = Index(g.to_dict())
    comps = {c["id"] for c in json.loads(Path(spec).read_text(encoding="utf-8"))["components"]}
    code_kinds = {"module", "class", "function", "data"}
    bad, checked = [], 0
    declared = []
    for c in json.loads(Path(spec).read_text(encoding="utf-8")).get("connections", []):
        a, b = f"concept:{c['from']}", f"concept:{c['to']}"
        if c.get("variant") == "dashed":
            declared.append(c["id"])  # dashed = declared relationship, not claimed by code (DESIGN.md)
            continue
        da = [x for x in ix.descendants(a) if g.nodes.get(x, {}).get("kind") in code_kinds]
        db = set(x for x in ix.descendants(b) if g.nodes.get(x, {}).get("kind") in code_kinds)
        if not da or not db or c["from"] not in comps:
            continue
        checked += 1
        deps = {ch["id"] for ch in ix.children(a, "deps", rollup=True)}
        if not deps & db:
            bad.append(f"{c['from']} -> {c['to']} ({c.get('label', '')})")
    rep.check("V5.8", f"solid overview arrows between code concepts are backed by code ({checked} checked; "
                      f"dashed, not claimed: {declared})", not bad, str(bad))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    for k in ("root", "graph", "html", "spec", "observed", "problems"):
        ap.add_argument(f"--{k}", required=True)
    ap.add_argument("--expect-run", type=int, default=-1)
    ap.add_argument("--expect-skip", type=int, default=-1)
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    g = Graph.load(Path(a.graph))
    rep = Report()
    steps = [
        lambda: v5_1_fresh(g, a.root, rep),
        lambda: v5_2_existence(g, a.root, rep),
        lambda: v5_3_completeness(g, a.root, rep),
        lambda: v5_4_evidence(g, a.root, rep),
        lambda: v5_5_runtime(json.loads(Path(a.observed).read_text(encoding="utf-8")), rep, a.expect_run, a.expect_skip),
        lambda: v5_6_concepts(g, json.loads(Path(a.problems).read_text(encoding="utf-8")), rep),
        lambda: v5_7_html(a.graph, a.html, a.spec, rep),
        lambda: v5_8_arrows(g, a.spec, rep),
    ]
    for step in steps:
        step()
    for r in rep.rows:
        print(f"{r['status']}  {r['step']:<6} {r['check']}" + (f"\n        {r['detail'][:600]}" if r["status"] == "FAIL" else ""))
    fails = rep.failed()
    print(f"TOTAL {len(rep.rows)} checks, {len(fails)} FAIL")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rep.rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

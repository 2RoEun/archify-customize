"""Build the viewer: archify deliver on the overview spec, then inject the panel and the same graph JSON."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .safeio import atomic_write_bytes, read_bytes_retry

HERE = Path(__file__).resolve().parent
ARCHIFY = Path(os.environ.get("ARCHIFY_HOME", Path.home() / ".agents" / "skills" / "archify"))
# Node.js runs archify: MINIMAP_NODE if set, otherwise the node on PATH
NODE = Path(os.environ.get("MINIMAP_NODE") or shutil.which("node") or "node")
MARK = "<!-- archify-minimap -->"


def archify_deliver(spec: Path, out_html: Path) -> dict:
    env = dict(os.environ, ARCHIFY_UPDATE_CHECK_DISABLED="1")
    # archify runs with cwd=ARCHIFY, so relative paths would resolve inside the archify folder: pass absolute ones
    proc = subprocess.run([str(NODE), str(ARCHIFY / "bin" / "archify.mjs"), "deliver", "architecture",
                           str(Path(spec).absolute()), str(Path(out_html).absolute()), "--quality", "showcase",
                           "--json"], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(ARCHIFY))
    try:
        receipt = json.loads(proc.stdout)
    except ValueError:
        receipt = {"ok": False, "error": (proc.stdout + proc.stderr)[:2000]}
    receipt["exit_code"] = proc.returncode
    return receipt


def inject(html: str, graph_json: str) -> str:
    if MARK in html:
        raise ValueError("HTML already contains a minimap block")
    safe = graph_json.replace("</", "<\\/")
    block = (f"{MARK}\n<style id=\"mm-style\">{(HERE / 'panel.css').read_text(encoding='utf-8')}</style>\n"
             f"<script type=\"application/json\" id=\"minimap-graph\">{safe}</script>\n"
             f"<script id=\"mm-panel-js\">{(HERE / 'panel.js').read_text(encoding='utf-8')}</script>\n"
             "<script>ArchifyMinimap.mount(document, JSON.parse(document.getElementById('minimap-graph').textContent));"
             "</script>\n")
    k = html.rfind("</body>")
    if k < 0:
        raise ValueError("no </body> in archify output")
    return html[:k] + block + html[k:]


def build(spec: Path, graph_file: Path, out_html: Path) -> dict:
    # unique temp name per process: two builds never delete each other's file
    tmp = out_html.with_name(f".{out_html.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.archify.html")
    try:
        receipt = archify_deliver(spec, tmp)
        if receipt.get("exit_code") != 0 or not receipt.get("ok"):
            return {"ok": False, "archify": receipt}
        html = tmp.read_text(encoding="utf-8")
    finally:
        if tmp.exists():
            tmp.unlink()
    graph_json = read_bytes_retry(Path(graph_file)).decode("utf-8")
    atomic_write_bytes(out_html, inject(html, graph_json).encode("utf-8"))
    return {"ok": True, "archify": {k: receipt.get(k) for k in ("ok", "validation")}, "bytes": out_html.stat().st_size}


def embedded_graph(html_path: Path) -> dict:
    return graph_from_html(read_bytes_retry(Path(html_path)).decode("utf-8"))


def graph_from_html(html: str) -> dict:
    start = html.index('<script type="application/json" id="minimap-graph">') + len(
        '<script type="application/json" id="minimap-graph">')
    end = html.index("</script>", start)
    return json.loads(html[start:end].replace("<\\/", "</"))

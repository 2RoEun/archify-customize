# archify-customize — a dependency minimap for any Python project

Open a picture of your project, click any box, and drill down to the exact function: **what is inside it, what it uses, and who uses it**. Every link carries a trust grade and the file:line it came from. The same map also answers questions from the terminal, so AI coding agents can check "what breaks if I change this?" before they edit.

It is built on top of [archify](https://github.com/tt-a1i/archify) (which draws the architecture picture) and adds a code map made from static analysis **plus a traced run of your tests**.

> **Credit and independence:** archify is © 2026 tt-a1i and released under the MIT License. This repository contains none of archify's code: you install archify separately (section 2) and this tool calls it. This is an independent, unofficial project; it is not affiliated with or endorsed by the archify authors.

What you get from one command:

| Output | For whom | What it is |
|---|---|---|
| `NAME-minimap.html` | people | The architecture picture; click a box to open a panel with trees down to single functions, and switch overlays (impact, test coverage, arrow trust, risk, changes). Works on phones too. |
| `NAME.minimap.json` | scripts and AI agents | The same graph, queried with `mm.py` (impact, coverage, paths, data flow, diffs, ...). |

---

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Quick start (5 minutes, with the bundled example)](#3-quick-start-5-minutes-with-the-bundled-example)
4. [Use it on your own project](#4-use-it-on-your-own-project)
5. [Command reference, with real output](#5-command-reference-with-real-output)
6. [The HTML viewer](#6-the-html-viewer)
7. [How much to trust a link: grades](#7-how-much-to-trust-a-link-grades)
8. [Safety and reliability](#8-safety-and-reliability)
9. [Let AI agents use it](#9-let-ai-agents-use-it)
10. [How this tool was tested](#10-how-this-tool-was-tested)
11. [Known limits](#11-known-limits)
12. [Troubleshooting](#12-troubleshooting)
13. [한국어 안내](#13-한국어-안내)

---

## 1. Requirements

| Need | Version | Why |
|---|---|---|
| Python | 3.10 or newer (tested on 3.12) | runs the analysis, the test tracer and the queries |
| Node.js | 18 or newer (tested on 24) | runs archify, which draws the picture |
| archify | current | the diagram engine |
| Your project | a Python package with a `unittest` test suite in `tests/` | the map is built from the code and from one run of these tests |

Works on Windows, macOS and Linux. The memory cap on the build (section 8) is Windows only; everything else works everywhere.

## 2. Install

**Step 1 — install archify** (one time):

```bash
npx skills add tt-a1i/archify -g
```

This puts archify in `~/.agents/skills/archify`, which is where this tool looks by default. If you installed it somewhere else, point to it:

```bash
# macOS / Linux / Git Bash
export ARCHIFY_HOME=/path/to/archify
# Windows PowerShell
$env:ARCHIFY_HOME = "C:\path\to\archify"
```

**Step 2 — get this tool:**

```bash
git clone https://github.com/<your-account>/archify-customize.git
cd archify-customize
```

There is nothing else to install: no pip packages are needed. `node` is found on your PATH; set `MINIMAP_NODE` to the full path of `node` if you want a different one.

**Step 3 — check that everything works** (optional, about 40 seconds):

```bash
python -m unittest discover -s tests -t .
```

You should see `Ran 90 tests ... OK`.

## 3. Quick start (5 minutes, with the bundled example)

The repository contains a small example project, `examples/vbank` (a toy bank: accounts, a store that appends to `data/events.jsonl`, reports and a CLI, with two tests).

**1. Build its map:**

```bash
python run.py --root examples/vbank --package vbank --name vbank
```

It extracts the code, runs the example's tests under a tracer, builds the picture, **verifies everything (13 checks)** and only then publishes. The end of the output looks like this:

```
[6] verify the unpublished pair
TOTAL 13 checks, 0 FAIL
[7] publish
    published vbank.minimap.json and vbank-minimap.html
peak memory of the build: 97 MB (cap 8192 MB)
```

**2. Look at it:** open `examples/vbank/docs/diagrams/vbank-minimap.html` in a browser. Click the **store** box: a panel opens with its contents. Try the mode buttons at the bottom left (section 6).

**3. Ask it something from the terminal:** "if I change `Store.append`, what is affected?"

```bash
python mm.py --graph examples/vbank/docs/diagrams/vbank.minimap.json --node fn:vbank.store.Store.append --impact --min-grade static
```

```
impact of fn:vbank.store.Store.append (edges >= static); level 0 = the node and everything inside it
L1 (2)
  operation [function] fn:vbank.accounts.Accounts.deposit.operation vbank/accounts.py:9
  operation [function] fn:vbank.accounts.Accounts.withdraw.operation vbank/accounts.py:18
L2 (1)
  locked [function] fn:vbank.store.Store.locked vbank/store.py:19
L3 (2)
  deposit [function] fn:vbank.accounts.Accounts.deposit vbank/accounts.py:8
  withdraw [function] fn:vbank.accounts.Accounts.withdraw vbank/accounts.py:14
L4 (2)
  test_cli [function] fn:tests.test_vbank.VBank.test_cli tests/test_vbank.py:22
  test_flow [function] fn:tests.test_vbank.VBank.test_flow tests/test_vbank.py:14
boxes: concept:accounts L1, concept:store L0
tests reaching it: 2
writes files: data:data/events.jsonl. Code that READS them is not in the list above ...
  data:data/events.jsonl: 1 readers
    fn:vbank.store.Store.read_all  vbank/store.py:17
      read through it by: fn:vbank.reports.audit, fn:vbank.accounts.Accounts.balance
```

How to read it: **L1** uses `append` directly, **L2** uses L1, and so on. Two boxes of the picture are touched, both tests reach it, and because `append` writes a file, the code that *reads* that file is listed too — change the file format and those readers are what break.

Tip: set the map once and drop `--graph`:

```bash
export MINIMAP_GRAPH=examples/vbank/docs/diagrams/vbank.minimap.json      # PowerShell: $env:MINIMAP_GRAPH = "..."
python mm.py --find append
```

## 4. Use it on your own project

Your project needs this layout (only `docs/diagrams/` is new):

```
myproject/
  mypkg/                 your package (the code to map)
  tests/                 unittest tests (run with: python -m unittest discover -s tests)
  docs/diagrams/
    myname.architecture.json   the overview picture (archify spec)
    myname.concepts.json       which code belongs to which box
```

**Step 1 — draw the overview** (`myname.architecture.json`). This is a normal archify spec: boxes (`components`) with an `id`, a `type`, a `label`, a position and a size, and arrows (`connections`). Start from the example and edit it: `examples/vbank/docs/diagrams/vbank.architecture.json`. A shortened version:

```json
{
 "schema_version": 1, "diagram_type": "architecture",
 "meta": {"title": "vbank", "output": "vbank-minimap.html", "quality_profile": "showcase"},
 "components": [
  {"id": "accounts", "type": "backend", "label": "accounts", "pos": [480, 60], "size": [140, 60]},
  {"id": "store", "type": "backend", "label": "store", "pos": [480, 220], "size": [140, 60]}
 ],
 "connections": [
  {"id": "accounts-to-store", "from": "accounts", "to": "store", "label": "calls"}
 ]
}
```

Draw an arrow from A to B when **A uses B**. Use `"variant": "dashed"` for a relationship you want to show but that is not a code dependency. archify checks the layout; if a label overlaps a box it tells you exactly where to move it (`labelAt`).

**Step 2 — say what belongs in each box** (`myname.concepts.json`). The keys must be the same ids as the boxes above:

```json
{"schema": "archify-minimap-concepts/1", "concepts": {
  "accounts": {"label": "accounts", "targets": ["mod:vbank.accounts"]},
  "store":    {"label": "store",    "targets": ["mod:vbank.store"]},
  "events":   {"label": "events.jsonl", "targets": ["data:data/events.jsonl"]}
}}
```

Targets are node ids: `mod:<package.module>` for a module (everything inside it comes along), `data:<path>` for a data file the code reads or writes, `doc:<file>` for a document. Every module of your package should be inside some box — the build checks this and tells you which ones are missing.

**Step 3 — build:**

```bash
python run.py --root path/to/myproject --package mypkg --name myname
```

The first time, any number of tests is accepted (failures still fail the build). Once it passes, pin your real counts so a silently skipped test is noticed next time: `--expect-run 245 --expect-skip 2`.

**Step 4 — rebuild whenever the code changes.** `python mm.py --graph ... --stale` tells you when the map is older than the code (exit code 1). A build takes seconds for small projects and about a minute for a project with a few hundred tests.

## 5. Command reference, with real output

All queries are read-only. In the examples, `mm` means `python mm.py --graph examples/vbank/docs/diagrams/vbank.minimap.json`. Add `--json` to any query for machine-readable output. `python mm.py --help` lists every option.

| Command | Question it answers |
|---|---|
| `mm --find TEXT` | What is the id of the thing called TEXT? |
| `mm --node ID --dir down/deps/users [--rollup] [--depth N]` | What is inside it / what does it use / who uses it? (`--rollup` merges everything inside a box) |
| `mm --node ID --impact [--min-grade verified/static/guess]` | If I change it, what is affected, level by level? (also lists readers of files it writes) |
| `mm --coverage [--node ID]` | How much of each box actually ran in the tests? |
| `mm --arrows` | Is each arrow in the picture really backed by code? |
| `mm --hotspots [--kind function/class/module]` | Where is the risk: much depends on it, but the tests never ran it? |
| `mm --node A --path B` | How does A end up depending on B? (shortest path, with file:line per step) |
| `mm --flow [--node data:FILE]` | Who writes and who reads a data file? |
| `mm --diff` | What changed since the previous build? |
| `mm --stale` | Is the map older than the code? (exit 1 = rebuild) |
| `python gate.py PLAN.json` | Can these work units safely run in parallel? (see `minimap/gate.py`) |

**Coverage** — note how the tool tells you that `cli.main` *is* tested, just in a child process it cannot trace:

```
coverage: functions that ran in the traced test suite
  concept:accounts                      6/6    100.0%
  concept:cli                           0/1    0.0%
  concept:reports                       1/2    50.0%
  concept:store                         4/4    100.0%
  total                                13/15   86.7%
never ran (inside boxes): 2
  main fn:vbank.cli.main vbank/cli.py:6  [the tests DO run this, in a child process started at tests/test_vbank.py:26; the tracer cannot see it]
  audit fn:vbank.reports.audit vbank/reports.py:9
```

**Arrow trust** — the picture's arrows checked against the code:

```
  cli-to-reports           static       edges 2 (static 2)
  reports-to-accounts      verified     edges 4 (verified 2, static 2)
  accounts-to-store        verified     edges 7 (verified 6, static 1); reverse 2
  store-to-events          guess        edges 4 (guess 4)
  reports-to-events        declared     edges 0 (-)
```

**Path** — and a warning when a step may not be a real call chain:

```
path fn:vbank.accounts.Accounts.deposit -> fn:vbank.accounts.Accounts.withdraw.operation (edges >= guess): 2 hops, weakest runtime-only
  1. deposit -calls[verified]-> locked  vbank/accounts.py:12
  2. locked -calls[runtime-only]-> operation  vbank/store.py:21
note: a runtime-only step was seen while the tests ran but is not tied to this caller; a shared helper that runs callbacks links every caller to every callback. Confirm the chain in the code before relying on it.
```

(`Store.locked` runs whatever callback it is given, so the tests saw it call both callbacks. `deposit` never really runs `withdraw`'s callback — the note tells you to check.)

**Data flow:**

```
writers (1)
  fn:vbank.store.Store.append [guess] vbank/store.py:11
readers (1)
  fn:vbank.store.Store.read_all [guess] vbank/store.py:17
path users (2)
  fn:vbank.store.Store.__init__ [guess] vbank/store.py:8
  fn:vbank.store.Store.read_all [guess] vbank/store.py:15
```

## 6. The HTML viewer

Open `NAME-minimap.html` in any browser (no server needed).

- **Click a box** in the picture: a panel opens (a bottom sheet on phones; drag its handle to resize).
- The panel's three tabs are in Korean: **포함** = contains, **의존** = depends on, **피의존** = used by. The checkbox **하위 요소 전체 합치기(롤업)** merges everything inside the box. The search field finds any function or module; click any row to jump to it; **← 뒤로** goes back.
- The **mode bar** (bottom left) draws overlays on the picture:

| Button | Meaning | Colours |
|---|---|---|
| 끔 (off) | the plain picture, exactly as archify drew it | — |
| 영향 (impact) | click a box first: what is affected if it changes | red L0 (the box) · orange L1 · yellow L2 · pale L3+ |
| 커버리지 (coverage) | share of functions the tests ran | green ≥90% · yellow ≥70% · red <70% |
| 화살표 (arrows) | how well each arrow is backed by code | green verified · blue static · purple runtime-only · orange guess · red none · grey declared / not code |
| 위험 (hotspots) | ⚠ count of functions others depend on that never ran | — |
| 변경 (changes) | since the previous build: +added −removed ~changed | — |

archify's own features (themes, zoom, export as image) keep working; mode "off" leaves the picture byte-for-byte unchanged.

## 7. How much to trust a link: grades

| Grade | Meaning |
|---|---|
| `verified` | found in the code **and** seen while the tests ran — the strongest evidence |
| `static` | resolved with certainty from the code, but not observed at run time |
| `runtime-only` | seen at run time but not visible statically (callbacks, `with` blocks, decorators, `getattr`) |
| `guess` | matched by name only (the type of a variable is unknown), and all file access |

`--min-grade static` ignores guesses; `--min-grade guess` shows the widest picture.

## 8. Safety and reliability

- **Nothing is published unless it passes verification.** The graph and HTML are built as temporary files, checked (13 checks: the map matches the code, every class and function is found at its file:line, the tests passed, the picture's boxes and arrows match the code, ...), and only then swapped in. A failed build leaves the previous outputs untouched.
- **One build at a time.** An OS lock next to the output; a second build exits with code 4 ("busy") and changes nothing.
- **It cannot freeze your computer** (Windows): the whole build runs in a job object with a memory cap (`MINIMAP_MEMORY_MB`, default 8192) and all child processes die with it. The build prints its peak memory.
- Exit codes of `run.py`: 0 published · 1 verification failed · 2 the tests failed · 3 build error before verification · 4 busy · 5 publish failed (previous outputs kept).

## 9. Let AI agents use it

Paste a rule like this into your agents' instructions (`AGENTS.md`, `CLAUDE.md`, ...):

> Before changing Python code in this project: run `python mm.py --stale` (exit 1 = rebuild with `run.py` first). Find the node with `--find`, then run `--node <id> --impact --min-grade static` and cite the stale state, node id, count per level, boxes and tests reaching it in your notes. If the change alters what the code writes to a file, open every reader listed under "writes files" and cite the line that decides whether it still accepts the new format; never call a reader compatible without looking.

Why the last sentence: in testing, a small model once listed a file's readers and declared them "compatible" without opening them — one of them rejected any record with an extra field.

## 10. How this tool was tested

- **90 unit tests** on hand-built fixtures with hand-derived answers (extraction, type tracking, tracing, trees, impact, coverage, arrow trust, hotspots, paths, data flow, diffs, the parallel gate, the viewer's JavaScript = the Python queries, concurrency and publishing).
- **Planted mutants:** `python mutants.py` breaks the code on purpose (one change at a time, under a memory cap) and checks that the tests fail. Every mutant is caught.
- **Browser checks:** every overlay colour and badge compared with the Python prediction for every box; mode "off" byte-identical to the original picture; phone sizes 412×891, 360×780 and 891×412 without overlap; checked on a real phone.
- **AI agent test:** fresh agents of three sizes were given a real editing task **without being told about this tool**. All of them found and followed the rule from the project's instructions every time. Where the weakest one missed a consequence, the fix was to put the answer into the tool's output (readers of written files, "the tests DO run this" tags) rather than to add more instructions — that is why the outputs above look the way they do.

## 11. Known limits

- The tests must be `unittest` tests in `tests/` (they are run in-process under a tracer). Code that tests start as a **child process** is not traced; the tool tags those entry points instead of counting them as tested.
- Calls through variables of unknown type are `guess`; the tool resolves them statically only when certain (one class for every binding, annotated parameters, `self` attributes, `with X() as x`).
- File access is seen statically only, so data-file links are at best `guess`. Paths reached through another object's attribute (`self.other.path`) are missed.
- Paths through a shared helper that runs callbacks can join different callers (the tool warns).
- The viewer's panel labels are in Korean (see section 6).

## 12. Troubleshooting

| You see | Do this |
|---|---|
| `no overview spec: .../NAME.architecture.json` | create it and `NAME.concepts.json` (section 4) |
| archify / node not found | install archify (section 2) or set `ARCHIFY_HOME`; install Node.js or set `MINIMAP_NODE` |
| `FAIL V5.5 traced suite ...` | your tests failed, or the counts differ from `--expect-run/--expect-skip`; run the tests yourself first |
| `FAIL V5.6b ... not reachable from a concept` | a module is in no box; add it to a box's `targets` |
| a label overlaps a box (archify layout error) | move the label with `labelAt` as the message suggests |
| `busy` (exit 4) | another build of the same map is running; wait and retry |
| `no map found` from `mm.py` | build first, then pass `--graph .../NAME.minimap.json` or set `MINIMAP_GRAPH` |

## License

MIT — free for anyone to use, copy, modify and share, including commercially; keep the copyright notice. See [LICENSE](LICENSE). archify itself has its own MIT license (© tt-a1i).

## 13. 한국어 안내

어떤 파이썬 프로젝트나 라이브러리든 구조를 그림으로 띄워서, 박스를 누르면 함수 단위까지 **무엇이 들어 있고, 무엇을 쓰고, 누가 쓰는지** 자세히 볼 수 있게 해 주는 도구입니다. 모든 연결에는 신뢰 등급과 근거(파일:줄)가 붙습니다. 같은 지도를 터미널에서도 조회할 수 있어서, AI 에이전트가 코드를 고치기 전에 "이걸 바꾸면 어디가 깨지나"를 확인할 수 있습니다.

1. 준비: Python 3.10+, Node.js 18+, archify(`npx skills add tt-a1i/archify -g`).
2. 예제로 바로 해 보기: `python run.py --root examples/vbank --package vbank --name vbank` → `examples/vbank/docs/diagrams/vbank-minimap.html`을 브라우저로 열고 박스를 눌러 보세요.
3. 내 프로젝트에 쓰기: `docs/diagrams/`에 그림 설정(`이름.architecture.json`)과 박스별 코드 목록(`이름.concepts.json`)을 만들고 `run.py --root 내프로젝트 --package 패키지 --name 이름`을 실행합니다(4장).
4. 질문하기: `python mm.py --graph 지도.json --node <id> --impact` 등(5장).
5. 화면 아래 왼쪽 모드 버튼: 끔 · 영향 · 커버리지 · 화살표 · 위험 · 변경(6장).

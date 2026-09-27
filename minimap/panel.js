/* archify-minimap viewer panel. Tree rules mirror minimap/query.py (DESIGN.md §6); tests compare both. */
(function (root) {
  'use strict';
  const DOWN_KINDS = ['contains', 'maps'];
  const DEP_KINDS = ['imports', 'calls', 'reads', 'writes', 'uses_path'];
  const USE_KINDS = DEP_KINDS; // users = exact reverse of deps
  const KIND_ORDER = ['concept', 'actor', 'doc', 'package', 'module', 'test', 'script', 'class', 'function', 'data'];

  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function Index(graph) {
    this.nodes = {};
    this.out = {};
    this.inc = {};
    for (const n of graph.nodes) this.nodes[n.id] = n;
    for (const e of graph.edges) {
      (this.out[e.from] = this.out[e.from] || []).push(e);
      (this.inc[e.to] = this.inc[e.to] || []).push(e);
    }
  }
  Index.prototype.sortKey = function (id) {
    const n = this.nodes[id] || { kind: 'data', name: id };
    const k = KIND_ORDER.indexOf(n.kind);
    return [k < 0 ? 99 : k, n.name, id];
  };
  Index.prototype.compareIds = function (a, b) {
    const ka = this.sortKey(a), kb = this.sortKey(b);
    return (ka[0] - kb[0]) || cmp(ka[1], kb[1]) || cmp(ka[2], kb[2]);
  };
  Index.prototype.descendants = function (id) {
    const seen = new Set([id]); const stack = [id];
    while (stack.length) {
      const cur = stack.pop();
      for (const e of this.out[cur] || []) {
        if (DOWN_KINDS.includes(e.kind) && !seen.has(e.to)) { seen.add(e.to); stack.push(e.to); }
      }
    }
    return Array.from(seen).sort(cmp);
  };
  Index.prototype.children = function (id, direction, rollup) {
    const group = new Set(rollup && direction !== 'down' ? this.descendants(id) : [id]);
    const found = new Map();
    const add = (k, e) => { if (!found.has(k)) found.set(k, new Set()); found.get(k).add(e.kind + ':' + e.grade); };
    for (const src of Array.from(group).sort(cmp)) {
      if (direction === 'users') {
        for (const e of this.inc[src] || []) if (USE_KINDS.includes(e.kind) && !group.has(e.from)) add(e.from, e);
      } else {
        const kinds = direction === 'down' ? DOWN_KINDS : DEP_KINDS;
        for (const e of this.out[src] || []) if (kinds.includes(e.kind) && !group.has(e.to)) add(e.to, e);
      }
    }
    return Array.from(found.keys()).sort((a, b) => this.compareIds(a, b))
      .map((k) => ({ id: k, via: Array.from(found.get(k)).sort(cmp) }));
  };
  Index.prototype.tree = function (id, direction, depth, rollup, path, via) {
    path = path || [];
    const node = { id: id, via: via || [], cycle: path.includes(id), children: [] };
    if (node.cycle || depth <= 0) return node;
    for (const c of this.children(id, direction, rollup)) {
      node.children.push(this.tree(c.id, direction, depth - 1, rollup, path.concat([id]), c.via));
    }
    return node;
  };

  /* ------------------------------------------------------------ STEP 4: impact, same as query.py Index.impact */
  const GRADE_RANK = { verified: 3, static: 2, 'runtime-only': 2, guess: 1 };
  const MIN_RANK = { verified: 3, static: 2, guess: 1 };
  Index.prototype.parents = function (id) {
    return (this.inc[id] || []).filter((e) => DOWN_KINDS.includes(e.kind)).map((e) => e.from);
  };
  Index.prototype.boxesOf = function (id) {
    const out = new Set(); const seen = new Set([id]); const stack = [id];
    while (stack.length) {
      const cur = stack.pop();
      if (cur.startsWith('concept:')) out.add(cur);
      for (const p of this.parents(cur)) if (!seen.has(p)) { seen.add(p); stack.push(p); }
    }
    return out;
  };
  Index.prototype.impact = function (id, minGrade, maxLevel) {
    minGrade = minGrade || 'guess';
    maxLevel = maxLevel === undefined ? 10 : maxLevel;
    const floor = MIN_RANK[minGrade];
    if (floor === undefined) throw new Error('unknown min grade: ' + minGrade);
    const start = this.descendants(id);
    const visited = new Set(start); let frontier = start; const levels = [];
    while (frontier.length && levels.length < maxLevel) {
      const nxt = new Set();
      for (const t of frontier.slice().sort(cmp)) {
        for (const e of this.inc[t] || []) {
          if (DEP_KINDS.includes(e.kind) && (GRADE_RANK[e.grade] || 0) >= floor && !visited.has(e.from)) nxt.add(e.from);
        }
      }
      if (!nxt.size) break;
      for (const n of nxt) visited.add(n);
      const lvl = Array.from(nxt).sort((a, b) => this.compareIds(a, b));
      levels.push(lvl);
      frontier = lvl;
    }
    const boxes = new Map();
    for (const b of this.boxesOf(id)) boxes.set(b, 0);
    if (id.startsWith('concept:')) boxes.set(id, 0);
    levels.forEach((lvl, i) => {
      for (const n of lvl) for (const b of this.boxesOf(n)) if (!boxes.has(b)) boxes.set(b, i + 1);
    });
    const boxObj = {};
    for (const b of Array.from(boxes.keys()).sort(cmp)) boxObj[b] = boxes.get(b);
    const isTest = (n) => { const x = this.nodes[n] || {}; return x.role === 'test' || x.kind === 'test'; };
    const tests = [].concat(...levels).filter(isTest);
    return { root: id, min_grade: minGrade, levels: levels, boxes: boxObj, tests: tests };
  };

  /* ------------------------------------------------------------ DOM (browser only) */
  // Phone layout (2026-09-26): bottom sheet under 700 px (target Galaxy S26 Ultra, CSS viewport 412 x 891),
  // drag handle to resize, larger tap targets, lazy children (built on first expand), long lists paged.
  const LABELS = { down: ['포함', ' (하위 요소)'], deps: ['의존', ' (내가 쓰는 것)'], users: ['피의존', ' (나를 쓰는 것)'] };
  const GRADE_HELP = { verified: '정적+실행 확인', static: '정적 확정', guess: '추정', 'runtime-only': '실행에서만 관찰' };
  const PAGE = 200;

  function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  function mount(doc, graph) {
    const ix = new Index(graph);
    const panel = doc.createElement('aside');
    panel.id = 'mm-panel';
    panel.innerHTML = '<div id="mm-grip" title="끌어서 높이 조절"></div>' +
      '<header><strong id="mm-title">Minimap</strong><button id="mm-close" title="닫기" aria-label="닫기">×</button></header>' +
      '<div id="mm-meta"></div><input id="mm-search" type="search" placeholder="노드 검색 (함수·모듈 이름)">' +
      '<div id="mm-results"></div><nav id="mm-tabs"></nav>' +
      '<label id="mm-roll"><input type="checkbox" id="mm-rollup" checked> 하위 요소 전체 합치기(롤업)</label>' +
      '<div id="mm-tree"></div><footer id="mm-legend"></footer>';
    doc.body.appendChild(panel);
    const state = { id: null, dir: 'down', history: [] };
    const $ = (s) => panel.querySelector(s);
    $('#mm-legend').innerHTML = Object.keys(GRADE_HELP).map((g) => '<span class="mm-g mm-' + g + '">' + g + '</span> ' + GRADE_HELP[g]).join(' · ') +
      '<br>그래프: ' + graph.nodes.length + ' 노드 · ' + graph.edges.length + ' 연결 · 생성 ' + esc(graph.stats && graph.stats.generated || '');
    for (const d of ['down', 'deps', 'users']) {
      const b = doc.createElement('button');
      b.innerHTML = LABELS[d][0] + '<span class="mm-long">' + LABELS[d][1] + '</span>';
      b.dataset.dir = d;
      b.onclick = () => { state.dir = d; render(); };
      $('#mm-tabs').appendChild(b);
    }
    $('#mm-close').onclick = () => panel.classList.remove('open');
    $('#mm-rollup').onchange = render;
    $('#mm-search').oninput = (ev) => {
      const q = ev.target.value.trim().toLowerCase();
      const box = $('#mm-results'); box.innerHTML = '';
      if (q.length < 2) return;
      graph.nodes.filter((n) => n.id.toLowerCase().includes(q) || n.name.toLowerCase().includes(q)).slice(0, 30)
        .forEach((n) => { const a = doc.createElement('a'); a.textContent = n.name + ' [' + n.kind + ']'; a.title = n.id; a.onclick = () => open(n.id); box.appendChild(a); });
    };

    // drag handle: resize the bottom sheet (phones); height kept in a CSS variable
    const grip = $('#mm-grip');
    grip.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      grip.setPointerCapture(ev.pointerId);
      const move = (e) => {
        const vh = doc.documentElement.clientHeight;
        const h = Math.min(vh * 0.94, Math.max(vh * 0.3, vh - e.clientY));
        // on the root, so the mode bar (STEP 13) can sit just above the sheet as it is resized
        doc.documentElement.style.setProperty('--mm-h', Math.round(h) + 'px');
      };
      const up = () => { grip.removeEventListener('pointermove', move); grip.removeEventListener('pointerup', up); };
      grip.addEventListener('pointermove', move);
      grip.addEventListener('pointerup', up);
    });

    function open(id) {
      if (!ix.nodes[id]) return;
      if (state.id && state.id !== id) state.history.push(state.id);
      state.id = id; panel.classList.add('open'); $('#mm-results').innerHTML = ''; render();
      if (mode === 'impact') applyOverlay();
    }

    /* ---------------- STEP 12: diagram overlays + mode switch. Numbers come from stats.overlays (computed by
       query.py at build time); impact is computed live with Index.impact (same as query.py, STEP 4). */
    const OV = (graph.stats && graph.stats.overlays) || null;
    const MODES = [['off', '끔'], ['impact', '영향'], ['coverage', '커버리지'], ['arrows', '화살표'], ['hotspots', '위험'], ['diff', '변경']];
    const HELP = {
      off: '',
      impact: '영향(≥static): 빨강 L0 선택한 곳 · 주황 L1 · 노랑 L2 · 연노랑 L3+',
      coverage: '테스트에서 실행된 함수 비율: 초록 ≥90% · 노랑 ≥70% · 빨강 <70%',
      arrows: '화살표 근거: 초록 verified · 파랑 static · 보라 runtime-only · 주황 guess · 빨강 근거 없음 · 회색 선언/코드 아님',
      hotspots: '⚠ = 다른 코드가 쓰는데 테스트에서 안 돈 함수 수',
      diff: '직전 빌드 대비 +추가 −삭제 ~등급·실행 변경',
    };
    const SVGNS = 'http://www.w3.org/2000/svg';
    let mode = 'off';
    const bar = doc.createElement('div');
    bar.id = 'mm-modes';
    bar.innerHTML = MODES.map((m) => '<button data-mode="' + m[0] + '">' + m[1] + '</button>').join('') + '<div id="mm-mode-help"></div>';
    doc.body.appendChild(bar);
    // STEP 13: room at the end of the page, so the last content can always be scrolled clear of the fixed bar
    const spacer = doc.createElement('div');
    spacer.id = 'mm-spacer';
    doc.body.appendChild(spacer);
    bar.querySelectorAll('button').forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
    const q = (s) => (typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(s) : s);
    const boxEls = (cid) => doc.querySelectorAll('svg [data-node-id="' + q(cid.slice(8)) + '"]');

    function clearOverlay() {
      doc.querySelectorAll('.mm-ov').forEach((e) => e.remove());
      doc.querySelectorAll('svg [data-node-id], svg [data-edge-id]').forEach((e) => {
        Array.from(e.classList).filter((c) => c.startsWith('mm-ov-')).forEach((c) => e.classList.remove(c));
        if (e.getAttribute('class') === '') e.removeAttribute('class');  // leave the element exactly as archify made it
      });
    }
    function mark(el, cls, badge, tip) {
      const bb = el.getBBox();
      el.classList.add('mm-ov-' + cls);
      const r = doc.createElementNS(SVGNS, 'rect');
      r.setAttribute('class', 'mm-ov mm-ov-frame mm-ovf-' + cls);
      [['x', bb.x - 5], ['y', bb.y - 5], ['width', bb.width + 10], ['height', bb.height + 10], ['rx', 10]]
        .forEach((a) => r.setAttribute(a[0], a[1]));
      el.appendChild(r);
      if (badge) {
        const t = doc.createElementNS(SVGNS, 'text');
        t.setAttribute('class', 'mm-ov mm-ov-badge');
        t.setAttribute('x', bb.x + bb.width - 6); t.setAttribute('y', bb.y + 17); t.setAttribute('text-anchor', 'end');
        t.textContent = badge;
        el.appendChild(t);
      }
      if (tip) { const ti = doc.createElementNS(SVGNS, 'title'); ti.setAttribute('class', 'mm-ov'); ti.textContent = tip; el.appendChild(ti); }
    }
    function applyOverlay() {
      clearOverlay();
      const help = bar.querySelector('#mm-mode-help');
      help.textContent = HELP[mode] || '';
      bar.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
      if (mode === 'off') return;
      if (mode === 'impact') {
        if (!state.id) { help.textContent += ' — 먼저 박스나 노드를 선택'; return; }
        const r = ix.impact(state.id, 'static');
        Object.keys(r.boxes).forEach((b) => boxEls(b).forEach((el) => mark(el, 'impact-' + Math.min(r.boxes[b], 3), 'L' + r.boxes[b])));
        help.textContent += ' — 기준: ' + (ix.nodes[state.id].name || state.id);
        return;
      }
      if (!OV) { help.textContent = '이 그래프에는 표시 데이터가 없음 (run.py로 다시 빌드)'; return; }
      if (mode === 'coverage') {
        Object.keys(OV.coverage).forEach((b) => {
          const c = OV.coverage[b];
          if (c.share === null || !c.functions) return;
          const band = c.share >= 0.9 ? 'good' : c.share >= 0.7 ? 'mid' : 'low';
          boxEls(b).forEach((el) => mark(el, 'cov-' + band, Math.round(c.share * 100) + '%', c.ran + '/' + c.functions + ' 함수 실행'));
        });
      } else if (mode === 'arrows') {
        // the visible arrow is the edge path without data-detail (archify also draws an invisible "context" twin)
        Object.keys(OV.arrows).forEach((id) => doc.querySelectorAll('svg [data-edge-id="' + q(id) + '"]:not([data-detail])')
          .forEach((el) => el.classList.add('mm-ov-trust-' + OV.arrows[id].trust)));
      } else if (mode === 'hotspots') {
        Object.keys(OV.hotspots).forEach((b) => boxEls(b).forEach((el) => mark(el, 'hot', '⚠' + OV.hotspots[b].count, '최고: ' + OV.hotspots[b].top_id)));
      } else if (mode === 'diff') {
        if (!OV.diff) { help.textContent += ' — 직전 빌드가 없어 비교 불가'; return; }
        help.textContent += ' (기준 ' + OV.diff.old + ')';
        Object.keys(OV.diff.boxes).forEach((b) => {
          const d = OV.diff.boxes[b];
          const txt = [d.added ? '+' + d.added : '', d.removed ? '−' + d.removed : '', d.changed ? '~' + d.changed : ''].filter(Boolean).join(' ');
          boxEls(b).forEach((el) => mark(el, 'diff', txt));
        });
      }
    }
    function setMode(m) { mode = MODES.some((x) => x[0] === m) ? m : 'off'; applyOverlay(); }
    function label(id) {
      const n = ix.nodes[id] || { name: id, kind: '?' };
      const name = n.method && n.qual ? n.qual.split('.').slice(-2).join('.') : n.name;  // Class.method
      return '<span class="mm-name">' + esc(name) + '</span> <em>' + esc(n.kind) + '</em>';
    }
    function fillList(ul, items, path) {
      let shown = 0;
      const more = () => {
        items.slice(shown, shown + PAGE).forEach((c) => ul.appendChild(row(c.id, c.via, path)));
        shown = Math.min(items.length, shown + PAGE);
        const old = ul.querySelector(':scope > li.mm-more'); if (old) old.remove();
        if (shown < items.length) {
          const li = doc.createElement('li'); li.className = 'mm-more';
          const b = doc.createElement('button'); b.textContent = '더 보기 (' + (items.length - shown) + '개 남음)';
          b.onclick = more; li.appendChild(b); ul.appendChild(li);
        }
      };
      more();
    }
    function row(id, via, path) {
      const li = doc.createElement('li');
      const n = ix.nodes[id] || {};
      const cycle = path.includes(id);
      const kids = cycle ? [] : ix.children(id, state.dir, $('#mm-rollup').checked);
      const grades = via.map((v) => { const [k, g] = v.split(':'); return '<span class="mm-g mm-' + g + '" title="' + (GRADE_HELP[g] || g) + '">' + k + '</span>'; }).join('');
      const loc = n.file ? '<small class="mm-loc">' + esc(n.file) + (n.line ? ':' + n.line : '') + '</small>' : '';
      const count = kids.length ? '<span class="mm-count">' + kids.length + '</span>' : '';
      li.innerHTML = '<div class="mm-row"><span class="mm-tog">' + (cycle ? '↺' : (kids.length ? '▸' : '·')) + '</span>' +
        '<div class="mm-body"><a class="mm-node">' + label(id) + '</a>' + count + ' ' + grades + (cycle ? ' <small>(순환)</small>' : '') + loc + '</div></div>';
      li.querySelector('.mm-node').onclick = () => open(id);
      if (kids.length) {
        let ul = null;
        const tog = li.querySelector('.mm-tog');
        tog.onclick = () => {
          if (!ul) { ul = doc.createElement('ul'); fillList(ul, kids, path.concat([id])); li.appendChild(ul); tog.textContent = '▾'; return; }
          ul.hidden = !ul.hidden; tog.textContent = ul.hidden ? '▸' : '▾';
        };
      }
      return li;
    }
    function render() {
      if (!state.id) return;
      const n = ix.nodes[state.id];
      $('#mm-title').innerHTML = label(state.id);
      $('#mm-meta').innerHTML = '<code>' + esc(state.id) + '</code>' + (n.file ? '<br>' + esc(n.file) + (n.line ? ':' + n.line + '–' + (n.end || n.line) : '') : '') +
        (state.history.length ? ' <a id="mm-back">← 뒤로</a>' : '');
      const back = $('#mm-back'); if (back) back.onclick = () => { state.id = state.history.pop(); render(); };
      panel.querySelectorAll('#mm-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.dir === state.dir));
      const top = ix.children(state.id, state.dir, $('#mm-rollup').checked);
      const box = $('#mm-tree'); box.innerHTML = '';
      if (!top.length) { box.appendChild(Object.assign(doc.createElement('p'), { textContent: '없음' })); return; }
      const ul = doc.createElement('ul');
      fillList(ul, top, [state.id]);
      box.appendChild(ul);
    }
    doc.addEventListener('click', (ev) => {
      const g = ev.target.closest && ev.target.closest('[data-node-id]');
      if (!g || panel.contains(g)) return;
      const cid = 'concept:' + g.getAttribute('data-node-id');
      if (ix.nodes[cid]) open(cid);
    }, true);
    return { open: open, index: ix, setMode: setMode, mode: () => mode };
  }

  const api = { Index: Index, mount: mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ArchifyMinimap = api;
})(typeof window !== 'undefined' ? window : globalThis);

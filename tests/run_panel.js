// Usage: node run_panel.js <graph.json> <depth>  -> JSON {"id|dir|rollup": tree} for every node, used by V4.4.
const fs = require('fs');
const path = require('path');
const { Index } = require(path.join(__dirname, '..', 'minimap', 'panel.js'));
const graph = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const depth = Number(process.argv[3] || 3);
const ix = new Index(graph);
const out = {};
for (const n of graph.nodes) {
  for (const dir of ['down', 'deps', 'users']) {
    for (const rollup of [false, true]) {
      out[n.id + '|' + dir + '|' + rollup] = ix.tree(n.id, dir, depth, rollup);
    }
  }
}
process.stdout.write(JSON.stringify(out));

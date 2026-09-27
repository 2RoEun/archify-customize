// Usage: node run_impact.js <graph.json> [panel.js]  -> JSON {"id|minGrade|maxLevel": impact} for every node (STEP 4).
// The optional second argument loads another panel.js copy (used to prove the test catches a planted mutant).
const fs = require('fs');
const path = require('path');
const { Index } = require(path.resolve(process.argv[3] || path.join(__dirname, '..', 'minimap', 'panel.js')));
const graph = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const ix = new Index(graph);
const out = {};
for (const n of graph.nodes) {
  for (const g of ['verified', 'static', 'guess']) {
    for (const m of [10, 1]) out[n.id + '|' + g + '|' + m] = ix.impact(n.id, g, m);
  }
}
process.stdout.write(JSON.stringify(out));

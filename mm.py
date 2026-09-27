"""Ask questions about a project map from the terminal (the same graph the HTML viewer shows).

  python mm.py --graph examples/vbank/docs/diagrams/vbank.minimap.json --find append
  python mm.py --graph MAP --node fn:vbank.store.Store.append --impact --min-grade static
  python mm.py --graph MAP --coverage   (also --arrows, --hotspots, --flow, --diff, --stale; --help lists all)
Set the environment variable MINIMAP_GRAPH once to leave out --graph.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from minimap.query import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    graph = os.environ.get("MINIMAP_GRAPH", "")
    if "--graph" in args:
        i = args.index("--graph")
        if i + 1 >= len(args):
            sys.exit("--graph needs a path to a .minimap.json file")
        graph = args[i + 1]
        del args[i:i + 2]
    if "-h" in args or "--help" in args:
        sys.exit(main(["MAP"] + args))
    if not graph or not Path(graph).is_file():
        print(f"no map found{' at ' + graph if graph else ''}. Build one with run.py first, then pass "
              "--graph PROJECT/docs/diagrams/NAME.minimap.json (or set MINIMAP_GRAPH). See README, Quick start.",
              file=sys.stderr)
        sys.exit(2)
    if "--diff" in args:  # bare --diff: the graph that run.py kept when it last replaced this one
        i = args.index("--diff")
        if i + 1 == len(args) or args[i + 1].startswith("--"):
            args.insert(i + 1, str(Path(os.environ.get("MINIMAP_STATE_DIR") or HERE) / "prev" / Path(graph).name))
    sys.exit(main([graph] + args))

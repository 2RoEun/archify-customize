"""Static extraction: Python AST → graph nodes and edges (DESIGN.md §3, §5)."""
from __future__ import annotations

import ast
import builtins
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .graph import Graph
from .safeio import read_bytes_retry

PATH_RE = re.compile(r"^[\w./\\-]*\w\.(jsonl|json|md|lock|txt|csv|html|py)$")
WRITE_MARKERS = ('"w"', "'w'", '"a"', "'a'", '"x"', "'x'", '"wb"', "'wb'", '"ab"', "'ab'",
                 "write_text", "write_bytes", ".replace(", ".unlink(", ".write(", "json.dump(")
READ_MARKERS = ("read_text", "read_bytes", "open(", "json.load(", ".read(", "readlines")
# Method names shared with builtin containers, str, Path and files: rule 6 would only produce noise.
COMMON_METHODS = frozenset("""
get items keys values append extend insert pop popitem update setdefault clear copy remove index count sort
join split rsplit strip lstrip rstrip format replace startswith endswith lower upper encode decode find
read write readline readlines close open exists is_file is_dir mkdir unlink rename resolve glob rglob iterdir
read_text write_text read_bytes write_bytes relative_to with_suffix with_name as_posix stat touch
add discard union intersection difference issubset
""".split())
MAX_CANDIDATES = 12
BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass
class Scope:
    node_id: str
    kind: str                      # module | class | function
    module: "ModuleInfo"
    cls: "ClassInfo | None" = None
    local_defs: dict = field(default_factory=dict)     # name -> node id (nested defs)
    local_imports: dict = field(default_factory=dict)  # alias -> ('mod', m) | ('sym', m, name) | ('ext',)
    local_types: dict = field(default_factory=dict)    # local name -> cls id (STEP 1 type tracking)
    local_paths: dict = field(default_factory=dict)    # local name -> set(data ids) (STEP 10)


@dataclass
class ClassInfo:
    node_id: str
    name: str
    module: "ModuleInfo"
    bases: list
    methods: dict = field(default_factory=dict)        # name -> node id
    path_attrs: dict = field(default_factory=dict)     # self.<attr> -> set(data ids)
    attr_types: dict = field(default_factory=dict)     # self.<attr> -> cls id (STEP 1 type tracking)


@dataclass
class ModuleInfo:
    name: str
    rel: str
    kind: str                      # module | test | script
    tree: ast.Module
    lines: list
    node_id: str
    top: dict = field(default_factory=dict)            # name -> node id (def/class)
    classes: dict = field(default_factory=dict)        # name -> ClassInfo
    imports: dict = field(default_factory=dict)        # alias -> ('mod', m) | ('sym', m, name) | ('ext',)
    path_names: dict = field(default_factory=dict)     # module-level NAME -> set(data ids)


class Extractor:
    def __init__(self, root: Path, package: str, extra_dirs: dict | None = None):
        self.root = Path(root)
        self.package = package
        self.extra_dirs = extra_dirs if extra_dirs is not None else {"tests": "test", "scripts": "script"}
        self.g = Graph(self.root.as_posix(), package)
        self.mods: dict[str, ModuleInfo] = {}
        self.methods_by_name: dict[str, list] = {}
        self.stats = {"unresolved_calls": 0, "skipped_common": 0, "skipped_many": 0, "parse_errors": [],
                      "typed_calls": 0, "typed_miss": 0}

    # ------------------------------------------------------------ discovery
    def _files(self):
        pkg_dir = self.root / self.package
        for p in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p, "module"
        for d, kind in self.extra_dirs.items():
            base = self.root / d
            if base.is_dir():
                for p in sorted(base.rglob("*.py")):
                    if "__pycache__" not in p.parts and "fixtures" not in p.relative_to(base).parts:
                        yield p, kind

    def _modname(self, path: Path) -> str:
        rel = path.relative_to(self.root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    # ------------------------------------------------------------ pass 1: definitions
    def run(self) -> Graph:
        for path, kind in self._files():
            rel = path.relative_to(self.root).as_posix()
            data = read_bytes_retry(path)  # one read: the recorded hash is of exactly the bytes parsed
            self.g.sources[rel] = hashlib.sha256(data).hexdigest()  # recorded even if parsing fails
            try:
                text = data.decode("utf-8-sig")
                tree = ast.parse(text, filename=rel)
            except (SyntaxError, ValueError) as exc:  # ValueError: undecodable bytes
                self.stats["parse_errors"].append(f"{rel}: {exc}")
                continue
            name = self._modname(path)
            mid = f"mod:{name}"
            info = ModuleInfo(name, rel, kind, tree, text.splitlines(), mid)
            self.mods[name] = info
            self.g.add_node(mid, kind, name.rsplit(".", 1)[-1], qual=name, file=rel, line=1,
                            end=len(info.lines) or 1)
        for info in self.mods.values():
            self._contain_module(info)
            self._define(info, info.tree.body, Scope(info.node_id, "module", info), prefix=info.name)
        for info in self.mods.values():
            self._module_path_names(info)
        for info in self.mods.values():
            self._imports(info)
        for info in self.mods.values():
            self._walk_scope_bodies(info)
        self.g.stats = {**self.stats, "nodes": len(self.g.nodes), "edges": len(self.g.edges),
                        "dirs": self.extra_dirs}  # lets the stale check rediscover the same file set
        self.g.finalize()
        return self.g

    def _contain_module(self, info: ModuleInfo) -> None:
        if "." not in info.name:
            return
        parent = info.name.rsplit(".", 1)[0]
        if parent not in self.mods:  # directory without __init__.py (tests/, scripts/)
            kind = info.kind
            self.g.add_node(f"mod:{parent}", kind, parent.rsplit(".", 1)[-1], qual=parent)
        self.g.add_edge(f"mod:{parent}", info.node_id, "contains", static="confirmed")

    def _define(self, info: ModuleInfo, body: list, scope: Scope, prefix: str, cls: ClassInfo | None = None):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}.{stmt.name}"
                nid = f"fn:{qual}"
                first = min([d.lineno for d in stmt.decorator_list] + [stmt.lineno])
                self.g.add_node(nid, "function", stmt.name, qual=qual, file=info.rel, line=stmt.lineno,
                                end=stmt.end_lineno, first=first, is_async=isinstance(stmt, ast.AsyncFunctionDef) or None,
                                method=bool(cls) or None, role=None if info.kind == "module" else info.kind)
                self.g.add_edge(scope.node_id, nid, "contains", static="confirmed")
                if scope.kind == "module":
                    info.top[stmt.name] = nid
                elif scope.kind == "class":
                    cls.methods[stmt.name] = nid
                    self.methods_by_name.setdefault(stmt.name, []).append(nid)
                else:
                    scope.local_defs[stmt.name] = nid
                inner = Scope(nid, "function", info, cls=cls if scope.kind == "class" else scope.cls)
                self._define(info, stmt.body, inner, prefix=qual, cls=inner.cls)
                stmt._mm_scope = inner  # remembered for pass 2
            elif isinstance(stmt, ast.ClassDef):
                qual = f"{prefix}.{stmt.name}"
                nid = f"cls:{qual}"
                self.g.add_node(nid, "class", stmt.name, qual=qual, file=info.rel, line=stmt.lineno,
                                end=stmt.end_lineno, role=None if info.kind == "module" else info.kind)
                self.g.add_edge(scope.node_id, nid, "contains", static="confirmed")
                ci = ClassInfo(nid, stmt.name, info, [_dotted(b) for b in stmt.bases])
                if scope.kind == "module":
                    info.top[stmt.name] = nid
                    info.classes[stmt.name] = ci
                elif scope.kind == "function":
                    scope.local_defs[stmt.name] = nid
                cscope = Scope(nid, "class", info, cls=ci)
                self._define(info, stmt.body, cscope, prefix=qual, cls=ci)
                stmt._mm_scope = cscope
            elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                # defs nested in control flow still belong to the enclosing scope
                for sub in _child_bodies(stmt):
                    self._define(info, sub, scope, prefix, cls)
        if scope.kind == "module":
            info._mm_scope = scope

    # ------------------------------------------------------------ module-level path names
    def _module_path_names(self, info: ModuleInfo) -> None:
        for stmt in info.tree.body:
            if isinstance(stmt, ast.Assign):
                ids = self._path_ids(stmt.value)
                if ids:
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            info.path_names[t.id] = ids

    def _path_ids(self, expr) -> set:
        joined, consumed = _path_joins(expr)
        out = set()
        for n in ast.walk(expr):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in consumed:
                did = self._data_id(joined.get(id(n))) or self._data_id(n.value)
                if did:
                    out.add(did)
        return out

    def _data_id(self, value: str | None) -> str | None:
        if not value or not PATH_RE.match(value) or len(value) > 120:
            return None
        lit = re.sub(r"^(?:\./|/)+", "", value.replace("\\", "/")) or value
        did = f"data:{lit}"
        self.g.add_node(did, "data", lit.rsplit("/", 1)[-1], path=lit)
        return did

    def _local_paths(self, scope: Scope, fn) -> None:
        """STEP 10: local names bound exactly once in this function (not nested defs) to an expression naming a file."""
        count: dict[str, int] = {}
        value: dict[str, set] = {}
        params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
        for n in _own_nodes(fn.body):
            targets = n.targets if isinstance(n, ast.Assign) else \
                [n.target] if isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr)) \
                else [i.optional_vars for i in n.items if i.optional_vars is not None] \
                if isinstance(n, (ast.With, ast.AsyncWith)) else []
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        count[x.id] = count.get(x.id, 0) + 1
            if isinstance(n, ast.ExceptHandler) and n.name:
                count[n.name] = count.get(n.name, 0) + 2
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                ids = self._path_ids(n.value)
                if ids:
                    value[n.targets[0].id] = ids
        scope.local_paths = {k: v for k, v in value.items() if count.get(k) == 1 and k not in params}

    # ------------------------------------------------------------ imports
    def _resolve_module(self, info: ModuleInfo, module: str | None, level: int) -> str | None:
        if level:
            base = info.name.split(".")
            is_pkg = info.rel.endswith("__init__.py")
            base = base if is_pkg else base[:-1]
            base = base[: len(base) - (level - 1)] if level > 1 else base
            name = ".".join(base + ([module] if module else []))
        else:
            name = module or ""
            if name not in self.mods and info.kind != "module":
                sibling = ".".join(info.name.split(".")[:-1] + [name])
                if sibling in self.mods:
                    name = sibling
        return name

    def _import_entries(self, info: ModuleInfo, stmt) -> list:
        """Return [(alias, binding, target_node_or_None)] for one import statement."""
        out = []
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                mod = a.name
                if a.asname:
                    binding = ("mod", mod) if mod in self.mods else ("ext",)
                    out.append((a.asname, binding, f"mod:{mod}" if mod in self.mods else None))
                else:
                    top = mod.split(".")[0]
                    binding = ("mod", top) if top in self.mods else ("pkgpath", mod) if mod in self.mods else ("ext",)
                    out.append((top, binding, f"mod:{mod}" if mod in self.mods else None))
        else:
            base = self._resolve_module(info, stmt.module, stmt.level)
            for a in stmt.names:
                alias = a.asname or a.name
                if a.name == "*":
                    out.append((None, None, f"mod:{base}" if base in self.mods else None))
                    continue
                sub = f"{base}.{a.name}" if base else a.name
                if sub in self.mods:
                    out.append((alias, ("mod", sub), f"mod:{sub}"))
                elif base in self.mods:
                    target = self._resolve_symbol(base, a.name)
                    out.append((alias, ("sym", base, a.name), target or f"mod:{base}"))
                else:
                    out.append((alias, ("ext",), None))
        return out

    def _resolve_symbol(self, module: str, name: str, depth: int = 0) -> str | None:
        info = self.mods.get(module)
        if info is None or depth > 5:
            return None
        if name in info.top:
            return info.top[name]
        b = info.imports.get(name)
        if b and b[0] == "sym":
            return self._resolve_symbol(b[1], b[2], depth + 1)
        if b and b[0] == "mod":
            return f"mod:{b[1]}"
        return None

    def _imports(self, info: ModuleInfo) -> None:
        # module-level bindings first (needed for re-export resolution), then edges from each scope
        for stmt in info.tree.body:
            for sub in [stmt] + ([s for b in _child_bodies(stmt) for s in b] if isinstance(stmt, (ast.If, ast.Try)) else []):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias, binding, _ in self._import_entries(info, sub):
                        if alias:
                            info.imports[alias] = binding

    def _import_edges(self, scope: Scope, stmt) -> None:
        info = scope.module
        for alias, binding, target in self._import_entries(info, stmt):
            if scope.kind == "function" and alias:
                scope.local_imports[alias] = binding
            if target:
                self.g.add_edge(scope.node_id, target, "imports", static="confirmed",
                                evidence=f"{info.rel}:{stmt.lineno}")

    # ------------------------------------------------------------ pass 2: bodies
    def _walk_scope_bodies(self, info: ModuleInfo) -> None:
        self._walk_body(info._mm_scope, info.tree.body)

    def _walk_body(self, scope: Scope, body: list) -> None:
        for stmt in body:
            self._walk_stmt(scope, stmt)

    def _walk_stmt(self, scope: Scope, stmt) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in getattr(stmt, "decorator_list", []):
                self._visit_expr(scope, d, stmt)
            inner = getattr(stmt, "_mm_scope", None)
            if inner is None:  # a def inside a construct pass 1 does not descend into (e.g. match)
                self.stats.setdefault("undefined_scopes", []).append(f"{scope.module.rel}:{stmt.lineno}")
                return
            body = stmt.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]  # skip docstring
            if isinstance(stmt, ast.ClassDef):
                self._class_path_attrs(inner.cls, stmt)
                self._class_attr_types(inner, stmt)
            else:
                self._local_types(inner, stmt)
                self._local_paths(inner, stmt)
            self._walk_body(inner, body)
            return
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._import_edges(scope, stmt)
            return
        # direct expressions of this statement (not nested statement bodies)
        for name, value in ast.iter_fields(stmt):
            if isinstance(value, list):
                if value and isinstance(value[0], ast.stmt):
                    self._walk_body(scope, value)
                else:
                    for v in value:
                        if isinstance(v, ast.excepthandler):
                            if v.type:
                                self._visit_expr(scope, v.type, stmt)
                            self._walk_body(scope, v.body)
                        elif isinstance(v, ast.match_case):
                            self._visit_expr(scope, v.pattern, stmt)
                            if v.guard:
                                self._visit_expr(scope, v.guard, stmt)
                            self._walk_body(scope, v.body)
                        elif isinstance(v, ast.AST) and not isinstance(v, ast.stmt):
                            self._visit_expr(scope, v, stmt)
            elif isinstance(value, ast.AST):
                self._visit_expr(scope, value, stmt)

    def _visit_expr(self, scope: Scope, expr, stmt) -> None:
        joined, consumed = _path_joins(expr)
        for n in ast.walk(expr):
            if isinstance(n, ast.Call):
                self._call(scope, n)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                if id(n) not in consumed and not _is_docstring_stmt(stmt):
                    did = self._data_id(joined.get(id(n))) or self._data_id(n.value)
                    if did:
                        self._path_edge(scope, did, stmt, n.lineno)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in scope.local_paths:
                for did in sorted(scope.local_paths[n.id]):
                    self._path_edge(scope, did, stmt, n.lineno)
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self" and scope.cls:
                for did in scope.cls.path_attrs.get(n.attr, ()):
                    if not (isinstance(stmt, ast.Assign) and _assigns_self_attr(stmt, n.attr)):
                        self._path_edge(scope, did, stmt, n.lineno)
            elif isinstance(n, ast.Name) and n.id in scope.module.path_names and scope.kind != "module":
                for did in scope.module.path_names[n.id]:
                    self._path_edge(scope, did, stmt, n.lineno)

    def _class_path_attrs(self, ci: ClassInfo, cls_node: ast.ClassDef) -> None:
        for n in ast.walk(cls_node):
            if isinstance(n, ast.Assign):
                ids = self._path_ids(n.value)
                if not ids:
                    continue
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        ci.path_attrs.setdefault(t.attr, set()).update(ids)

    # ------------------------------------------------------------ STEP 1: type tracking (certain cases only)
    def _class_of_expr(self, scope: Scope, expr) -> str | None:
        """cls id for a class reference: Name, dotted module path, or a string annotation."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str) and expr.value.isidentifier():
            expr = ast.Name(expr.value)
        if isinstance(expr, ast.Name):
            t = self._resolve_name(scope, expr.id)
            return t if t and t.startswith("cls:") else None
        dotted = _dotted(expr)
        if not dotted or "." not in dotted:
            return None
        parts = dotted.split(".")
        b = scope.local_imports.get(parts[0]) or scope.module.imports.get(parts[0])
        if not b or b[0] not in ("mod", "pkgpath"):
            return None
        module = b[1] if b[0] == "mod" else parts[0]
        for p in parts[1:-1]:
            if f"{module}.{p}" not in self.mods:
                return None
            module = f"{module}.{p}"
        t = self._resolve_symbol(module, parts[-1])
        return t if t and t.startswith("cls:") else None

    def _instance_class(self, scope: Scope, value) -> str | None:
        return self._class_of_expr(scope, value.func) if isinstance(value, ast.Call) else None

    def _enter_returns_self(self, cls_id: str) -> bool:
        target = self._method_in_class(self._class_info(cls_id), "__enter__")
        if not target:
            return False
        node = self.g.nodes[target]
        info = self.mods.get(node["qual"].rsplit(".", 2)[0])
        if info is None:
            return False
        for n in ast.walk(info.tree):
            if isinstance(n, ast.FunctionDef) and n.name == "__enter__" and n.lineno == node["line"]:
                rets = [r for r in ast.walk(n) if isinstance(r, ast.Return)]
                return bool(rets) and all(isinstance(r.value, ast.Name) and r.value.id == "self" for r in rets)
        return False

    def _local_types(self, scope: Scope, fn) -> None:
        """Names whose every binding in this function (not nested defs) is the same analysed class."""
        seen: dict[str, set] = {}
        bad: set = set()

        def note(name, cls_id):
            if cls_id:
                seen.setdefault(name, set()).add(cls_id)
            else:
                bad.add(name)

        for s in _own_statements(fn.body):
            if isinstance(s, (ast.Import, ast.ImportFrom)):  # local imports are needed to resolve class names
                for alias, binding, _ in self._import_entries(scope.module, s):
                    if alias:
                        scope.local_imports[alias] = binding
        args = fn.args
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            if a.annotation is not None:
                note(a.arg, self._class_of_expr(scope, a.annotation))
        for n in _own_nodes(fn.body):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        note(t.id, self._instance_class(scope, n.value))
                    else:
                        for x in ast.walk(t):
                            if isinstance(x, ast.Name):
                                bad.add(x.id)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                note(n.target.id, self._class_of_expr(scope, n.annotation) or self._instance_class(scope, n.value))
            elif isinstance(n, (ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
                tgt = n.target
                for x in ast.walk(tgt):
                    if isinstance(x, ast.Name):
                        bad.add(x.id)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if isinstance(item.optional_vars, ast.Name):
                        c = self._instance_class(scope, item.context_expr)
                        note(item.optional_vars.id, c if c and self._enter_returns_self(c) else None)
            elif isinstance(n, (ast.ExceptHandler,)) and n.name:
                bad.add(n.name)
        scope.local_types = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and k not in bad}

    def _class_attr_types(self, cscope: Scope, cls_node: ast.ClassDef) -> None:
        """self.<attr> whose every assignment in the class's methods creates the same analysed class."""
        seen: dict[str, set] = {}
        bad: set = set()
        for m in cls_node.body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            mscope = getattr(m, "_mm_scope", None) or cscope
            params = {a.arg: self._class_of_expr(mscope, a.annotation)
                      for a in m.args.args + m.args.kwonlyargs if a.annotation is not None}
            for n in _own_nodes(m.body):
                targets, value, ann = [], None, None
                if isinstance(n, ast.Assign):
                    targets, value = n.targets, n.value
                elif isinstance(n, ast.AnnAssign):
                    targets, value, ann = [n.target], n.value, n.annotation
                for t in targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        c = (self._class_of_expr(mscope, ann) if ann is not None else None) or \
                            self._instance_class(mscope, value) or \
                            (params.get(value.id) if isinstance(value, ast.Name) else None)
                        if c:
                            seen.setdefault(t.attr, set()).add(c)
                        else:
                            bad.add(t.attr)
        cscope.cls.attr_types = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and k not in bad}

    def _path_edge(self, scope: Scope, did: str, stmt, lineno: int) -> None:
        info = scope.module
        text = " ".join(info.lines[stmt.lineno - 1: (stmt.end_lineno or stmt.lineno)])
        if isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.While, ast.If, ast.Try)):
            text = info.lines[lineno - 1]
        kind = "writes" if any(m in text for m in WRITE_MARKERS) else \
            "reads" if any(m in text for m in READ_MARKERS) else "uses_path"
        self.g.add_edge(scope.node_id, did, kind, static="guess", evidence=f"{info.rel}:{lineno}")

    # ------------------------------------------------------------ calls
    def _call(self, scope: Scope, call: ast.Call) -> None:
        info = scope.module
        ev = f"{info.rel}:{call.lineno}"
        func = call.func
        if isinstance(func, ast.Name):
            target = self._resolve_name(scope, func.id)
            if target:
                self.g.add_edge(scope.node_id, target, "calls", static="confirmed", evidence=ev)
            return
        if not isinstance(func, ast.Attribute):
            return
        attr = func.attr
        base = func.value
        dotted = _dotted(base)
        if dotted in ("self", "cls") and scope.cls:
            target = self._method_in_class(scope.cls, attr)
            if target:
                self.g.add_edge(scope.node_id, target, "calls", static="confirmed", evidence=ev)
                return
            self._guess(scope, attr, ev)
            return
        typed = None  # STEP 1: a variable whose class is known for certain
        if dotted and "." not in dotted and dotted in scope.local_types:
            typed = scope.local_types[dotted]
        elif dotted and dotted.count(".") == 1 and dotted.startswith("self.") and scope.cls:
            typed = scope.cls.attr_types.get(dotted[5:])
        if typed:
            target = self._method_in_class(self._class_info(typed), attr)
            if target:
                self.stats["typed_calls"] += 1
                self.g.add_edge(scope.node_id, target, "calls", static="confirmed", evidence=ev)
            else:
                self.stats["typed_miss"] += 1  # method from a non-analysed base (e.g. unittest): not guessed
            return
        if dotted:
            head = dotted.split(".")[0]
            binding = scope.local_imports.get(head) or info.imports.get(head)
            if binding:
                if binding[0] == "ext":
                    return
                target = self._resolve_dotted(binding, dotted, attr)
                if target:
                    self.g.add_edge(scope.node_id, target, "calls", static="confirmed", evidence=ev)
                return  # a known module whose attribute is not an analysed def: not recorded
            cls_target = self._resolve_name(scope, dotted) if "." not in dotted else None
            if cls_target and cls_target.startswith("cls:"):
                ci = self._class_info(cls_target)
                target = self._method_in_class(ci, attr) if ci else None
                if target:
                    self.g.add_edge(scope.node_id, target, "calls", static="confirmed", evidence=ev)
                return
            if head in BUILTIN_NAMES and head not in info.top:
                return
        self._guess(scope, attr, ev)

    def _guess(self, scope: Scope, attr: str, ev: str) -> None:
        if attr in COMMON_METHODS:
            self.stats["skipped_common"] += 1
            return
        cands = self.methods_by_name.get(attr, [])
        if not cands:
            self.stats["unresolved_calls"] += 1
            return
        if len(cands) > MAX_CANDIDATES:
            self.stats["skipped_many"] += 1
            return
        for c in cands:
            self.g.add_edge(scope.node_id, c, "calls", static="guess", evidence=ev, candidates=cands)

    def _resolve_name(self, scope: Scope, name: str) -> str | None:
        if name in scope.local_defs:
            return scope.local_defs[name]
        b = scope.local_imports.get(name)
        info = scope.module
        if b is None and name in info.top:
            return info.top[name]
        b = b or info.imports.get(name)
        if b and b[0] == "sym":
            t = self._resolve_symbol(b[1], b[2])
            return t if t and not t.startswith("mod:") else None
        return None

    def _resolve_dotted(self, binding, dotted: str, attr: str) -> str | None:
        parts = dotted.split(".")
        if binding[0] == "mod":
            module = binding[1]
        elif binding[0] == "pkgpath":
            module = parts[0]
        elif binding[0] == "sym":
            t = self._resolve_symbol(binding[1], binding[2])
            if t and t.startswith("cls:"):
                ci = self._class_info(t)
                return self._method_in_class(ci, attr) if ci and len(parts) == 1 else None
            if t and t.startswith("mod:"):
                module = t[4:]
            else:
                return None
        else:
            return None
        for p in parts[1:]:
            nxt = f"{module}.{p}"
            if nxt in self.mods:
                module = nxt
            else:
                ci = self.mods[module].classes.get(p) if module in self.mods else None
                return self._method_in_class(ci, attr) if ci else None
        t = self._resolve_symbol(module, attr)
        return t if t and not t.startswith("mod:") else None

    def _class_info(self, cls_id: str) -> ClassInfo | None:
        qual = cls_id[4:]
        module, _, name = qual.rpartition(".")
        info = self.mods.get(module)
        return info.classes.get(name) if info else None

    def _method_in_class(self, ci: ClassInfo | None, attr: str, seen=None) -> str | None:
        if ci is None:
            return None
        seen = seen or set()
        if ci.node_id in seen:
            return None
        seen.add(ci.node_id)
        if attr in ci.methods:
            return ci.methods[attr]
        for b in ci.bases:
            if not b:
                continue
            scope = ci.module._mm_scope
            t = self._resolve_name(scope, b) if "." not in b else None
            if t and t.startswith("cls:"):
                found = self._method_in_class(self._class_info(t), attr, seen)
                if found:
                    return found
        return None


# ------------------------------------------------------------ helpers
def _dotted(expr) -> str | None:
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


def _child_bodies(stmt) -> list:
    out = []
    for name in ("body", "orelse", "finalbody"):
        b = getattr(stmt, name, None)
        if isinstance(b, list):
            out.append(b)
    for h in getattr(stmt, "handlers", []) or []:
        out.append(h.body)
    return out


def _path_joins(expr) -> tuple[dict, set]:
    """STEP 10: `root / "data" / "x.jsonl"` and `os.path.join(root, "data", "x.jsonl")` name one file: the trailing
    string parts join into one path. Returns ({id(last part): joined path}, {id(the other parts)})."""
    joined, consumed = {}, set()
    for n in ast.walk(expr):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            parts, cur = [], n
            while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
                parts.append(cur.right)
                cur = cur.left
            parts.append(cur)
            parts.reverse()
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "join" \
                and len(n.args) >= 2 and not n.keywords:
            parts = list(n.args)
        else:
            continue
        tail = []
        for p in reversed(parts):
            if not (isinstance(p, ast.Constant) and isinstance(p.value, str)):
                break
            tail.insert(0, p)
        if len(tail) < 2 or id(tail[-1]) in joined or id(tail[-1]) in consumed:
            continue  # an inner part of a longer chain that was already joined
        text = "/".join(s for s in (t.value.replace("\\", "/").strip("/") for t in tail) if s)
        joined[id(tail[-1])] = text
        consumed.update(id(t) for t in tail[:-1])
    return joined, consumed


def _own_statements(body: list):
    """Statements of a function body including nested blocks, but not nested defs/classes."""
    for s in body:
        yield s
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for b in _child_bodies(s):
            yield from _own_statements(b)
        for c in getattr(s, "cases", []) or []:
            yield from _own_statements(c.body)


def _own_nodes(body: list):
    """Every AST node of a function body, not descending into nested defs, classes or lambdas."""
    stack = list(body)
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(c)


def _is_docstring_stmt(stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) \
        and isinstance(stmt.value.value, str)


def _assigns_self_attr(stmt: ast.Assign, attr: str) -> bool:
    return any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
               and t.attr == attr for t in stmt.targets)


def extract(root, package: str, extra_dirs: dict | None = None) -> Graph:
    return Extractor(Path(root), package, extra_dirs).run()

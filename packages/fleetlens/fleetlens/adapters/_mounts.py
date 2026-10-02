"""Where a router is mounted, and therefore what its routes' paths really are.

A blueprint is declared in one file and mounted in another, usually several others:

    app/modules/search/routes/v1/test.py       test = Blueprint("search_v1")
                                               @test.route("/auto-complete")
    app/modules/search/routes/v1/__init__.py   Blueprint.group(test, url_prefix="/test")
    app/modules/search/routes/__init__.py      Blueprint.group(v1, url_prefix="/v1")
    app/modules/search/__init__.py             Blueprint.group(s, url_prefix="/search")

Reading one file at a time sees `/auto-complete` and reports that as the endpoint. The
service serves `/search/v1/test/auto-complete`.

That is worse than a wrong string, because the prefix is frequently the only thing telling
two endpoints apart. `/content/v1/test/{id}/dynamic` and `/content/v2/test/{id}/dynamic`
both reduce to `/{id}/dynamic`, and since an interface id is derived from its path, the two
collapse into one row and the v1/v2 split vanishes from the index. The same happens to a
public and an internal route that share a handler name. Cross-repo resolution then matches
callers against paths no service actually serves.

The three frameworks spell mounting differently and mean nearly the same thing:

    Sanic     Blueprint.group(child, ..., url_prefix="/p")
    Flask     app.register_blueprint(child, url_prefix="/p")
    FastAPI   app.include_router(child, prefix="/p")

so all three resolve through one graph. Nodes are router variables keyed by the module they
are declared in, edges are mountings, and a node's prefix is what accumulates from a root
down to it. A router mounted in two places has two prefixes and genuinely serves two paths,
so both are kept.

Not covered: a mount built at runtime (a loop over a registry, a prefix read from config),
and Starlette's `Mount`, which nests applications rather than routers. Those leave the
router unresolved, and an unresolved router keeps the module-local behaviour it had before
this existed rather than guessing.
"""
from __future__ import annotations

import ast
from pathlib import Path

from ._pysrc import parse
from ._walk import iter_files

#: Cheap substring gate. Parsing every file in a repository to find the few that mount
#: routers is most of the cost of this pass, and a file that mounts one necessarily names
#: the thing it is mounting.
_MARKERS = ("Blueprint", "APIRouter", "include_router", "register_blueprint")

#: Mounting calls, by the attribute they are called as. `group` is the risky one: `.group`
#: is also how you read a regex match, so it counts only as `Blueprint.group(...)`, which
#: is the Sanic spelling and is checked against the receiver below.
_MOUNT_ATTRS = {"group", "register_blueprint", "include_router"}

#: How deep a mounting chain may be followed. Guards against a cycle that `visited` cannot
#: catch and against pathological nesting; real chains are three or four deep.
_MAX_DEPTH = 24
#: How many distinct prefixes one router may resolve to. A router mounted a handful of
#: times is ordinary; hundreds means the graph has been misread, and emitting an interface
#: for each would bury the real ones.
_MAX_PREFIXES = 8


def own_prefix(call: ast.Call) -> str:
    """The prefix a router declares for itself, from its own constructor call.

    Covers `APIRouter(prefix="/p")`, `Blueprint(name, url_prefix="/p")` and Sanic's
    `Blueprint(name, version=4)`, which contributes a `/v4` segment rather than a literal
    prefix. A prefix written without a leading slash still denotes one.
    """
    prefix, version = "", ""
    for kw in call.keywords:
        if not isinstance(kw.value, ast.Constant) or kw.value.value is None:
            continue
        if kw.arg in ("prefix", "url_prefix"):
            prefix = str(kw.value.value).strip("/")
        elif kw.arg == "version":
            v = str(kw.value.value).strip("/")
            version = v if v.startswith("v") else f"v{v}"
    parts = [p for p in (version, prefix) if p]
    return "/" + "/".join(parts) if parts else ""


def _module_of(path: Path, repo: Path) -> tuple:
    """(dotted module name, whether it is a package) for a file in the repo."""
    parts = list(path.relative_to(repo).with_suffix("").parts)
    is_pkg = parts and parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), bool(is_pkg)


def _relative_base(module: str, is_pkg: bool, level: int) -> str:
    """The package a `from ..x import y` is relative to.

    One dot means the module's own package, which for `__init__.py` is itself and for any
    other file is its parent; each further dot goes up one more.
    """
    parts = module.split(".") if module else []
    if not is_pkg:
        parts = parts[:-1]
    up = level - 1
    return ".".join(parts[:len(parts) - up]) if 0 <= up <= len(parts) else ""


def _module_level(tree: ast.Module):
    """Every node outside a function body.

    `ast.walk` descends into handlers, where a short name like `eta`, `order` or `cart` is
    frequently reused as a local. Reading those as router declarations let the last one win,
    which erased the real router's prefix. Conditionals, loops and `try` at module level are
    still descended into, since a router may well be declared inside one.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                             ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


class MountGraph:
    """Resolved URL prefixes for every router variable in a repository.

    Built once per repository; `prefixes_for(module, var)` is then a dict lookup.
    """

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._own: dict = {}          # "module.var" -> prefix declared on the router
        self._flask: set = set()      # keys declared in a module that imports flask
        self._parents: dict = {}      # child key -> [(parent key, prefix from the mount)]
        self._alias: dict = {}        # re-exported name -> where it was declared
        self._modules: set = set()
        self._memo: dict = {}
        self._build()

    # -- construction ---------------------------------------------------------------

    def _build(self) -> None:
        for path in iter_files(self.repo, (".py",)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # A package __init__ is read whichever way it looks, because a pure re-export
            # names neither a framework nor a router type -- `from .routes import bp` is
            # the whole file -- and skipping it breaks the chain from a mount to the
            # module that declared what is being mounted.
            if not (any(m in text for m in _MARKERS) or path.name == "__init__.py"):
                continue
            tree = parse(text, path)
            if tree is None:
                continue
            module, is_pkg = _module_of(path, self.repo)
            self._modules.add(module)
            self._scan(tree, module, is_pkg)
        self._follow_aliases()

    def _follow_aliases(self) -> None:
        """Re-key the graph onto declarations, now that every module has been read.

        Edges are recorded under the name used at the mount site, which for a re-exported
        router is an alias. Resolving during the scan is not possible because the module
        holding the declaration may not have been read yet, so it happens once at the end.
        """
        def declared(key: str) -> str:
            seen: set = set()
            while key in self._alias and key not in seen and len(seen) < _MAX_DEPTH:
                seen.add(key)
                key = self._alias[key]
            return key

        parents: dict = {}
        for child, edges in self._parents.items():
            parents.setdefault(declared(child), []).extend(
                (declared(parent), edge) for parent, edge in edges)
        self._parents = parents
        own: dict = {}
        for key, prefix in self._own.items():
            k = declared(key)
            if prefix or k not in own:
                own[k] = prefix
        self._own = own
        self._flask = {declared(k) for k in self._flask}

    def _scan(self, tree: ast.Module, module: str, is_pkg: bool) -> None:
        imports = self._imports(tree, module, is_pkg)
        flask = self._imports_flask(tree)

        def key_of(node) -> str:
            """The graph key for a router referred to by name in this module."""
            if not isinstance(node, ast.Name):
                return ""
            return imports.get(node.id) or f"{module}.{node.id}"

        # Declarations come from module level only. A router is declared once where the
        # module can see it; a name reused inside a function is a different variable that
        # happens to be spelled the same, and `eta = random.randrange(...)` in a handler
        # was overwriting the `eta` blueprint's prefix with nothing.
        handled, local = set(), set()
        for node in _module_level(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)):
                continue
            key = f"{module}.{node.targets[0].id}"
            prefix = own_prefix(node.value)
            # Belt and braces for a name legitimately rebound at module level: a
            # declaration that carries a prefix is the one worth keeping.
            if prefix or key not in self._own:
                self._own[key] = prefix
            if flask:
                self._flask.add(key)
            self._link(node.value, key, key_of)
            handled.add(id(node.value))
            local.add(node.targets[0].id)

        # A name a module imports and does not declare is that module's alias for the one
        # it came from. Packages re-export routers through several `__init__.py` layers, so
        # a mount names `pkg.router` while the declaration lives at `pkg.sub.mod.router`.
        for name, target in imports.items():
            if name not in local:
                self._alias[f"{module}.{name}"] = target

        # Mountings are read from everywhere, because an application factory that wires the
        # routers together inside a function is an ordinary way to build one.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and id(node) not in handled:
                self._link(node, None, key_of)

    def _link(self, call: ast.Call, as_key, key_of) -> None:
        """Record the mounting `call` performs, if it performs one.

        `as_key` is set when the call is also a declaration, as Sanic's `Blueprint.group`
        is: the group is itself a router, and its own prefix applies to what it holds. A
        bare `app.register_blueprint(...)` declares nothing, so the prefix travels on the
        edge instead.
        """
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in _MOUNT_ATTRS:
            return
        if call.func.attr == "group":
            # `Blueprint.group(...)`, not `match.group(1)`.
            recv = call.func.value
            if not (isinstance(recv, ast.Name) and recv.id.endswith("Blueprint")):
                return
            parent, edge = as_key, ""
        else:
            parent = key_of(call.func.value)
            edge = own_prefix(call)
        if not parent:
            return
        for arg in call.args:
            child = key_of(arg)
            if child and child != parent:
                self._parents.setdefault(child, []).append((parent, edge))

    def _imports(self, tree: ast.Module, module: str, is_pkg: bool) -> dict:
        """local name -> "module.name", for names imported from inside this repository."""
        out = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = _relative_base(module, is_pkg, node.level)
                target = f"{base}.{node.module}" if node.module else base
            elif node.module:
                target = node.module
            else:
                continue
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{target}.{a.name}"
        return out

    @staticmethod
    def _imports_flask(tree: ast.Module) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "flask":
                    return True
            elif isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == "flask" for a in node.names):
                    return True
        return False

    # -- resolution -----------------------------------------------------------------

    def _canonical(self, key: str) -> str:
        """`key` as this graph stores it, allowing for a differently rooted import path.

        A repo whose packages sit under `src/` derives module names with that prefix while
        its own absolute imports leave it out. Matching on the tail recovers the link when
        exactly one module can be meant; an ambiguous tail is left unresolved rather than
        guessed at.
        """
        if key in self._own or key in self._parents:
            return key
        suffix = "." + key
        hits = [k for k in self._own if k.endswith(suffix)]
        return hits[0] if len(hits) == 1 else key

    def prefixes_for(self, module: str, var: str) -> list:
        """Every URL prefix the router `var` of `module` is mounted under.

        Empty when the router is unknown to the graph, which the caller reads as "fall
        back to what this module alone says" rather than as "no prefix".
        """
        return self._resolve(self._canonical(f"{module}.{var}"), set(), 0)

    def _resolve(self, key: str, seen: set, depth: int) -> list:
        if key in self._memo:
            return self._memo[key]
        if key in seen or depth > _MAX_DEPTH:
            return []                      # a cycle contributes no path of its own
        if key not in self._own and key not in self._parents:
            return []
        own = self._own.get(key, "")
        parents = self._parents.get(key)
        if not parents:
            out = [own]
        else:
            out = []
            for parent, edge in parents:
                for base in self._resolve(self._canonical(parent), seen | {key}, depth + 1):
                    # Flask replaces a blueprint's own prefix when the registration gives
                    # one; Sanic and FastAPI add to it. Both are the documented behaviour
                    # of the framework, and services rely on each.
                    tail = "" if (edge and key in self._flask) else own
                    full = f"{base}{edge}{tail}"
                    if full not in out:
                        out.append(full)
            out = out[:_MAX_PREFIXES] or [own]
        if depth == 0:
            self._memo[key] = out
        return out

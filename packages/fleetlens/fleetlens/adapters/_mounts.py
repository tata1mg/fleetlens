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


class MountGraph:
    """Resolved URL prefixes for every router variable in a repository.

    Built once per repository; `prefixes_for(module, var)` is then a dict lookup.
    """

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._own: dict = {}          # "module.var" -> prefix declared on the router
        self._flask: set = set()      # keys declared in a module that imports flask
        self._parents: dict = {}      # child key -> [(parent key, prefix from the mount)]
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
            if not any(m in text for m in _MARKERS):
                continue
            tree = parse(text, path)
            if tree is None:
                continue
            module, is_pkg = _module_of(path, self.repo)
            self._modules.add(module)
            self._scan(tree, module, is_pkg)

    def _scan(self, tree: ast.Module, module: str, is_pkg: bool) -> None:
        imports = self._imports(tree, module, is_pkg)
        flask = self._imports_flask(tree)

        def key_of(node) -> str:
            """The graph key for a router referred to by name in this module."""
            if not isinstance(node, ast.Name):
                return ""
            return imports.get(node.id) or f"{module}.{node.id}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call):
                key = f"{module}.{node.targets[0].id}"
                self._own[key] = own_prefix(node.value)
                if flask:
                    self._flask.add(key)
                self._link(node.value, key, key_of)
            elif isinstance(node, ast.Call):
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

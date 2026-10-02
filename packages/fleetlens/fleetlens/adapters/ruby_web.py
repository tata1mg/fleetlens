"""Rails route discovery.

Rails declares its HTTP surface in a routing DSL rather than in decorators, so this adapter
interprets that DSL rather than anchoring on a per-handler annotation:

    namespace :api do
      namespace :v1 do
        resources :orders, only: [:index, :show] do
          member { get 'status' }
        end
      end
    end

`namespace` and `scope` contribute path prefixes, `resources` expands into the standard
RESTful routes (filtered by `only:` / `except:`), and `member` / `collection` blocks nest
under the resource with and without its `:id` segment.

Routes are commonly split across several files that the main one pulls in with `extend`, so
every `.rb` file under `config/` is read, not just `config/routes.rb`. In one real Rails app
`config/routes.rb` declared 5 routes and `config/routes/*.rb` declared 613.

Paths whose value is computed rather than written are recorded as skipped sites, never
guessed. Rails engines mounted with `mount` are not followed (recorded instead).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import _ruby as R
from .base import Interface, InterfaceAdapter, SkippedSite, snippet_of

_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_PREFIXERS = {"namespace", "scope"}

# `resources :things` generates these; `resource :thing` (singular) drops index and :id.
_PLURAL_ACTIONS = {
    "index":   [("GET", "")],
    "create":  [("POST", "")],
    "new":     [("GET", "/new")],
    "edit":    [("GET", "/:id/edit")],
    "show":    [("GET", "/:id")],
    "update":  [("PUT", "/:id"), ("PATCH", "/:id")],
    "destroy": [("DELETE", "/:id")],
}
_SINGULAR_ACTIONS = {
    "create":  [("POST", "")],
    "new":     [("GET", "/new")],
    "edit":    [("GET", "/edit")],
    "show":    [("GET", "")],
    "update":  [("PUT", ""), ("PATCH", "")],
    "destroy": [("DELETE", "")],
}


def _singular(name: str) -> str:
    """`orders` -> `order`, for the `:<parent>_id` segment Rails nests children under.

    Rails singularises with ActiveSupport's inflections, which carry a table of irregular
    forms this cannot reproduce. The common English endings are handled and anything else
    falls back to dropping a trailing `s`; an imperfect parameter name still yields the
    right path shape, which is what distinguishes one endpoint from another.
    """
    for suffix, replacement in (("ies", "y"), ("ses", "s"), ("xes", "x"),
                                ("zes", "z"), ("ches", "ch"), ("shes", "sh")):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)] + replacement
    return name[:-1] if name.endswith("s") and not name.endswith("ss") else name


def _join(prefix: list, path: str) -> str:
    segs = [s.strip("/") for s in prefix if s and s.strip("/")]
    tail = (path or "").strip()
    if tail and not tail.startswith("/"):
        tail = "/" + tail
    joined = "/" + "/".join(segs) + tail if segs else (tail or "/")
    return joined.replace("//", "/") or "/"


def _route_files(repo: Path):
    """Only files that actually declare routes.

    Scanning all of `config/` picks up false routes: an initializer's CORS block uses
    `resource '*'` and zeitwerk config calls `delete`, neither of which is a route. Rails
    convention is `config/routes.rb` plus `config/routes/`, so use that.
    """
    cfg = repo / "config"
    if not cfg.is_dir():
        return
    main = cfg / "routes.rb"
    if main.exists():
        yield main
    routes_dir = cfg / "routes"
    if routes_dir.is_dir():
        for p in sorted(routes_dir.rglob("*.rb")):
            yield p


def _draw_blocks(tree, src: bytes):
    """The route table(s) in a file.

    `Rails.application.routes.draw do ... end` in the main file, and the
    `router.instance_eval do ... end` that route modules use to inject into it. Anything
    outside those is not routing, even in a routes file.
    """
    blocks = []
    for call in R.walk_calls(tree.root_node):
        if R.call_name(call, src) in ("draw", "instance_eval"):
            blk = R.block_of(call)
            if blk is not None:
                blocks.append(blk)
    return blocks


class _Walker:
    def __init__(self, src: bytes, rel: str, out: list, skipped: Optional[list]):
        self.src, self.rel, self.out, self.skipped = src, rel, out, skipped
        self.lines = src.decode("utf-8", "replace").splitlines()

    def emit(self, method: str, path: str, node, handler: Optional[str] = None):
        self.out.append(Interface(
            method=method, path=path, type="rest", handler=handler,
            framework="rails", evidence=[f"{self.rel}:{node.start_point[0] + 1}"]))

    def record_skip(self, node, reason: str, expr: str):
        if self.skipped is None:
            return
        line = node.start_point[0] + 1
        self.skipped.append(SkippedSite(
            kind="interface", reason=reason, file=self.rel, line=line, expr=expr,
            snippet=snippet_of(self.lines, line, node.end_point[0] + 1),
            names=[], method=None))

    def visit(self, node, prefix: list, resource: Optional[tuple] = None):
        """resource is (path_prefix_list, is_singular) for member/collection nesting."""
        for child in node.children:
            if child.type not in ("call", "method_call", "command", "command_call"):
                self.visit(child, prefix, resource)
                continue
            name = R.call_name(child, self.src)
            block = R.block_of(child)

            if name in _PREFIXERS:
                seg = R.first_literal(child, self.src)
                if seg is None:
                    kw = R.kwargs(child, self.src)
                    # `scope module: 'x'` adds no path segment; that is not a gap
                    if not kw or "path" in kw:
                        self.record_skip(child, "non-literal-path",
                                         R.text(child, self.src)[:60])
                if block is not None:
                    self.visit(block, prefix + ([seg] if seg else []), resource)
                continue

            if name in ("resources", "resource"):
                self._resources(child, prefix, singular=(name == "resource"))
                continue

            if name in ("member", "collection") and resource is not None:
                base, _singular = resource
                nested = base + ([":id"] if name == "member" else [])
                if block is not None:
                    self.visit(block, nested, resource)
                continue

            if name in _VERBS:
                self._verb(child, prefix, name)
                continue

            if name == "root":
                kw = R.kwargs(child, self.src)
                self.emit("GET", _join(prefix, "/"), child, kw.get("to", "").strip("'\""))
                continue

            if name == "mount":
                # a mounted engine brings its own route table, which we do not follow
                self.record_skip(child, "mounted-engine", R.text(child, self.src)[:60])
                continue

            if block is not None:
                self.visit(block, prefix, resource)
            else:
                self.visit(child, prefix, resource)

    def _verb(self, node, prefix: list, verb: str):
        kw = R.kwargs(node, self.src)
        path = R.first_literal(node, self.src)
        if path is None:
            self.record_skip(node, "non-literal-path", R.text(node, self.src)[:60])
            return
        handler = (kw.get("to") or kw.get("action") or "").strip("'\":")
        self.emit(verb.upper(), _join(prefix, path), node, handler or None)

    def _resources(self, node, prefix: list, singular: bool):
        name = R.first_literal(node, self.src)
        if name is None:
            self.record_skip(node, "non-literal-resource", R.text(node, self.src)[:60])
            return
        kw = R.kwargs(node, self.src)
        table = _SINGULAR_ACTIONS if singular else _PLURAL_ACTIONS
        actions = set(table)
        if "only" in kw:
            actions &= set(R.symbol_list(kw["only"]))
        if "except" in kw:
            actions -= set(R.symbol_list(kw["except"]))
        base = prefix + [name]
        for action in sorted(actions):
            for method, suffix in table[action]:
                self.emit(method, _join(base, suffix), node, f"{name}#{action}")
        block = R.block_of(node)
        if block is not None:
            # Everything inside the block is nested under one member of the resource, so it
            # carries that member's id: `resources :orders do resources :items end` serves
            # /orders/:order_id/items, not /orders/items. A singular `resource` has no id to
            # nest under. `member` and `collection` are computed from the bare base instead,
            # which is why `resource` is passed unchanged.
            nested = base if singular else base + [f":{_singular(name)}_id"]
            self.visit(block, nested, (base, singular))


def _controller_actions(repo: Path) -> dict:
    """{("orders", "index"): "app/controllers/orders_controller.rb:12"}.

    Rails maps a route's `to: 'orders#index'` onto `OrdersController#index` by convention, so
    the handler's location is derivable without a call graph. Indexing endpoint to code for
    Ruby therefore does not wait on scip-ruby.
    """
    out: dict = {}
    root = repo / "app" / "controllers"
    if not root.is_dir():
        return out
    for f in sorted(root.rglob("*_controller.rb")):
        rel_path = f.relative_to(root).as_posix()
        name = rel_path[:-len("_controller.rb")]          # api/v1/orders
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        evidence_path = f.relative_to(repo).as_posix()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("def "):
                action = stripped[4:].split("(")[0].strip().rstrip(";")
                # index by full path and by bare name, since routes may write either
                out.setdefault((name, action), f"{evidence_path}:{i}")
                out.setdefault((name.rsplit("/", 1)[-1], action), f"{evidence_path}:{i}")
    return out


def _attach_handlers(repo: Path, interfaces: list) -> None:
    """Point each route's evidence at its controller action as well as its declaration.

    The interface loader anchors `handled_by` from evidence, so adding the controller
    location is what makes an endpoint traceable into code.
    """
    actions = _controller_actions(repo)
    if not actions:
        return
    for iface in interfaces:
        h = (iface.handler or "").strip()
        if "#" not in h:
            continue
        controller, _, action = h.partition("#")
        loc = actions.get((controller.strip("/"), action)) or \
            actions.get((controller.strip("/").rsplit("/", 1)[-1], action))
        if loc:
            iface.evidence.append(loc)


class RailsRouteAdapter(InterfaceAdapter):
    name = "rails"

    def applies(self, repo: Path) -> bool:
        repo = Path(repo)
        if (repo / "config" / "routes.rb").exists():
            return True
        gemfile = repo / "Gemfile"
        try:
            return "rails" in gemfile.read_text(errors="replace")
        except OSError:
            return False

    def discover(self, repo: Path, skipped: Optional[list] = None) -> list:
        repo = Path(repo)
        out: list = []
        for path in _route_files(repo):
            tree, src = R.parse(path)
            if tree is None:
                continue
            rel = path.relative_to(repo).as_posix()
            walker = _Walker(src, rel, out, skipped)
            blocks = _draw_blocks(tree, src)
            # a module of plain route calls with no draw/instance_eval wrapper still counts
            for node in (blocks or [tree.root_node]):
                walker.visit(node, [])
        # one path may be declared in several files; keep the first occurrence
        seen, uniq = set(), []
        for i in out:
            key = (i.method, i.path)
            if key not in seen:
                seen.add(key)
                uniq.append(i)
        _attach_handlers(repo, uniq)
        return uniq

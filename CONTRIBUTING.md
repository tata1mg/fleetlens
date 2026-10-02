# Contributing to fleetlens

Thanks for looking. The most useful contributions, in order:

1. **A framework adapter.** Teach fleetlens to find routes or queues in a framework it
   doesn't know yet. Self-contained, no core changes.
2. **A repo shape that confused a parser.** An issue with the code pattern that got missed
   is worth more than a bug report without one.
3. **Benchmark corpora.** A public multi-repo system with a hand-labelled service graph.

## Setup

```bash
git clone https://github.com/tata1mg/fleetlens.git
cd fleetlens
python -m venv .venv && source .venv/bin/activate
pip install -e "packages/fleetlens[all]"
npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

```bash
cd packages/fleetlens && python -m pytest -q
```

Tests are fast (well under a second) and hermetic. They build tiny repos in `tmp_path`
rather than reaching for real ones. Keep it that way.

## Writing a framework adapter

An adapter answers two questions about a repo: *does this framework appear here*, and *what
interfaces does it expose*. It never touches the store, the resolver, or the MCP layer.

```python
# packages/fleetlens/fleetlens/adapters/my_framework.py
from .base import Interface, InterfaceAdapter, SkippedSite

class MyFrameworkAdapter(InterfaceAdapter):
    name = "my-framework"

    def applies(self, repo: Path) -> bool:
        """Cheap check: does this framework appear in the repo at all?"""

    def discover(self, repo: Path, skipped: list[SkippedSite] | None = None) -> list[Interface]:
        """Return the interfaces this repo exposes."""
```

Register it in `adapters/registry.py` by appending to `ADAPTERS`, and add a test in
`tests/` that writes a small sample repo and asserts the interfaces found.

### The two rules that matter

**Report what you can prove; record what you can't.** If the path is a literal, emit an
`Interface`. If it's an expression you can't resolve, append a `SkippedSite` describing the
site. Never guess, and never silently drop it. The optional LLM tier consumes exactly those
sites, and the unresolved count is a published metric rather than a hidden failure.

```python
if path_is_literal:
    out.append(Interface(method="GET", path=path, type="rest",
                         handler=fn_name, evidence=[f"{rel}:{line}"]))
elif skipped is not None:
    skipped.append(SkippedSite(kind="interface", reason="non-literal-path",
                               file=rel, line=line, expr=src_of(arg),
                               snippet=..., names=identifiers_in(arg)))
```

**Report what you do not recognise.** Framework coverage is never finished, so the Python
adapter also records any call carrying a URL-shaped literal that no pattern claimed, as an
`unrecognised-route-registration` skipped site. If you teach it a new registration style,
that site stops being reported because the route is now found. Check with:

```bash
fl doctor /path/to/repo      # ... interfaces   42 found, 3 unresolved, 2 unrecognised
```

A repo reporting `unrecognised` is the best possible bug report for an adapter: the pattern
is real, in real code, and already isolated.

**Stay generic.** Adapters describe *frameworks and libraries*, not individual codebases. If
a rule would only ever fire on one company's repo, it belongs in the LLM gap-filler or a
`fleetlens.yaml` declaration, not in an adapter. This keeps the core honest and portable.

## Writing a host resolver

Mapping a hostname to a service is policy, not parsing, so it is a plugin point rather than
core logic. A resolver answers one question and returns `None` when it does not know:

```python
class MyResolver:
    name = "my-scheme"
    def resolve(self, binding: HostBinding, ctx: ResolveContext) -> str | None:
        # binding: .key, .value, .host, .port
        # ctx:     .slugs, .declared, .ports, .aliases
        ...

from fleetlens.adapters import hosts
hosts.HOST_RESOLVERS.insert(0, MyResolver())
```

You get the whole binding rather than a hostname because the signal is often elsewhere: a dev
config says `localhost:9402` and only the port identifies the service, while
`ORDERS_SERVICE.HOST` carries it in the key.

Resolvers must be conservative: a wrong answer invents a dependency that never existed, which
is worse than no answer. Prefer whole-token matching over substrings, and return `None` for
anything you are not sure about. The `hosts:` block in `fleetlens.yaml` exists so a human
can settle the cases you decline.

## Conventions

- Every interface carries `evidence` (`path:line`). Provenance is not optional.
- Never mutate deterministic objects from an LLM path; LLM results are stored alongside with
  `source="llm"` and a confidence.
- No network calls in the deterministic core.
- Comments explain *why*, not *what*. Most code needs none.

## Pull requests

Keep them focused: one adapter, one fix, one concern. Include a test. Say what you verified
and what you didn't; "I didn't test the Windows path handling" is a useful sentence.

## License

Contributions are accepted under [Apache-2.0](LICENSE).

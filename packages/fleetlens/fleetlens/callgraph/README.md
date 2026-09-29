# Static call graph (`callgraph/v1`)

A repo-local static call graph — *which function/method calls which* — added as a first-
class context type. It powers cross-symbol impact analysis and, joined with interfaces,
"this endpoint → everything its handler transitively calls."

## Why SCIP + tree-sitter (and not a heuristic)

Two sources, joined by source position:

| Source | Gives us | Why it alone is not enough |
|---|---|---|
| **SCIP** index (`scip-python`, `scip-typescript`, `scip-ruby`) | type-accurate name resolution — `x.get()` binds to the *real* `get` | opaque symbol ids, per-occurrence granularity |
| **tree-sitter** symbol index (`_symbols.json`) | stable, readable node ids (`path::qualname`) + line spans | no name resolution → a name-match heuristic wires `.get()` to every `get` (measured: in-degree 88 of noise) |

Joining them keeps SCIP's precision *and* the platform's stable ids. A name-matching
heuristic and CodeGraphContext's SCIP mapping both collapse symbols to bare names and
reproduce the noise; the symbol-id-faithful join does not.

## Model

- **Nodes** — `code_symbol` Knowledge Objects (functions/methods/classes). Global id
  `code_symbol:<slug>:<path>::<qualname>`. Never embedded (deterministic lookup targets).
- **`calls` edges** — `code_symbol -> code_symbol`. Callers and callees are
  functions/methods. Classes are nodes but **not call-edge targets**: SCIP cannot tell a
  call from an attribute read or type annotation, so a reference to a class symbol
  (`Foo.CONST`, `x: Foo`) is not treated as a call. A constructor `Foo()` resolves to the
  class symbol (not `__init__`) and is therefore out of scope for v1.
- **`handled_by` edges** — `interface -> code_symbol`. Resolved from each interface's
  `evidence` (`path:line`) to the tightest enclosing handler node. The join that turns a
  flat interface list into a downstream call tree.

All of it lands in the existing `knowledge_objects` + `relationships` tables. `source =
"callgraph"`, so a re-run refreshes only the call graph. Each run is a **full snapshot**:
`sync-callgraph` deletes the repo's prior `code_symbol` nodes (the FK cascade drops their
edges) before re-inserting.

## Toolchain

- **`build-callgraph`** (`fleetlens[callgraph]`) — runs the SCIP indexer + emits
  `.context/callgraph.json`. Needs tree-sitter (the symbol index) and the language's SCIP
  indexer CLI on PATH.
- **SCIP reader** — dependency-free (`scip_reader.py`): decodes only the handful of
  protobuf fields the graph needs, pinned to `scip.proto`. No `protobuf`/`protoc`, so it
  is immune to the protobuf-version fragility that breaks other SCIP tooling.
- **SCIP indexers** (host installs, not Python deps):
  - Python — `npm install -g @sourcegraph/scip-python`
  - TypeScript — `npm install -g @sourcegraph/scip-typescript` *(v1.1)*
  - Ruby — `gem install scip-ruby --platform x86_64-linux` (platform-specific gems only;
    no generic build is published) *(v1.1; weakest — Rails metaprogramming limits any
    static call graph)*
- **`sync-callgraph`** (`context-sync`) — loads the artifact into the store.

## Language support

v1 ships **Python**. The extractor consumes SCIP's uniform format, so TypeScript and Ruby
are the *same transformer* — only the per-language indexer invocation is added
(`cli._INDEXERS`).

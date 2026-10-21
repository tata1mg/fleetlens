# Changelog

## 0.1.0

The first release. fleetlens derives a cross-repository service graph from source and serves
it to a coding agent over MCP.

Beta rather than 1.0: the tool surface has been stable for a while and is benchmarked, but
the index schema is at version 1 and some of what the roadmap calls likely next will change
things an integrator can see.

### What it does

**Indexes a folder of repositories** into one SQLite file, which is also the unit of
deployment. Python, TypeScript, JavaScript and Ruby.

- **Interfaces**: HTTP routes and message queue publishers and consumers, read from source
  rather than from a hand-written catalog. Flask, FastAPI, Sanic, Django, Express and Rails
  routing, including mounted and nested routers.
- **Call graph**: symbols and calls through SCIP, with each endpoint linked to the handler
  that serves it.
- **Outbound calls**: the HTTP requests one service makes to another, with the config host
  bindings that say where those addresses point.
- **Cross-repo edges**: `calls` and `publishes_to` between services, joined across
  repository boundaries, each with evidence and a confidence label.

**A manifest, `fleetlens.yaml`,** declares what a repository is, because deployment topology
cannot be read from source:

```yaml
services:   [{name: orders, path: services/orders}]   # repos are not 1:1 with services
libraries:  [{path: packages/shared}]                 # code only, never a mesh node
guidance:   [{path: rules}]                           # authored engineering rules
hosts:      {orders-internal: orders}                 # addresses no resolver could map
```

**Serves it over MCP**, stdio for one engineer or HTTP behind a shared bearer token for a
team. Fourteen tools: `get_service_graph`, `list_services`, `list_interfaces`,
`get_service_relationships`, `get_endpoint_call_graph`, `get_callers`, `get_callees`,
`get_symbol`, `find_symbol`, `get_index_info`, `discover_services`, `discover_interfaces`,
`discover_libraries` and `get_engineering_guidance`.

**An optional LLM tier** adds a summary and an embedding per object, so services, interfaces
and libraries can be found by meaning rather than by name. Content-hash gated, so a re-run
only pays for what changed. Works against a local Ollama or any OpenAI-compatible endpoint;
the deterministic core never needs either.

**Human-authored engineering guidance** as a first-class object type, kept separate from
everything derived. A rulebook is a directory of Markdown rules declared in its manifest,
and each rule is joined to the services it governs by the dependencies they declare. What a
person wrote is labelled as a claim, with the date it was last reviewed, so it is never
served looking like an extraction.

### Measured

A controlled study of 48 runs, 12 cross-repository questions over a 16-service corpus, with
and without fleetlens available to the same agent:

| | baseline | with fleetlens |
|---|---|---|
| F1 against ground truth | 0.788 | 0.888 |
| Fabricated services | 45 | 6 |
| New tokens (mean) | 35,442 | 18,022 |
| Turns (mean) | 16.5 | 8.7 |

Questions, ground truth, harness and the runs where it did not help are in
[`bench/`](bench/) and [BENCHMARK.md](BENCHMARK.md). Server latency under concurrency is in
the README.

### Known limits

- One index per server, and a single team-wide token rather than per-user identity.
- gRPC and GraphQL are not extracted as interfaces.
- Go is not supported.
- A repository deployed more than once is indexed as one service, so both deployments'
  routes and queues are attributed to it ([#12](https://github.com/tata1mg/fleetlens/issues/12)).
- A removed service leaves orphaned enrichment rows behind on re-index.
- `find_symbol` scans when a term matches nothing, because a substring search cannot use a
  B-tree.

See [ROADMAP.md](ROADMAP.md) for what is likely next and what is deliberately out of scope.

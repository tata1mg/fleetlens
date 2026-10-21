# Changelog

## 0.2.0

Pin an index, so an agent evaluation can be reproduced.

The index is a snapshot, and on a server that reindexes on a schedule it moves constantly.
That is right day to day and wrong when you are measuring something: if the index changes
between two runs, a difference in the result cannot be attributed to the change you made.

### Pinning

```bash
fl pin create --as eval-q4 --reason "baseline for the Q4 agent run"
# pinned build 20261021T071755Z as "eval-q4", expires 2026-11-20
```

```
http://<host>:8081/mcp               # always the newest index
http://<host>:8081/pins/eval-q4/mcp  # that snapshot, until it expires
```

Same tools, same token, same responses, answered from the snapshot. One MCP server serves
both, so there is no second process to run and no port to allocate.

- **`fl pin create | list | extend | delete`**, and `fl unpin` as an alias for delete. A
  thin stdlib client over plain HTTP, so curl works just as well and installing the CLI is
  optional. A base `pip install` needs none of the indexing toolchain.
- **Admin routes** `GET/POST /pins` and `PATCH/DELETE /pins/<alias>`, on the existing port
  and bearer token. Deliberately not MCP tools: an agent would carry pin management in its
  tool list and eventually call it.
- **Snapshots are taken with `VACUUM INTO`** from a read-only connection, so they are
  consistent even while a build is running, and the source needs no write access.
- **Retention is server-controlled**, so one person cannot opt out of the policy the
  deployment's owner set. `fl pin create` takes no duration.

| | default | setting |
|---|---|---|
| a pin lives for | 30 days | `FLEETLENS_RETAIN_PINNED_DAYS` |
| one extension adds | up to 10 days | `FLEETLENS_PIN_EXTEND_MAX_DAYS` |
| pins served at once | 3 | `FLEETLENS_MAX_OPEN_PINS` |

### Build identity

Every index now records a build id, when it was built, the schema version, the fleetlens
version, and the git SHA of each indexed repository. `get_index_info` returns them, plus
`repositories` and a `serving` field saying whether the answer came from the live index or a
pin. Record those with a result and it stays attributable after the index has moved on.

An index built before this release reports null build fields rather than failing, but it
cannot be pinned until it is re-indexed.

### Also

- `fl enrich --kinds guidance` no longer requires a generation model to be installed. It
  embeds authored rules and calls no model that generates anything.
- A new CI job installs with no extras and asserts that importing the CLI pulls in none of
  mcp, numpy, tree-sitter, starlette or uvicorn, so the client stays installable on its own.

### Measured

Serving a pin costs a context-variable read and a dictionary lookup per tool call, 54 ns
against a tool latency of 3 to 100 ms.

The cost that matters is memory: each open pin holds its own vector block, measured at 92 MB
for 15,000 enriched interfaces at 1536 dimensions, built in about 280 ms on first use and
sub-millisecond after. That is why the number of pins served at once is a hard cap rather
than a hint. SQLite connections are per store per thread, so the count is stores times
threads.

### Known limits

- **No per-pin disk quota.** A pin is a full copy of the index, and with one shared token
  anyone can fill the disk. Retention bounds pins in time, not in size.
- **No locking** between `extend`, `delete` and `collect`. An extend racing the collector on
  an expired pin can leave a record with no snapshot.
- **`owner` is a label, not authentication.** One shared token means anyone can delete
  anyone's pin.
- **Expiry runs only while the server is up.** Sweeps happen at startup, hourly, and on
  `GET /pins`. There is no cron or timer.
- **Docker's `/data` mount is read-only**, so the default pins directory is not writable
  there. Add a writable volume and point `--pins-dir` at it.
- You can only pin the index that is current when you ask. Builds that have been replaced
  are not kept.

Guide: [docs/pinning.md](docs/pinning.md).

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
  rather than from a hand-written catalog. Flask, FastAPI, Sanic, Express and Rails
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

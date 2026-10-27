# Changelog

## 0.3.0

Read the source behind an indexed symbol, when the operator allows it.

Every other tool here answers from the index and hands back a `file:line`. That is enough
when the agent is sitting on the repository and useless when it is not, which is the normal
case for every repository but the one being worked in. `get_source` returns the lines
themselves, so "what does this handler validate", "which branch returns that error" and
"what does this caller actually send" can be answered without cloning anything.

### `get_source`

```bash
FLEETLENS_EXPOSE_SOURCE=1 fl serve --http --host 0.0.0.0
```

Off unless an operator turns it on, and not offered over stdio at all. Over stdio the agent
is already on the machine holding the repositories and can open the file itself, so the tool
would add a lookup and no capability. Absent rather than registered and refusing, because an
advertised tool that always fails costs an agent a turn and teaches it to stop trusting the
server.

It takes the `code_symbol:...` ids that `find_symbol`, `get_callers`, `get_callees` and
`get_endpoint_call_graph` already return, so the usual path is to locate a symbol with the
existing tools and read it with this one.

Scoped to the symbol's own span. `context_lines` widens it by up to 50 lines either side,
for a decorator or a signature sitting just outside. At most 500 lines come back in one
response and `truncated` says when that bit. There is no whole-file mode: the point of
fleetlens is that an agent stops opening files, and a tool that hands back a thousand lines
invites it straight back.

Paths excluded from indexing are unreachable through it. Not by a second filter, but because
a symbol that was never indexed has no id to ask for.

### Spans you can trust

A span is a pair of line numbers, and line numbers mean nothing without the file they were
counted in. Returning whatever occupies lines 42 to 58 today would be confidently wrong in
the one way this project exists to avoid: indistinguishable from a fact, because shifted
lines usually land on real code that reads perfectly.

So every symbol now records a hash of the file it was parsed from. `get_source` reads at the
commit the index was built from, falls back to the working copy, and returns whichever one
matches that hash, saying which in `read_from`. A pinned index reads at the pin's commit.
When neither matches you get `span_mismatch` and no source, which says the index is stale
for that file rather than guessing at an offset.

### Also

- **`fl serve` refuses a `--db` that does not exist** instead of creating it. Only `--http`
  opened the store read-only, so a mistyped or wrongly-resolved path made stdio create an
  empty database and then answer every question with zero services, with no error anywhere.
  An MCP client picks its own working directory, so a relative path that was right in the
  shell is routinely wrong there.
- **`build-callgraph` no longer reuses `.context/_symbols.json` on sight.** Nothing writes
  that file during a normal index, so it only appeared when someone had run `fl-symbols` by
  hand, and from then on every index of that repo reported spans frozen at that moment while
  recording the current commit beside them. Reuse is now `--symbols`, and the caller owns it.
- **A failed SCIP indexer degrades to symbols only and exits 2.** The output is cleared
  before the attempt, so a failure leaves no call graph rather than the last successful one.
  Loaders read `.context/` without consulting exit status, so a stale file there became
  spans in a fresh index, attached to today's commit.
- **`scip-typescript` is optional**, which is what `fl doctor` always reported. A missing
  indexer now costs the call edges and keeps routes, outbound calls and messaging.
- **The repo's own virtualenv goes on the SCIP indexer's PATH.** `scip-python` resolves
  dependencies by shelling out to `pip`, and without this it resolves against whichever
  environment comes first, or fails outright where there is no `pip` at all.
- **`fl --version`**.
- **`callgraph` and `all` refer to the other extras** rather than repeating their pins, so
  adding a dependency to one cannot leave the others behind.

### Upgrading

An index built before 0.3.0 carries no file hashes. `get_source` still answers from it, with
`verified: false` and a warning saying the span could not be checked against the file it was
measured in. Re-index to make spans verifiable. Nothing else requires a re-index, and the
store schema is unchanged at version 1.

### Measured

Verification is free and reading from history is not. On one developer machine, against a
10 KB file, the `sha1` comparison that decides whether a span is trustworthy takes 7
microseconds, while the `git show` that fetches the bytes takes 48 ms at the median and 63 ms
at p95. Essentially all of that is process spawn, so it scales with the number of calls
rather than with file size, against a tool latency budget of 3 to 100 ms.

The drift this guards against is not theoretical. In one repository carrying a
`_symbols.json` left from an earlier manual run, 15% of 4,110 spans no longer matched the
file they had been measured in. Before this release those spans would have been served as
source with nothing to indicate a problem.

### Known limits

- **One variable moves the server from publishing a map to publishing the code.** Without
  `get_source` a leaked token exposes service names, endpoint paths, queue names, symbol
  names and file paths. With it, a leaked token exposes the source of every repository in
  the index. A single shared token, no TLS and no per-user identity are defensible for a
  metadata service on a trusted network and are a different proposition for a code-read API.
- **No audit trail.** There is no record of which symbols were read or by whom, because
  there is no per-user identity to attribute a read to.
- **All or nothing.** The variable cannot be scoped to some repositories and not others.
- **The checkouts have to be on the serving machine.** An index outlives the repositories it
  was built from, and `get_source` returns `unavailable` when the checkout is gone.
- **A force-pushed or garbage-collected commit** falls back to the working copy, which is
  reported in `read_from` and may not verify.
- **`span_mismatch` returns nothing at all.** That is deliberate, but it means a stale index
  degrades a question into a re-index rather than into a partial answer.

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

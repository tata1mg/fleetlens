# fleetlens

> **The cross-repo context your coding agent can't grep for.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-early-orange.svg)](ROADMAP.md)
[![MCP](https://img.shields.io/badge/MCP-native-5A4FCF.svg)](https://modelcontextprotocol.io)

Your coding agent can read one repository. Your system does not fit in one.

Ask an agent what breaks if you change `POST /orders` and it greps the repository in front of
it, finds nothing, and answers from inference. That failure is hard to spot, because a
confident guess about a service boundary reads exactly like a fact.

fleetlens builds the missing layer. Point it at a folder of service repositories and it derives
the cross-repo service graph: who calls whom over HTTP, who publishes and consumes which
queues, and which endpoint maps to which handler. Then it serves that graph to your agent over
[MCP](https://modelcontextprotocol.io).

### Deterministic first

This is what separates fleetlens from a tool that asks a model to read your code.

The graph is built by parsing source. Python and TypeScript syntax trees, Rails routing
files, and SCIP call graphs. Nothing is sampled, so no answer changes because a model was
asked the same question twice, and the same commit read by the same toolchain produces the
same graph. The toolchain is part of that: the call graph is built by an external SCIP
indexer that resolves imports through the installed dependencies, so a repository whose
virtualenv has changed can yield different call edges from the same source. Routes, queues
and outbound calls are read from the syntax tree alone, so they move only when the source or
fleetlens's own parsers change. Every fact records
the file and line it came from, so you can go and check it yourself.

When a parser cannot work out a path, it writes down where it got stuck instead of guessing.
A route fleetlens could not read becomes a number in the output that you can act on. This
matters because a confident guess about a service boundary reads exactly like a fact, and
that is the failure the whole project exists to avoid.

The LLM layer is optional and it sits on top. It only ever looks at the places the parsers
already flagged, it cannot overrule anything they worked out, and whatever it suggests has to
match a literal string that really exists in the repository. Switch it off and you still have
the graph.

The deterministic core needs no API key, no hosted service, nothing running in production and
no network. It emits no telemetry. The parsers run on your machine because that is the only
place they can run, and switching the optional layers off costs you nothing but the summaries.

The optional layers do call a model, and where that model runs is your choice.
[Ollama](https://ollama.com) on your own hardware keeps everything on the machine, which is
the default. Pointing them at a hosted provider with `--provider openai` sends endpoint
paths, handler names and docstrings to that provider. On a private codebase that distinction
usually decides which of the two you pick.

---

## Quickstart

You need Python 3.10 or newer, Node and npm for the indexers below, and git, which
fleetlens runs at index time to record the commit each repository was at.

fleetlens is not on PyPI yet, so install from source:

```bash
git clone https://github.com/tata1mg/fleetlens.git
cd fleetlens
pip install -e "packages/fleetlens[all]"
```

It shells out to a per-language [SCIP](https://github.com/sourcegraph/scip) indexer for the
call graph. Install the ones you need:

```bash
npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

Two things about the repositories you are about to index. They must be **writable**,
because indexing writes a `.context/` directory into each one. And a Python repository
needs its **virtualenv present**, because `scip-python` resolves imports through the
installed packages and without them the call graph for that repository comes back empty.

`fl doctor` checks the toolchain, and `fl doctor /path/to/repo` checks one repository
against it. Run it before the first index: it catches most of the above.

Index a folder of service repos into one shared SQLite file, then serve it:

```bash
fl doctor
fl index-all /path/to/your/services --db "$PWD/fleetlens.db"
fl serve --db "$PWD/fleetlens.db"
```

Point your MCP client at it over stdio. On Claude Code that is one command, and `which`
and `$PWD` fill in the two paths for you:

```bash
claude mcp add fleetlens -- "$(which fl)" serve --db "$PWD/fleetlens.db"
```

Any other client wants the same thing as JSON. Use absolute paths for both the executable
and the index: the client starts `fl` in a working directory of its own choosing, and it
does not read your shell's `PATH`, so a relative `--db` and a bare `fl` are the two things
that most often turn a working index into an agent that reports no services.

```json
{
  "mcpServers": {
    "fleetlens": {
      "command": "/absolute/path/to/fl",
      "args": ["serve", "--db", "/absolute/path/to/fleetlens.db"]
    }
  }
}
```

Now ask your agent what the orders service exposes, or what breaks if you change
`POST /orders`. It answers from the graph instead of guessing.

To share one index with a team, `fl serve --http` serves the same tools over
streamable-HTTP behind a shared bearer token, with the index opened read-only. See
[docs/deployment.md](docs/deployment.md).

<details>
<summary>Other commands</summary>

```bash
fl index /path/to/repo --db fleetlens.db     # a single repo instead of a fleet
fl resolve --db fleetlens.db                 # recompute cross-repo edges
fl export-graph --db fleetlens.db -o mesh.html   # self-contained HTML render, no server
```
</details>

---

## What your agent gets

| Tool | Answers |
|---|---|
| `list_services` | What services exist in this fleet |
| `list_interfaces` | What does this service expose: routes, queues, and where each came from |
| `get_service_relationships` | Who calls this service, and what it depends on |
| `get_endpoint_call_graph` | Trace an endpoint into the code it actually runs |
| `find_symbol` / `get_symbol` | Locate a function across the fleet |
| `get_callers` / `get_callees` | Blast radius of a change, transitively |
| `get_service_graph` | Every service and every dependency edge, in one call |
| `get_engineering_guidance` | The team's own written standards for writing code here |
| `get_index_info` | Which build answered this, and what commit each repo was at |
| `discover_services` / `discover_interfaces` / `discover_libraries` | Natural-language search *(opt-in, needs embeddings)* |
| `get_source` | The code behind a symbol *(off by default, HTTP only, see below)* |

---

## What you get

Those are what an agent calls. These are the things you use directly.

### A context you can freeze, so an experiment is repeatable

The index is rebuilt on a schedule, which is what you want day to day and exactly wrong the
moment you are measuring something. Change a prompt, rerun the agent, get a different answer,
and you cannot tell whether your change did it or the index moved underneath you.

Pinning freezes a copy of the index and serves it at its own URL, so the same question
gets the same context tomorrow. It fixes fleetlens's side of the experiment, not the model's:
an agent may still word things differently run to run, but it is no longer reading a
different fleet.

```bash
fl pin create --as eval-q4 --reason "baseline for the Q4 agent run"
# pinned build 20261021T071755Z as "eval-q4", expires 2026-11-20
```

```
http://fleetlens.internal:8081/mcp               # always the newest index
http://fleetlens.internal:8081/pins/eval-q4/mcp  # that snapshot, until it expires
```

Same tools, same token, same indexed context, and an alias that does not exist returns 404 rather
than quietly falling back to the live index. `fl pin list`, `fl pin extend` and `fl unpin`
manage them, and all four are a thin convenience over plain HTTP on the same port, so curl
works just as well from CI.

Every answer also carries the build id, the schema version and the commit each repository was
at, through `get_index_info`, so a measurement stays attributable after the pin has expired.
[Full guide](docs/pinning.md).

### The graph as a file, with no server

`fl export-graph` writes the whole dependency graph to a self-contained HTML file. Nothing to
install and nothing to run for whoever opens it, which is usually the point when the audience
is a review or an architecture discussion rather than an agent.

### The code behind a symbol, if you turn it on

Every answer here carries a `file:line`, which is enough when the repository is on your
machine and nothing at all when it is not. On a shared server it usually is not: the index
holds the whole fleet and your laptop holds a handful of checkouts.

`get_source` closes that, for the questions structure cannot answer. What does this handler
validate, in what order does it call its dependencies, which branch returns the error you
are chasing.

```bash
FLEETLENS_EXPOSE_SOURCE=1 fl serve --http --host 0.0.0.0
```

```
get_source("code_symbol:orders:app/main.py::get_order")
-> @app.get("/orders/{order_id}")
   def get_order(order_id: str):
       if not order_id.isdigit():
           raise ValueError("order_id must be numeric")
       ...
```

It takes the symbol ids the other tools already return, so an agent locates a symbol and
then reads it. It is scoped to that symbol's span rather than the file, capped, and there is
no whole-file mode, because the reason fleetlens gets an agent to an answer in fewer tokens
is that it stops opening files.

The source is read at the commit the index was built from, not from the working copy. A
server that rebuilds on a timer moves on between builds, and lines recorded as 42 to 58 may
hold something else an hour later. Reading history instead means the code always matches the
span the index reported, and a pinned index returns the code as of that pin. The response
carries the `commit` it used, and sets `stale` if that commit was gone and it had to fall
back to disk.

**Off unless you set that variable, and never offered over stdio**, where the agent can open
the file itself. Read what you are agreeing to under [Security](#security) before enabling
it on a shared server.

### Your team's standards, not just the ones it inferred

`get_engineering_guidance` serves human-written rules about which libraries, frameworks and
patterns to use in this fleet. Conventions derived by reading a codebase are the conventions
that exist, which are not always the ones you want new code to follow, so these are authored
rather than inferred.

---

## How it works

Three layers. Each is optional on top of the one before it, and the first needs no LLM, no API
key and no network.

### 1. Deterministic core

* **Call graph.** A SCIP index (Python, TypeScript) joined to a tree-sitter symbol index.
  Ruby indexes symbols without call edges unless `scip-ruby` is installed.
* **Interface discovery.** HTTP routes read from the AST (FastAPI, Flask, Sanic, Starlette,
  Express) and from the Rails routing DSL, plus queues and topics as described below.
* **Cross-repo resolver.** Each service's outbound calls are matched against the full fleet
  interface inventory, keyed on the request path. No hostname registry is needed because the
  path is the join key, and cross-language edges fall out of this for free.

### 2. Async edges: queues and topics

Services that talk over SQS, SNS, Kafka, RabbitMQ or Celery get `publishes_to` edges as well as
HTTP `calls`. There are two deterministic sources:

* **Code sites.** Calls like `client.send_message(QueueUrl=...)`, `producer.produce(topic, ...)`
  or `consumer.subscribe(...)` on a messaging client, or in a module that imports a messaging
  library. These establish that the service does messaging, and give the direction.
* **Config channels.** Channel-shaped keys (`QUEUE_NAME`, `TOPIC_ARN`, `*_QUEUE`) in
  `config*.json|yaml`, `.env*` or `settings.py`. Direction comes from the key path, so
  `SUBSCRIBE_TO.HIGH.QUEUE_NAME` consumes and `DISPATCH.EMAIL.QUEUE_NAME` publishes. These are
  only trusted when the service also has messaging code, so a stray queue name never becomes an
  edge.

The channel name is the cross-repo join key: a publisher and a consumer of the same channel
in different repos become one edge.

<details>
<summary>Rails routing, in detail</summary>

Rails declares routes in a DSL rather than per-handler decorators, so the adapter interprets
that DSL. `namespace` and `scope` contribute path prefixes, `resources` expands into the
standard RESTful routes honouring `only:` and `except:`, and `member` and `collection` blocks
nest under the resource with and without its `:id`.

Routes are usually split across files that `config/routes.rb` pulls in with `extend`, so
`config/routes/` is read too. In one production app `config/routes.rb` declared 5 routes and
`config/routes/*.rb` declared 805.

`to: 'orders#index'` is resolved to `app/controllers/orders_controller.rb`, so Rails endpoints
trace into code through `get_endpoint_call_graph` even without a call graph.
</details>

### Service addresses from config

Most fleets keep the address of every service they call in config: `ORDERS_HOST`,
`PAYMENTS_BASE_URL`, `AUTH_SERVICE_ENDPOINT`. That is a twelve-factor convention rather than
one team's habit, so reading those bindings is generic. It is the same mechanism the
messaging adapter uses for queue names.

Deciding *which* service a given host denotes is not generic, so it is a plugin point. The
resolvers that ship cover Kubernetes DNS, slug matching against the names of services already
in the fleet, and the loopback case, where a dev or docker-compose config says
`localhost:9402` and the only signal is the port or the config key. They see the whole
binding rather than a hostname, and they are deliberately conservative: matching is on whole
tokens, so `orders` never matches `reorders-legacy`, and a resolver returns nothing rather
than guess, because a wrong answer invents a dependency that never existed.

Two escape hatches for the addresses they decline. Declare the mapping in `fleetlens.yaml`
under `hosts:`, which needs no code, or register your own resolver, which is a class with one
method and no fork: [writing a host resolver](CONTRIBUTING.md#writing-a-host-resolver).

An address declared in config is evidence of intent to call, not proof of a call, since a
config may list a host nobody uses. These edges carry `confidence: config`, and are upgraded to
`corroborated` when an observed request path agrees.

### 3. Optional LLM tiers

The parsers only report what they can prove. When a route or queue name is an expression
rather than a string, for example `@bp.route(Model.uri())` or
`sqs.send_message(QueueUrl=self.url)`, the site is recorded rather than guessed, and written
to `.context/skipped.json`.

```bash
ollama pull qwen2.5-coder:7b
fl index <repo> --db fleetlens.db --fill-gaps   # resolve those sites, and only those
```

Each site gets the snippet plus the definitions it references, and every answer is
**grounded**: a proposed path is accepted only if it exists as a string literal in that repo
(a queue may also resolve through a config key to its value). Anything else is rejected and
recorded as unresolved, with the model's reason. Results are stored as `source="llm"`
alongside the deterministic ones, never replacing them.

Local by default via Ollama, so your code still never leaves the machine. Remote providers
work too (`--provider openai`), and those do send code to an API, which is why they are opt-in.

<details>
<summary>Semantic search (separate opt-in tier)</summary>

```bash
ollama pull bge-m3            # embeddings
ollama pull qwen2.5-coder:7b  # summaries
fl enrich --db fleetlens.db                       # summaries + embeddings, only what changed
fl serve  --db fleetlens.db --embed-model bge-m3  # enables discover_* tools
```

`fl enrich` writes both summaries and embeddings by default, so it needs both models and
exits with an error if either is missing. `fl enrich --pull` fetches whatever is absent.
To skip the summaries and index embeddings alone, pass `--kinds` without them.
</details>

### fleetlens.yaml, for when the guess is wrong

Everything above is inferred, and the inference is that one repo is one service named after
its directory. A `fleetlens.yaml` at a repo root overrides that. Four keys, each optional,
and `fleetlens.yml` and `fleetlens.json` work the same:

| Key | Declares | Without it |
|---|---|---|
| `services` | what this repo deploys, each with a `name`, a `path` and an optional `language` | the repo is one service, named after its directory |
| `libraries` | paths that are code only and deploy nowhere | nothing is treated as library-only code |
| `guidance` | paths holding your team's authored engineering rules | no authored rules |
| `hosts` | hostnames mapped to service names, for addresses the resolvers decline | heuristics only |

```yaml
# fleetlens.yaml
services:
  - name: orders
    path: services/orders
  - name: billing
    path: services/billing
libraries:                  # code-only paths, never mesh services
  - path: packages/shared
guidance:
  - path: docs/standards
hosts:
  internal-lb-7.example.com: orders
  "*.payments.internal": payments
```

A repo that is entirely a library declares only `libraries`, with `path: "."`. Its code is
indexed and `find_symbol` reaches it, but it contributes no service and no interfaces. That
distinction earns its place: a shared package is not something another service calls over the
network, so listing one as a service invents a node nobody deploys, hands it whatever routes
its examples and its own health blueprint happen to declare, and makes "which services are
unused" unreadable.

With the semantic tier, `fl enrich` summarises each library from its README and the code it
exports, and `discover_libraries` finds it by what it provides ("retry with backoff"), so a
helper that already exists turns up before someone writes it again.

### Excluding directories

A repo controls what fleetlens reads with a `.fleetlensignore` at its root, one
gitignore-style pattern per line:

```
examples/
fixtures/
generated/
```

This applies everywhere: interfaces, outbound calls, config hosts, symbols and the call
graph. Keys, certificates and credential files are never read whatever the file says, and a
repo can exclude more but cannot opt back in; `SECURITY.md` lists the patterns that always
apply.

---

## How it compares

The comparison worth making is where each one's facts come from, because that is what does
not change when any of these ships a new feature.

|  | fleetlens | Sourcegraph / SCIP | Backstage | OpenTelemetry |
|---|---|---|---|---|
| Where the service graph comes from | parsed from source | a code index, built for navigation | a catalog you own and populate | observed traces |
| Needs running services | no | no | no | **yes** |
| Relationships come from code, not description | **yes** | **yes** | no | **yes** |
| Sees a path that never executed | **yes** | yes | n/a | **no** |

The last row is the real split with tracing: a dependency only exercised by a rare code path
produces no spans, so it is absent from a trace-derived graph and present in a parsed one.
The split with a code index is narrower, and is about the question being answered:
navigation is per-symbol, where this is a service-to-service graph joined across repository
boundaries.

The split with a catalog is about where a relationship comes from rather than how much
typing it costs. Backstage can register entities automatically through discovery providers,
so the catalog need not be assembled by hand; what a service depends on is still something
the descriptor states, and a descriptor can be right, stale or absent independently of the
code. Here that edge is read out of the call site, which is why it carries a file and a line
and a confidence rather than an author.

fleetlens is not a code-search engine, not a service catalog you curate, and not a repo
chatbot. It is the derived graph that sits underneath those, and MCP is its only interface:
there is no UI, because the reader is an agent.

---

## What it can't do yet

Completeness is not claimed. Gaps are reported instead: `fl index` prints an unresolved-site
count on every run, and `.context/skipped.json` lists each one.

What a language supports differs by capability, so the matrix below is split by capability
rather than reduced to a yes or no.

| | Routes | Outbound HTTP | Queues | Call graph |
|---|---|---|---|---|
| **Python** | yes | yes | yes | `scip-python`, required |
| **TypeScript** | Express only | yes | yes | `scip-typescript`, required |
| **JavaScript** | Express only | **no** | **no** | none |
| **Ruby** | Rails routing DSL | yes | config keys only | `scip-ruby`, optional |

Nothing else is supported. Go, Java, Kotlin, Scala, Rust, PHP and C# files are counted so
that engineering-guidance rules can be scoped by language, but there is no adapter for any
of them and a repository in one of those languages is not discovered by a fleet sweep.

- **Frameworks:** FastAPI, Flask, Sanic, Starlette and a generic fallback for unrecognised
  Python route decorators; Express; the Rails routing DSL. No Django, no NestJS decorators,
  no gRPC, no GraphQL.
- **JavaScript is asymmetric.** Routes are extracted from `.js` files, outbound calls and
  queue sites are not, so a JavaScript service can appear as a leaf in the graph when it is
  not one.
- **Express router prefixes** from `app.use('/prefix', router)` are not composed, so a
  TypeScript or JavaScript path can be missing its prefix. Python mount prefixes are resolved.
- **Rails engines** mounted with `mount` are recorded as skipped rather than followed.
- **Config-derived hosts:** an outbound call assembled as `urljoin(config_host, endpoint)` is
  recorded as unresolved rather than joined.
- **Dynamically registered consumers** have no handler anchor, so a queue consumer's callback
  isn't linked to its channel.
- **Source-only.** If two services communicate in a way that isn't visible in source, this
  will not see it. That is the deliberate trade for needing nothing at runtime.

## Design principles

1. Deterministic first; LLM optional.
2. Source-only ingestion. Never boot a service.
3. Every fact carries provenance: the file and line it was read from. Derived
   relationships carry a confidence label as well, because joining two repositories
   is an inference and reading a route out of a file is not.
4. Report the gaps instead of guessing.
5. Pluggable producers behind one write contract, so adding a framework never touches the core.

## Security

The deterministic core runs entirely on your machine and contacts nothing. Config is read for
structure, never for secret values, and `.fleetlensignore` is fail-closed.

The LLM tiers are the only path that can send anything anywhere. They are off by default, and
with Ollama they stay on your hardware. With `--provider openai` they do not: prompts carry
endpoint paths, handler names and docstrings.

`fl serve --http` is protected by one shared bearer token and offers no TLS, no per-user
identity and no rotation without a restart. Run it on a private network.

### What `FLEETLENS_EXPOSE_SOURCE` costs you

Worth being blunt about, because it changes what the server is rather than adding to it.

Without it, a leaked token exposes the shape of your systems: service names, endpoint paths,
queue names, symbol names and file paths. With it, a leaked token exposes **the source of
every repository in the index**. One variable moves the server from publishing a map to
publishing the code.

Weigh that against the properties above. A single token shared by a team, no TLS, no way to
tell who made a request and no way to revoke one person without rotating for everyone are
defensible for a metadata service on a trusted network. They are a different proposition for
a code-read API, and the server does not become more careful because you set the variable.

Two things limit the blast radius, and it is worth knowing exactly how far they go:

- **Only indexed symbols are readable.** There is no path argument, so nothing outside the
  index can be reached, and files excluded by `.fleetlensignore` or by the always-excluded
  patterns were never indexed in the first place. The protection is that the span does not
  exist, not that a check rejects it.
- **Exclusions are name-based.** A credential sitting inside an ordinary source file is
  readable, exactly as it is to anyone who can clone the repository.

So the question to ask is not whether fleetlens leaks source. It is whether everyone holding
the shared token should be able to read every repository in the index. Where that is already
true, such as a team whose members can clone all of them anyway, this changes little. Where
it is not, leave it off.

More in [SECURITY.md](SECURITY.md).

## Contributing

Adding a framework adapter is the highest-leverage contribution and does not touch the core.
See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports that include the repo shape
that confused a parser are especially welcome.

## Benchmark

Two measurements. The first asks whether fleetlens makes a coding agent better at questions
that span several repositories. The second asks how fast the server answers when several
agents are using it at once.

### Does it help an agent?

A controlled study on one private 16 service corpus. One coding agent, 12 questions
spanning several repositories, run with and without the fleetlens MCP server available and
everything else held constant: 24 runs per arm, 2 repetitions per question per arm.

| | baseline | with fleetlens | change |
|---|---|---|---|
| F1 against ground truth | 0.788 | 0.888 | +13% |
| New tokens (mean) | 35,442 | 18,022 | -49% |
| Turns (mean) | 16.5 | 8.7 | -47% |

Read one caveat with those numbers. The size of the benefit depended more on whether the
agent discovered the tools at all than on the arm it was in: in the runs where it never
called fleetlens, it scored like the baseline. That split was not randomised, because the
agent chose when to go looking, so the gap between an adopting run and a non-adopting one is
an optimistic figure rather than a measured effect.

One corpus, one framework family, one agent. Treat the numbers as indicative rather than
general. The questions, the ground truth, the harness, the scoring and the questions where
fleetlens did not help are all in
[BENCHMARK.md](BENCHMARK.md) and
[`bench/`](bench).

### Server performance

Measured on a real fleet served from one small VM. The index held 205 services, 15,461
interfaces, 356,164 symbols and 288,596 relationships, in a SQLite file of 664 MB. The
machine was an AWS m7g.xlarge: Graviton3, 4 virtual CPUs, 16 GB of memory, no swap, running
fleetlens with its default settings.

Each cell gives the median and the 95th percentile response time in milliseconds, for a
given number of agents talking to the server at once.

| tool | 1 agent | 8 agents | 16 agents | best requests/sec |
|---|---|---|---|---|
| `get_index_info` | 5.3 / 6.0 | 30.4 / 85.0 | 63.2 / 202.2 | 155 |
| `get_endpoint_call_graph` | 6.1 / 7.3 | 37.2 / 85.0 | 78.6 / 202.8 | 128 |
| `get_callers` | 5.6 / 6.0 | 36.8 / 135.7 | 68.0 / 212.6 | 151 |
| `get_callees` | 5.7 / 7.0 | 37.6 / 128.0 | 75.8 / 219.6 | 145 |
| `get_symbol` | 5.8 / 6.7 | 33.8 / 107.8 | 74.9 / 205.0 | 140 |
| `find_symbol` | 5.8 / 47.2 | 41.6 / 121.5 | 72.7 / 196.1 | 90 |
| `list_interfaces` | 5.6 / 12.6 | 37.1 / 104.5 | 111.7 / 276.4 | 132 |
| `get_service_relationships` | 8.5 / 24.9 | 72.9 / 111.1 | 171.8 / 309.1 | 88 |
| `list_services` | 18.1 / 18.7 | 57.5 / 110.9 | 140.6 / 265.5 | 75 |
| `get_service_graph` | 76.4 / 105.3 | 582.2 / 661.6 | 1196.0 / 1291.4 | 12 |
| `find_symbol`, nothing matches | 272.7 / 277.8 | 1657.0 / 1918.9 | 3290.9 / 3538.0 | 4 |

Most of the call graph answers in under ten milliseconds on an index of a third of a million
symbols, because each lookup goes through a database index rather than reading every row.

Two rows are slower, and both for reasons worth stating. `get_service_graph` hands back the
entire dependency graph in one response, so it is slow because the answer is large. It exists
because on the benchmark corpus, asking the same question the other way round took between
18 and 26 separate calls.
And `find_symbol` has to read every symbol when the search term matches nothing, because a
substring search cannot use a database index. A term that does match comes back in about six
milliseconds. Fixing that is the next thing on
[ROADMAP.md](ROADMAP.md).

Reproduce with [`bench/mcp_perf.py`](bench/mcp_perf.py), which samples its
arguments from whatever index it is pointed at:

```bash
python bench/mcp_perf.py --url http://host:8081/mcp --token "$FLEETLENS_TOKEN" \
    --concurrency 1,8,16 --requests 60
```

Three things to know about these numbers. The measuring script ran on the same four-CPU
machine as the server, so it was competing for the same processors, which makes the results
slightly worse than the server alone would manage. This deployment serves the parsed index
only, with no embedding model loaded, so the two search tools are not covered. And these
numbers predate the current thread pool: the run was made when every tool was limited to
two at a time, a limit that now applies only to the one tool that builds its response in
Python rather than waiting on a socket. Concurrent figures today are better than the table
shows.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## License

[Apache-2.0](LICENSE).

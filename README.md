# fleetlens

> **The cross-repo context your coding agent can't grep for.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-early-orange.svg)](ROADMAP.md)
[![MCP](https://img.shields.io/badge/MCP-native-5A4FCF.svg)](https://modelcontextprotocol.io)

Your coding agent can read one repository. Your system is forty of them.

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
files, and SCIP call graphs. The same commit always produces the same graph. Nothing is
sampled, so no answer changes because a model was asked the same question twice. Every fact
records the file and line it came from, along with how confident the parser was, so you can
go and check it yourself.

When a parser cannot work out a path, it writes down where it got stuck instead of guessing.
A route fleetlens could not read becomes a number in the output that you can act on. This
matters because a confident guess about a service boundary reads exactly like a fact, and
that is the failure the whole project exists to avoid.

The LLM layer is optional and it sits on top. It only ever looks at the places the parsers
already flagged, it cannot overrule anything they worked out, and whatever it suggests has to
match a literal string that really exists in the repository. Switch it off and you still have
the graph.

None of this leaves your network. There is no API key, no hosted service, nothing to run in
production, and no telemetry. The parsers run on your machine because that is the only place
they can run. The optional layers talk to [Ollama](https://ollama.com) on your own hardware,
so even those stay inside the building. On a private codebase that is often the difference
between being able to use something and not.

---

## Quickstart

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

Index a folder of service repos into one shared SQLite file, then serve it:

```bash
fl index-all /path/to/your/services --db fleet.db
fl serve --db fleet.db
```

Point your MCP client at it over stdio:

```json
{ "mcpServers": { "fleetlens": { "command": "fl", "args": ["serve", "--db", "fleet.db"] } } }
```

Now ask your agent what the orders service exposes, or what breaks if you change
`POST /orders`. It answers from the graph instead of guessing.

To share one index with a team, `fl serve --http` serves the same tools over
streamable-HTTP behind a shared bearer token, with the index opened read-only. See
[docs/deployment.md](docs/deployment.md).

<details>
<summary>Other commands</summary>

```bash
fl index /path/to/repo --db fleet.db     # a single repo instead of a fleet
fl resolve --db fleet.db                 # recompute cross-repo edges
fl export-graph --db fleet.db -o mesh.html   # self-contained HTML render, no server
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
| `discover_services` / `discover_interfaces` | Natural-language search *(opt-in, needs embeddings)* |

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

### Rails

Rails declares routes in a DSL rather than per-handler decorators, so the adapter interprets
that DSL. `namespace` and `scope` contribute path prefixes, `resources` expands into the
standard RESTful routes honouring `only:` and `except:`, and `member` and `collection` blocks
nest under the resource with and without its `:id`.

Routes are usually split across files that `config/routes.rb` pulls in with `extend`, so
`config/routes/` is read too. In one production app `config/routes.rb` declared 5 routes and
`config/routes/*.rb` declared 805.

`to: 'orders#index'` is resolved to `app/controllers/orders_controller.rb`, so Rails endpoints
trace into code through `get_endpoint_call_graph` even without a call graph.

### Service addresses from config, and pluggable host resolution

Most fleets keep the address of every service they call in config: `ORDERS_HOST`,
`PAYMENTS_BASE_URL`, `AUTH_SERVICE_ENDPOINT`. That is a twelve-factor convention, not one
team's habit, so extracting those bindings is generic. It is the same mechanism the messaging
adapter already uses for queue names.

Deciding *which* service a host denotes is **not** generic, so it is pluggable. Resolvers are
tried in order, first hit wins:

| Resolver | Handles |
|---|---|
| `ManifestResolver` | explicit `hosts:` mappings in `fleetlens.yaml`, no code needed |
| `KubernetesDNSResolver` | `orders.default.svc.cluster.local` |
| `SlugMatchResolver` | `orders-svc`, `orders_service:8080`, the common default |
| `PortResolver` | `localhost:9402` where another service declares `PORT: 9402` |
| `KeyNameResolver` | `ORDERS_SERVICE.HOST = localhost:...`, where the key names the target |

The last two matter more than they look: development and docker-compose configs address peers
on loopback, so the hostname carries nothing and the **port** or the **key** is the only
signal. A resolver sees the whole binding (key, value, host, port), not just a hostname.

`SlugMatchResolver` is deliberately conservative: it matches whole tokens, so `orders` never
matches `reorders-legacy`, and it refuses known third-party domains outright.

When your addresses follow no pattern a heuristic could infer, declare them. No fork or patch
is needed:

```yaml
# fleetlens.yaml
hosts:
  internal-lb-7.example.com: orders
  "*.payments.internal": payments
```

Or register your own resolver and keep everything else:

```python
from fleetlens.adapters import hosts

class LegacyPrefix:
    name = "legacy"
    def resolve(self, binding, ctx):
        return "orders" if binding.host.startswith("legacy-") else None

hosts.HOST_RESOLVERS.insert(0, LegacyPrefix())
```

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
fl index <repo> --db fleet.db --fill-gaps   # resolve those sites, and only those
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
ollama pull bge-m3
fl enrich --db fleet.db                       # summaries + embeddings, only what changed
fl serve  --db fleet.db --embed-model bge-m3  # enables discover_* tools
```
</details>

### Repos ≠ services

One repo is one service by default. When a repo ships several, declare them:

```yaml
# fleetlens.yaml
services:
  - name: orders
    path: services/orders
  - name: billing
    path: services/billing
libraries:                  # code-only paths, never mesh services
  - path: packages/shared
```

A utilities repo that is entirely a library declares only the `libraries` key:

```yaml
# fleetlens.yaml
libraries:
  - path: "."
```

Its code is indexed and `find_symbol` reaches it, but it contributes no service and no
interfaces. That matters because a shared package is not something another service calls
over the network: listing one as a service invents a node nobody deploys, hands it whatever
routes its examples and its own health blueprint happen to declare, and makes "which
services are unused" unreadable.

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

|  | fleetlens | Sourcegraph / SCIP | Backstage | OpenTelemetry |
|---|---|---|---|---|
| Cross-repo service graph | derived from source | repo-scoped navigation | hand-written YAML | derived from traces |
| Needs running services | no | no | no | **yes** |
| Needs a human-maintained catalog | no | no | **yes** | no |
| Reports what it couldn't determine | **yes** | n/a | no | no |
| Built for agents (MCP) | **yes** | no | no | no |

fleetlens is not a code-search engine, not a service catalog you curate, and not a repo
chatbot. It is the derived graph that sits underneath those.

---

## What it can't do yet

Completeness is not claimed. Gaps are reported instead: `fl index` prints an unresolved-site
count on every run, and `.context/skipped.json` lists each one.

- **Languages:** Python and TypeScript. No Go, Java, or Ruby yet.
- **Frameworks:** FastAPI, Flask, Sanic, Starlette, Express. No NestJS decorators, Django,
  gRPC, or GraphQL.
- **Config-derived hosts:** an outbound call assembled as `urljoin(config_host, endpoint)` is
  recorded as unresolved rather than joined.
- **Dynamically registered consumers** have no handler anchor, so a queue consumer's callback
  isn't linked to its channel.
- **Source-only.** If two services communicate in a way that isn't visible in source, this
  will not see it. That is the deliberate trade for needing nothing at runtime.

## Design principles

1. Deterministic first; LLM optional.
2. Source-only ingestion. Never boot a service.
3. Every fact carries provenance and confidence.
4. Report the gaps instead of guessing.
5. Pluggable producers behind one write contract, so adding a framework never touches the core.

## Security

The default path is local-only: the deterministic core runs entirely on your machine, so
nothing can exfiltrate because nothing leaves. Config is read for structure, never for secret
values, and `.fleetlensignore` is fail-closed. LLM tiers are the only path that can send code
anywhere, they are off by default, and local Ollama keeps even those on-machine. More in
[SECURITY.md](SECURITY.md).

## Contributing

Adding a framework adapter is the highest-leverage contribution and does not touch the core.
See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports that include the repo shape that confused
a parser are especially welcome.

## Benchmark

Two measurements. The first asks whether fleetlens makes a coding agent better at questions
that span several repositories. The second asks how fast the server answers when several
agents are using it at once.

### Does it help an agent?

Placeholder, filled in when the current run finishes.

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
because asking the same question the other way round took between 18 and 26 separate calls.
And `find_symbol` has to read every symbol when the search term matches nothing, because a
substring search cannot use a database index. A term that does match comes back in about six
milliseconds. Fixing that is the next thing on [ROADMAP.md](ROADMAP.md).

Reproduce with [`bench/mcp_perf.py`](bench/mcp_perf.py), which samples its arguments from
whatever index it is pointed at:

```bash
python bench/mcp_perf.py --url http://host:8081/mcp --token "$FLEETLENS_TOKEN" \
    --concurrency 1,8,16 --requests 60
```

Three things to know about these numbers. The measuring script ran on the same four-CPU
machine as the server, so it was competing for the same processors, which makes the results
slightly worse than the server alone would manage. This deployment serves the parsed index
only, with no embedding model loaded, so the two search tools are not covered. And fleetlens
runs at most two tool calls at a time by default, because most of the work is building the
response in Python rather than waiting on the disk, and allowing more turned out to be
slower rather than faster.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## License

[Apache-2.0](LICENSE).

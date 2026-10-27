# fleetlens MCP tools

Generated from the server's own tool registrations by
`python -m fleetlens.server.toolsdoc`. Do not edit by hand: a test regenerates this
page and fails when it differs, so a signature or docstring change that is not
reflected here turns CI red.

15 tools. Descriptions are verbatim from the docstrings an agent reads
over MCP, so what you see here is what it sees.

## Availability

Tools are registered only when they can answer. A tool that is absent is not an
error: an advertised tool that always fails costs an agent a turn and teaches it to
stop trusting the server. A default `fl serve` against an index with no guidance and
no embeddings advertises 10 of these 15.

| Tool | Registered when |
|---|---|
| [`discover_interfaces`](#discover_interfaces) | an embedding model is configured (`--embed-model`) |
| [`discover_libraries`](#discover_libraries) | an embedding model is configured (`--embed-model`) |
| [`discover_services`](#discover_services) | an embedding model is configured (`--embed-model`) |
| [`find_symbol`](#find_symbol) | always |
| [`get_callees`](#get_callees) | always |
| [`get_callers`](#get_callers) | always |
| [`get_endpoint_call_graph`](#get_endpoint_call_graph) | always |
| [`get_engineering_guidance`](#get_engineering_guidance) | the index contains guidance objects |
| [`get_index_info`](#get_index_info) | always |
| [`get_service_graph`](#get_service_graph) | always |
| [`get_service_relationships`](#get_service_relationships) | always |
| [`get_source`](#get_source) | `--http` **and** `FLEETLENS_EXPOSE_SOURCE` is set |
| [`get_symbol`](#get_symbol) | always |
| [`list_interfaces`](#list_interfaces) | always |
| [`list_services`](#list_services) | always |

---

## discover_interfaces

**Registered when** an embedding model is configured (`--embed-model`).

| Parameter | Type | Default |
|---|---|---|
| `query` | string | **required** |
| `limit` | integer | `5` |

Find endpoints by meaning, e.g. "the endpoint that sends order-confirmation
emails". Ranked semantic search over LLM-generated interface summaries (needs the
enrichment tier). Returns interface ids + summaries + scores. Use get_endpoint_call_graph
on a result to trace it.

---

## discover_libraries

**Registered when** an embedding model is configured (`--embed-model`).

| Parameter | Type | Default |
|---|---|---|
| `query` | string | **required** |
| `limit` | integer | `5` |

Find shared code libraries by what they provide, in natural language, e.g. "retry
with exponential backoff" or "parsing feature-flag config". Use before writing a
helper that may already exist. Ranked semantic search over LLM-generated library
summaries (needs the enrichment tier). Returns library ids + summaries + scores; use
find_symbol to reach the code.

---

## discover_services

**Registered when** an embedding model is configured (`--embed-model`).

| Parameter | Type | Default |
|---|---|---|
| `query` | string | **required** |
| `limit` | integer | `5` |

Find services by what they do, in natural language. Ranked semantic search over
LLM-generated service summaries (needs the enrichment tier). Returns service ids +
summaries + scores.

---

## find_symbol

| Parameter | Type | Default |
|---|---|---|
| `query` | string | **required** |
| `limit` | integer | `20` |

Find code_symbol ids by a substring of their id — a method name, class, or file
fragment (e.g. "OrderManager.get" or "managers/db.py"). The entry point to the other
call-graph tools when you know a name but not the full id.

---

## get_callees

| Parameter | Type | Default |
|---|---|---|
| `symbol_id` | string | **required** |
| `max_depth` | integer | `2` |

What a function/method calls, transitively — its downstream call tree. `symbol_id`
is a fully-qualified code_symbol id (from find_symbol or an endpoint trace). Only
in-repo calls are edges; stdlib/third-party calls are intentionally omitted.

---

## get_callers

| Parameter | Type | Default |
|---|---|---|
| `symbol_id` | string | **required** |
| `max_depth` | integer | `2` |

Who calls a function/method, transitively — its upstream callers (the blast radius
of changing it). `symbol_id` is a fully-qualified code_symbol id.

---

## get_endpoint_call_graph

| Parameter | Type | Default |
|---|---|---|
| `interface_id` | string | **required** |
| `max_depth` | integer | `3` |

Trace an endpoint into the code: from an interface to its handler and everything
that handler transitively calls. Use for impact analysis ("what does changing this
endpoint touch?"). `interface_id` is a fully-qualified interface id (e.g.
"interface:orders:post-orders"). `max_depth` bounds the walk (default 3, capped 6).
Returns resolved handlers, reached nodes (with path + line), and `calls` edges.

---

## get_engineering_guidance

**Registered when** the index contains guidance objects.

| Parameter | Type | Default |
|---|---|---|
| `topic` | string | `''` |
| `service` | string | `''` |
| `detail` | string | `'auto'` |

Organisation engineering standards: which libraries, frameworks and patterns to
use when writing code in this fleet. Human-authored, not derived from source.

Call this BEFORE writing or reviewing code in any of these repositories. Conventions
inferred by reading the fleet are the *existing* conventions, which during a
migration are the ones being moved away from.

`service` returns the rules governing that service in full, matched on what it
declares. `topic` searches by meaning and returns the nearest few rules as titles,
excerpts and scores, so ask again with `topic=<id>` for the text of one. Neither
lists everything. `detail` is "auto", "full" or "titles" to override that.

A topic search always returns its nearest matches, which are not necessarily
relevant ones. Read the titles and decide: if none of them covers the question, the
rulebook does not cover it, and you should say so rather than apply the closest
one.

---

## get_index_info

Takes no parameters.

When this index was built, and how much it covers.

Worth checking before you trust an answer on a shared server: the index is a
snapshot of the repositories at the moment it was built, so anything merged since
is invisible to every other tool here. Also tells you how many services are
covered, which is how you find out a repository was never indexed at all.

`build_id` identifies this exact build. A harness running an evaluation should
record it with each result, so the answer can be attributed to the index that
produced it. `repositories` maps each indexed repository to the git commit it was
built from; it is here and not on /healthz because it can run to hundreds of
entries.

---

## get_service_graph

| Parameter | Type | Default |
|---|---|---|
| `min_confidence` | string | `''` |

THE WHOLE microservice dependency graph in one call — every service and every
service-to-service edge across all repositories.

Use this FIRST for any fleet-wide or cross-repository question: which service is
depended on by the most others, what depends on X, what would break if X changed,
which services are unused or isolated, how many services exist, which talk over
HTTP vs queues. Answers these without reading any source files.

Each service carries depends_on_count / depended_on_by_count, so ranking and
fan-in/fan-out questions are answered directly from this one response. Each edge
carries `relationship` (calls | publishes_to | shares_channel), a `confidence`, and
the evidence that produced it (request paths, queue names, config keys).

`min_confidence` optionally filters weaker edges: "high" or "corroborated".

Check `likely_duplicate_deployments` before ranking: one repository checked out
several times appears as several services, which inflates raw counts.

---

## get_service_relationships

| Parameter | Type | Default |
|---|---|---|
| `service_id` | string | **required** |

Dependencies of ONE service: `downstream` (services it calls or publishes to) and
`upstream` (services that call it). Use for a single service; for questions spanning
the whole fleet use get_service_graph instead, which returns everything in one call.

`service_id` is fully-qualified (e.g. "service:orders"). Edges are derived
deterministically from source and config, and each carries a confidence and the
evidence that produced it.

---

## get_source

**Registered when** `--http` **and** `FLEETLENS_EXPOSE_SOURCE` is set.

| Parameter | Type | Default |
|---|---|---|
| `symbol_id` | string | **required** |
| `context_lines` | integer | `0` |
| `max_lines` | integer | `200` |

The actual source of an indexed symbol, read from the repository.

Use when the structure is not enough and the question is about what the code does:
which validations run and in what order, which branch returns an error, what a
handler calls before it writes. Every other tool here answers from the index and
gives you a `file:line`; this returns the lines themselves.

Takes a `code_symbol:...` id from `find_symbol`, `get_callers`, `get_callees` or
`get_endpoint_call_graph`, so the usual path is to locate the symbol first and read
it second.

Scoped to the symbol's own span. `context_lines` widens it by a few lines either
side, for a decorator or a signature that sits just outside, and is capped at 50.
At most 500 lines come back in one response, and `truncated` says when that bit.
There is no whole-file mode: read the symbols you need rather than the files they
are in.

The source is checked against a hash of the file the index actually parsed. It
reads the commit the index was built from first, falls back to the working copy,
and returns whichever matches, saying which in `read_from`; a pinned index reads
at the pin's commit. `verified` is false only for indexes built before those
hashes existed. When nothing matches you get `span_mismatch` and no source, which
means the index is stale for that file and needs rebuilding: the lines would
otherwise land on a neighbouring function and read perfectly.

---

## get_symbol

| Parameter | Type | Default |
|---|---|---|
| `symbol_id` | string | **required** |

Look up one code symbol: location (path + line) and how many direct callers/callees
it has. Returns a not_found response (with did_you_mean) if unknown.

---

## list_interfaces

| Parameter | Type | Default |
|---|---|---|
| `service_id` | string | **required** |

Every interface a service exposes: id, method, path, handler, and provenance
(`source` = static from the parsers, or llm from the grounded gap-filler). The
entry point to get_endpoint_call_graph when you know the service but not the
endpoint. `service_id` is fully-qualified (e.g. "service:orders").

---

## list_services

Takes no parameters.

List every indexed microservice in this codebase, with interface and symbol
counts. Use to discover what services exist before drilling in. For dependencies
between them, prefer get_service_graph, which returns the whole mesh at once.

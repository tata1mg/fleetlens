# Roadmap

fleetlens is early. This is what works, what does not, and what is likely next. It is
descriptive rather than a schedule, and dates are deliberately absent.

## What works today

**Languages.** Python, TypeScript and Ruby. Interfaces, queues and cross-repo edges are
found for all three. The call graph additionally needs a SCIP indexer: `scip-python` and
`scip-typescript` install from npm, while Ruby needs a Sorbet setup, so Ruby routes and
queues work out of the box but callers and callees do not.

**Frameworks.** FastAPI and Flask, Express and NestJS, Rails. Message channels are found
across the common brokers by reading publish and consume sites plus configuration.

**The cross-repo resolver.** Outbound calls and config-declared hosts are joined against
the fleet's interface inventory to produce `calls`, `publishes_to` and `shares_channel`
edges. Host resolution is pluggable, so a fleet with its own naming convention can teach
fleetlens that convention without a fork.

**Serving.** An MCP server over stdio for one engineer, or over HTTP behind a shared token
for a team. `export-graph` renders the same graph as a standalone HTML file with no server.

**Optional LLM tier.** The parsers record the sites they could not resolve, and the tier
resolves exactly those. An answer is accepted only when the path or channel it proposes
exists literally in the repository. Off by default, local Ollama by default when on.

## Known limits

These are the honest gaps, not a to-do list with owners.

- **Go and Java are not supported.** They are the most common request we expect.
- **One measured corpus.** [BENCHMARK.md](BENCHMARK.md) reports results on a single
  organisation's repositories with one framework family. Treat the numbers as indicative,
  not general, until there is a public polyglot corpus to repeat them on.
- **Recall is not complete and is not claimed to be.** Every fact carries evidence and a
  confidence label, and unresolved sites are counted and reported rather than hidden.
- **gRPC and GraphQL** are not extracted as interfaces.
- **Incremental enrichment is coarse.** Content hashing gates re-work, but orphaned
  objects from a removed service are not pruned.
- **One index per server.** No multi-tenancy, and the shared HTTP server has a single
  team-wide token rather than per-user identity.

## Likely next

Roughly in order of how often we expect it to matter:

1. **Go support**, which needs an interface adapter and a SCIP indexer decision.
2. **A public benchmark corpus**, so the numbers can be reproduced by someone who does not
   work here. This is the single biggest weakness in the current evidence.
3. **gRPC and GraphQL interfaces.**
4. **Index metadata**: schema version, the git SHA of each indexed repository, and a
   server that refuses an index newer than it understands.
5. **Pruning on re-index**, so a removed service leaves no orphans behind.

Framework adapters are the easiest contribution and do not touch the core. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Not planned

- **A web UI.** The consumer is a coding agent. `export-graph` covers the case where a
  human wants to look at the graph.
- **A runtime or telemetry dependency in the core.** Deriving the graph from source is the
  point; it works on code that is not deployed and on services that are not instrumented.
  Trace-derived edges would be a useful optional corroborator, never a requirement.
- **A hand-maintained catalog.** If a fact has to be typed in by a human and kept current
  by a human, it will go stale, and a stale catalog is worse than none.
- **Guessing.** A confident guess about a service boundary is indistinguishable from a
  fact, so unresolved stays unresolved and gets reported as such.

# fleetlens

**The cross-repo context your coding agent can't grep for.**

Your coding agent can read one repository. Your system does not fit in one.

Ask an agent what breaks if you change `POST /orders` and it greps the repository in front
of it, finds nothing, and answers from inference. That failure is hard to spot, because a
confident guess about a service boundary reads exactly like a fact.

fleetlens builds the missing layer. Point it at a folder of service repositories and it
derives the cross-repo service graph: who calls whom over HTTP, who publishes and consumes
which queues, and which endpoint maps to which handler. Then it serves that graph to your
agent over [MCP](https://modelcontextprotocol.io).

## Measured

A controlled study, 24 runs per arm, 12 cross-repository questions over a 16-service
corpus, the same agent with and without fleetlens.

| | Baseline | fleetlens | Change |
|---|---|---|---|
| F1 against ground truth | 0.788 | 0.888 | +13% |
| Fabricated services | 45 | 6 | -87% |
| New tokens (mean) | 35,442 | 18,022 | -49% |
| Turns (mean) | 16.5 | 8.7 | -47% |
| Wall clock (mean) | 115.5s | 35.3s | -69% |

The questions, the ground truth, the harness and the runs where it did not help are all in
[BENCHMARK.md](https://github.com/tata1mg/fleetlens/blob/main/BENCHMARK.md).

## Deterministic first

The graph is built by parsing source, not by asking a model to read your code. Nothing is
sampled, and every fact records the file and line it came from, so you can go and check it.

When a parser cannot work out a path it writes down where it got stuck instead of guessing.
The LLM layer is optional, sits on top, only looks at places the parsers already flagged,
and cannot overrule anything they worked out. Switch it off and you still have the graph.

## Install

```bash
pip install "fleetlens[all] @ git+https://github.com/tata1mg/fleetlens.git@v0.3.1#subdirectory=packages/fleetlens"
fl index-all ~/repos --db fleetlens.db
fl serve --db fleetlens.db
```

## Documentation

- [Deploying a shared server](deployment.md), stdio for one engineer or HTTP for a team
- [Pinning an index](pinning.md), so an agent evaluation can be reproduced
- [MCP tool reference](tools.md), generated from the server's own registrations
- [The full README](https://github.com/tata1mg/fleetlens#readme), which is the canonical
  description of what this does and does not do
- [Roadmap](https://github.com/tata1mg/fleetlens/blob/main/ROADMAP.md) and
  [security notes](https://github.com/tata1mg/fleetlens/blob/main/SECURITY.md)

Apache 2.0. Source at [github.com/tata1mg/fleetlens](https://github.com/tata1mg/fleetlens).

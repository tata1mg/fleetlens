# Benchmark: does fleetlens help a coding agent?

This is a controlled study. We gave a coding agent a set of questions about a 16 service
codebase, ran it twice with everything held constant except whether the fleetlens MCP server
was available, and measured the difference.

The questions, ground truth, harness and scoring are in [`bench/`](bench/). Results where
fleetlens did not help are included below.

## Summary

| | Baseline | With fleetlens |
|---|---|---|
| F1 against ground truth | 0.788 | 0.888 |
| Fabricated services across 24 runs | 45 | 6 |
| New tokens (median) | 37,146 | 11,773 |
| Turns (median) | 14 | 5 |
| Wall clock (median) | 114s | 22s |

Accuracy went up, and the agent invented far fewer services that do not exist. It also used
about half the tokens and half the turns.

One result is worth reading before the rest: the size of the benefit depended heavily on
whether the agent discovered the MCP tools at all. That is covered in
[Tool adoption](#tool-adoption).

## Setup

### Arms

Two arms, differing only in whether the fleetlens MCP server was available.

| | A0 baseline | A1 fleetlens |
|---|---|---|
| Agent | Claude Code 2.1.278, model `sonnet` (`claude-sonnet-5`) | same |
| Tools | `Read`, `Grep`, `Glob`, `Bash` | same, plus `mcp__fleetlens__*` |
| MCP servers | `--strict-mcp-config`, none configured | `--strict-mcp-config`, fleetlens only |
| Corpus | same, read only | same, read only |
| Subagents | disabled (`--disallowedTools Agent,Task`) | same |
| Budget | 300s per question | same |

`--strict-mcp-config` is required. Without it both arms pick up whatever MCP servers the
operator has configured globally, which invalidates the comparison.

The baseline keeps read only `Bash` as well as `Grep`. A weak baseline is the first thing a
reader will question, so the baseline is as capable as we could reasonably make it.

48 runs total: 12 questions, 2 arms, 2 repetitions. Every run is a fresh session with no
memory of the others. Four runs execute concurrently.

### Corpora

| | Services | Visibility | Size |
|---|---|---|---|
| notifyone | 4 | [public](https://github.com/orgs/tata1mg/repositories?q=notifyone) | 335 Python, 100 TypeScript files |
| Main study | 16 unique, across 26 directories | private | 18,164 Python files, 520 MB |

Ten of the 26 directories are duplicate checkouts of a repository that is deployed more than
once (for example a web service and its background worker).

The agent searches a pruned, read only copy. We remove `venv/`, `node_modules/`, `.git/`,
`.context/` and `.claude/`. Three of those removals matter:

* `venv/`. One service carried 191 MB of site-packages. Having the baseline grep vendored
  dependencies is not representative, and it would make fleetlens look better than it is.
* `.context/`. This is fleetlens's own output. Leaving it in would give the baseline the
  answers.
* `.claude/`. Agent configuration, hooks, and agents from an earlier context extraction
  system, all of which would affect both arms.

There is one asymmetry. fleetlens indexes the repository in place with its virtualenv present,
because `scip-python` cannot resolve imports without one. fleetlens's own AST adapters skip
`venv/` regardless, so only the call graph benefits.

### Index under test

```
26 services, 148 cross-repo edges (6 async), built in about 14 minutes, no API key
```

The index is built deterministically from source. The optional LLM tier ran locally on
**qwen2.5-coder:14b via Ollama**, so no code left the machine. It contributed very little on
this corpus, because 99.6% of route paths here are string literals that the parsers read
directly.

## Questions

Twelve questions, each needing information from more than one repository. Ground truth is a
set of service names, so scoring is mechanical.

An earlier pilot used questions that could each be answered by grepping for one literal
string. That measures grep, not fleetlens, so those questions were replaced with ones that
need enumeration, reverse lookup, set intersection, ranking, or an exhaustive negative check.

| Category | Example |
|---|---|
| ranking | Which single service has the most other services depending on it over HTTP? |
| aggregation | List every service that `athena_service` sends HTTP requests to. |
| reverse | List every service that sends HTTP requests to `hr_digitisation`. |
| negative | Which services are never called by any other service? |
| intersection | Which services both expose HTTP routes and reference a message queue? |
| transitive | `patient_service_torpedo` is unavailable. Which services call it directly? |
| blind spot | Which service is not written in Python? |
| structural | Which directories are duplicate checkouts of the same repository? |

Ranking and negative questions were chosen deliberately. Both require checking all 16
services, which is cheap against an index and expensive with grep.

### Ground truth

Ground truth was derived independently of fleetlens, using regex over source and config. That
is a simpler and different method from fleetlens's AST and SCIP pipeline, so the two do not
share an implementation.

Questions and answers were committed before any run started. Git history shows the order.

Ground truth was written by the author of the tool being measured. It ships with `file:line`
evidence in [`bench/questions/c4.jsonl`](bench/questions/c4.jsonl) so it can be audited.
During review it was found to be wrong in one case, where it missed a dependency declared in
YAML that fleetlens had found correctly, and was corrected.

## Metrics

Answers are sets of service names, so two things can go wrong independently.

* **Precision.** Of the services the agent named, how many were correct.
* **Recall.** Of the services it should have named, how many it found.
* **F1.** The harmonic mean of precision and recall.

Here is a real run, answering "list every service `athena_service` calls", where ground truth
is 8 services:

```
named 12:  content_service, dexter, droplet, hr_digitisation, lab_buddy,
           labs_agg, patient_service_torpedo, theseus          (8 correct)
           corporate-service, offer-service-sanic,
           search-service, unified-cart                        (4 invented)

precision 8/12 = 0.67    recall 8/8 = 1.00    F1 = 0.80
```

The agent found every correct answer, and a third of what it returned was invented. Scoring on
recall alone would have marked that run perfect.

The harmonic mean prevents gaming from either direction. Naming all 26 services gives recall
1.00 and F1 0.47. Naming a single correct service gives precision 1.00 and F1 0.22.

Fabricated services are counted separately from F1. F1 treats a missed service and an invented
one as equally bad. For a dependency graph they are not: a dependency that does not exist
sends an engineer to look at code that has no relationship to the problem.

Token counts are **new tokens**, meaning input plus output plus cache creation. Cache reads are
excluded because they are the unchanged prompt being re-read on every API call. They grow with
turn count rather than with work done, and cost roughly ten times less. Including them
inflated one run from 150k to 1.24M.

## Results

### Aggregate, 24 runs per arm

| | Baseline | fleetlens | Change |
|---|---|---|---|
| F1 | 0.788 | 0.888 | +13% |
| Exact match rate | 0.500 | 0.542 | |
| Fabricated services | 45 | 6 | -87% |
| Runs containing a fabrication | 7/24 | 5/24 | |
| New tokens (mean) | 35,442 | 18,022 | -49% |
| Turns (mean) | 16.5 | 8.7 | -47% |
| Wall clock (mean) | 115.5s | 35.3s | -69% |
| Timeouts | 0 | 0 | |

Restricted to cross repository questions (22 runs per arm), F1 was 0.769 against 0.893, and
fabrications 45 against 5.

### By question

| Question | Category | A0 F1 | A1 F1 | A0 fabricated | A1 fabricated |
|---|---|---|---|---|---|
| q01 | ranking | 1.00 | 1.00 | 0 | 0 |
| q02 | aggregation | 0.80 | 0.94 | 8 | 2 |
| q03 | reverse | 1.00 | 1.00 | 0 | 0 |
| q04 | negative | 0.80 | 0.80 | 0 | 0 |
| q05 | intersection | 0.94 | 0.83 | 2 | 0 |
| q06 | ranking | 0.00 | 1.00 | 2 | 0 |
| q07 | blind spot | 1.00 | 0.83 | 0 | 1 |
| q08 | async join | 1.00 | 1.00 | 0 | 0 |
| q09 | transitive | 0.97 | 1.00 | 0 | 0 |
| q10 | structural | 0.40 | 0.40 | 0 | 0 |
| q11 | negative | 1.00 | 1.00 | 0 | 0 |
| q12 | aggregation | 0.55 | 0.85 | 33 | 3 |

### Fabricated dependencies

On q12, one baseline run returned 31 services that do not exist in the codebase, including
`arrowhead-external`, `clevertap-external`, `auth-service` and `payment-service`. It had found
config keys naming third party systems the fleet integrates with, and reported them as
services in the fleet.

The answer was well formed and internally consistent. Nothing about it signals a guess. This is
why fabrications are tracked separately from F1.

## Tool adoption

The study was run under two server configurations. The difference between them was larger than
the difference between the arms.

| Configuration | F1 | Fabricated | Tokens | Turns | Runs where fleetlens was used |
|---|---|---|---|---|---|
| 1, baseline | 0.733 | 18 | 30,281 | 16.2 | 0/24 |
| 1, fleetlens | 0.829 | 43 | 32,724 | 16.1 | 9/24 |
| 2, baseline | 0.788 | 45 | 35,442 | 16.5 | 0/24 |
| 2, fleetlens | 0.888 | 6 | 18,022 | 8.7 | 20/24 |

In configuration 1 the MCP server was connected in all 24 runs, and the agent did not call it
in 15 of them. It fell back to grep instead, including on reverse lookup, intersection and
aggregation questions.

Whether the agent called `ToolSearch` predicted this exactly. All 9 runs that searched for
tools went on to use fleetlens. None of the 15 runs that did not search ever found the tools.
Splitting configuration 1 by what actually happened:

| Configuration 1, fleetlens arm | Runs | F1 | Fabricated |
|---|---|---|---|
| fleetlens was called | 9 | 0.956 | 0 |
| fleetlens was not called | 15 | 0.753 | 43 |

Every fabrication in that arm came from a run where the tool was never called. Note that this
split is not randomised. The agent chose when to search for tools, and it tended to search on
ranking, negative and transitive questions, which are the ones where a graph is most obviously
the right instrument. Selection effects make 0.956 an optimistic figure.

Configuration 2 changed two things, and adoption went from 37.5% to 83%:

1. **Server instructions.** MCP clients surface a server's instructions even when individual
   tools are deferred behind a search step. The instructions now state which questions
   fleetlens answers, using the vocabulary a cross repository question would use.
2. **`get_service_graph`.** Returns every service and every edge in a single call. Fleet wide
   questions previously took 18 to 26 calls to walk the mesh service by service. Calls per run
   dropped from 5.2 to 2.3.

Adoption depends on the client. MCP clients differ in whether they load tool definitions into
context or defer them behind a search step. On a client that loads them eagerly, configuration
1 would probably have behaved like configuration 2.

## Where fleetlens did not help

* **q10, structural, 0.40 for both arms.** Asked which directories are duplicate checkouts of
  one repository, neither arm did well. `get_service_graph` now reports
  `likely_duplicate_deployments`, but identifying deploy variants remains weak.
* **q05 and q07.** The baseline matched or beat fleetlens. Set intersection and "which service
  is not Python" can be answered by direct inspection, and the index adds nothing.
* **q04.** Both arms tied at 0.80, making the same omission.
* **The baseline improved between configurations while unchanged**, from 0.733 to 0.788, with
  fabrications going from 18 to 45. Nothing about the baseline arm changed. This is run to run
  variance.
* **Precomputed counts can mislead.** `get_service_graph` computes fan-in, and its top answer
  was `hr_digitisation` with 17 inbound edges, where the correct answer is
  `patient_service_torpedo`. Duplicate checkouts are counted more than once. Earlier runs
  answered correctly because walking service by service let the agent deduplicate by name. The
  response now carries a `likely_duplicate_deployments` warning.

## Threats to validity

* Ground truth was written by the author of the tool under test. Independent derivation,
  pre-registration and published evidence reduce this but do not remove it. It was found wrong
  once and corrected.
* Ground truth and fleetlens both read the same config keys. A host declared in config but
  never actually called would appear as a false edge in both. This is a shared blind spot
  rather than independent confirmation.
* n = 2 per cell, against a measured noise floor of 34%. The noise floor comes from re-running
  the unchanged baseline arm: 34% mean deviation per question, 25% median, 167% maximum.
  Individual question differences are not meaningful at this sample size. The q06 swing between
  0.00 and 1.00 across configurations is variance.
* Wall clock is contended, because four runs execute concurrently. Turns and tokens are not
  affected.
* One corpus, one organisation, one framework family (Sanic and torpedo), one model, one day.
* The corpus is Python dominant. One service is written in Ruby and is invisible to fleetlens's
  parsers, despite having the second highest fan-in in the fleet.
* The questions were written with knowledge of what fleetlens does. They target cross repository
  reasoning because that is the claim under test, but that is not a neutral selection.

## Reproducing this

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...          # or ANTHROPIC_API_KEY

python bench/make_questions_c4.py --out bench/questions/c4.jsonl
python bench/run.py --questions bench/questions/c4.jsonl \
    --corpus /path/to/corpus --db fleet.db --reps 2 --workers 4
python bench/score.py --questions bench/questions/c4.jsonl \
    --runs "bench/results/runs_*.jsonl"
```

[`bench/README.md`](bench/README.md) documents the protocol, the corpus hygiene rules, and the
measurement problems we hit, including two that produced wrong numbers before they were caught.

Claude Code reports dollar figures computed at list prices (`costBasis: "list"`). Under
subscription authentication nothing is billed, so tokens are the measured quantity.

# mind1 benchmark

Measures what mind1 changes for a coding agent answering cross-repo questions:
**correctness, hallucination rate, token cost, and wall-clock.**

## The comparison

Two arms differing by exactly one variable: whether the mind1 MCP server is available.

| | A0 baseline | A1 mind1 |
|---|---|---|
| Agent | Claude (pinned) | Claude (same) |
| Tools | `Read,Grep,Glob,Bash` | same **+ `mcp__mind1__*`** |
| MCP | `--strict-mcp-config`, empty | `--strict-mcp-config`, mind1 only |
| Corpus | identical, read-only | identical, read-only |

`--strict-mcp-config` is mandatory. Without it both arms inherit whatever MCP servers the
operator has configured globally, and the comparison means nothing.

The baseline gets **read-only Bash as well as grep** on purpose. A weak baseline is the
first thing a skeptic attacks; any mind1 win here is a conservative one.

## Corpus hygiene

Corpora are pruned copies, locked read-only (`chmod -R a-w`). Removed before every run:

- `venv/`, `node_modules/`. notifyone-core alone is 191M of site-packages. Letting the
  baseline grep that is unrepresentative noise that *flatters* mind1.
- `.context/`, which is mind1's own output. Leaving it would hand the baseline mind1's
  answers for free.
- `.claude/` and `CLAUDE.md`. Agent configs, hooks and prior context-extraction agents that
  would contaminate both arms and can inject instructions.

Re-verify after any indexing run: building an index regenerates `.context/`.

Note one deliberate asymmetry: mind1 indexes the repo **in place, with its virtualenv
present**, because `scip-python` cannot resolve imports without one. The agent searches the
same source tree minus the virtualenv. This is how mind1 is really used, and mind1's
own AST adapters skip `venv/` regardless, so only the call graph benefits.

## Ground truth

Pre-registered: questions and answers are committed **before** any arm runs, with git history
as proof of order. Each answer carries `file:line` evidence and is derived from reading
source, never from mind1 output, which would make the study circular.

**Stated limitation:** ground truth was authored by the tool's author. The file ships with
its evidence so any reader can audit it.

## Running

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...      # or ANTHROPIC_API_KEY
python bench/run.py --questions bench/questions/c1.jsonl \
    --corpus /path/to/corpus/c1 --db /path/to/c1.db --reps 3
python bench/score.py --questions bench/questions/c1.jsonl \
    --runs "bench/results/runs_*.jsonl"
```

## Metrics

**Tokens are the measured quantity.** Precision / recall / F1 against ground-truth sets ·
**hallucinations** (asserted entities neither correct nor tolerable) · `billable_tokens`
summed across every assistant message · turns · wall-clock · `mind1_calls` (proof the
treatment arm actually used the treatment). Index build cost is reported separately and
amortized, never folded into per-question cost.

### Do not report dollars as spend

Claude Code emits `total_cost_usd`, but `modelUsage` marks it `"costBasis": "list"`. It is
token counts multiplied by published list prices. Under subscription auth
(`CLAUDE_CODE_OAUTH_TOKEN`) **no dollars are billed**; usage draws against subscription quota
and rate limits. The field is recorded as `cost_usd_list_equivalent` and is only meaningful
to readers who are on API billing.

Two measurement traps, both hit during the pilot:

1. The `result` event's `usage` is the **final turn only**. Using it understated a multi-turn
   run by ~19x. Sum usage across assistant messages from `--output-format stream-json`.
2. Subagents (`Agent`) fold their tokens into the parent and add large variance, so both arms
   run with `--disallowedTools Agent,ScheduleWakeup,Task`.

### Noise floor

Measured by running the **unchanged** baseline arm twice: mean |delta| 34%, median 25%, max
167% per question, +15% in aggregate. Any claimed effect smaller than that needs enough
repetitions to resolve it, hence `--reps 5` and reporting medians with spread, not means.

## Pre-registered hypotheses

- **H1** mind1 cuts tokens per question, most on cross-repo questions.
- **H2** mind1 reduces hallucinated cross-service edges.
- **H3** correctness gains concentrate in async/queue questions and approach zero
  within a single repo.
- **H4** qwen2.5-coder:14b gap-filling helps materially on notifyone only; on the
  literal-routing corpora it should add close to nothing.

H4 predicts a null result on two of three corpora. Publishing that is the point.

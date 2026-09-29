"""Benchmark harness — run one question set through both arms and record raw results.

Arms differ by exactly one variable: whether the fleetlens MCP server is available.
Everything else (model, tools, corpus, prompt, turn budget) is identical.

    A0 baseline : Read/Grep/Glob/Bash over the corpus, no MCP servers
    A1 fleetlens: the same, plus mcp__fleetlens__*

`--strict-mcp-config` is mandatory: without it both arms would silently inherit whatever
MCP servers the operator has configured globally, which would invalidate the comparison.

Usage:
    python bench/run.py --questions bench/questions/c1.jsonl \
        --corpus /path/to/corpus/c1 --db /path/to/c1.db --reps 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ANSWER_INSTRUCTION = """

When you have finished investigating, output your final answer as a JSON code block, exactly like this:

```json
{"answer": ["item1", "item2"], "reasoning": "one sentence"}
```

"answer" must be a JSON list of strings, and may be empty if the correct answer is "none".
Output the JSON block once, at the very end."""

BASE_TOOLS = "Read,Grep,Glob,Bash"
FLEETLENS_TOOLS = BASE_TOOLS + ",mcp__fleetlens__*"


def arm_specs(db: Path, fl_bin: str) -> dict:
    return {
        "A0_baseline": {
            "mcp": {"mcpServers": {}},
            "tools": BASE_TOOLS,
        },
        "A1_fleetlens": {
            "mcp": {"mcpServers": {"fleetlens": {
                "type": "stdio",
                "command": fl_bin,
                "args": ["serve", "--db", str(db)],
                "env": {},
            }}},
            "tools": FLEETLENS_TOOLS,
        },
    }


def extract_answer(text: str):
    """Last ```json block with an "answer" key wins; None if the agent never emitted one."""
    for block in reversed(re.findall(r"```json\s*(.*?)```", text or "", re.S)):
        try:
            obj = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "answer" in obj:
            return obj
    return None


def parse_stream(raw: str):
    """Read the authoritative cumulative usage, and collect the tool-call log.

    Token accounting comes from the result event's `modelUsage`, not from summing per-message
    usage. Summing looked right on a short run but drifted on long ones (duplicate message
    events double-count) and always undercounted output. `modelUsage` is what Claude Code
    itself bills from.

    Note what the numbers mean. `cacheRead` is the unchanged prompt re-read on every API
    call, so it grows with TURNS rather than with work done, and is priced about 10x below
    fresh input. Adding it to a headline "tokens consumed" figure is misleading: a 28-turn
    run showed 1.24M total of which 1.09M was cache re-reads. `new_tokens` below is the
    honest measure of what the model actually had to process.
    """
    final, tools, mcp_status = None, [], None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("mcp_servers") is not None:
            mcp_status = ev["mcp_servers"]
        if ev.get("type") == "result":
            final = ev
        msg = ev.get("message")
        if isinstance(msg, dict):
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools.append(c.get("name"))

    tot = {"input_tokens": 0, "output_tokens": 0,
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for d in ((final or {}).get("modelUsage") or {}).values():
        tot["input_tokens"] += d.get("inputTokens") or 0
        tot["output_tokens"] += d.get("outputTokens") or 0
        tot["cache_read_input_tokens"] += d.get("cacheReadInputTokens") or 0
        tot["cache_creation_input_tokens"] += d.get("cacheCreationInputTokens") or 0
    return final, tot, tools, mcp_status


def run_one(question: dict, arm: str, spec: dict, corpus: Path, cwd: Path,
            model: str, timeout: int) -> dict:
    mcp_path = (cwd / f"mcp_{arm}.json").resolve()
    mcp_path.write_text(json.dumps(spec["mcp"]))

    prompt = question["prompt"] + ANSWER_INSTRUCTION
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--mcp-config", str(mcp_path),
        "--allowedTools", spec["tools"],
        "--add-dir", str(corpus),
        "--model", model,
        # both arms: no subagents. Agent spawning is legitimate but adds large variance and
        # folds subagent tokens into the parent's count, swamping the effect under test.
        "--disallowedTools", "Agent,ScheduleWakeup,Task",
    ]
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        raw = proc.stdout
    except subprocess.TimeoutExpired:
        return {"id": question["id"], "arm": arm, "error": "timeout",
                "wall_s": time.time() - started}

    payload, usage, tools_called, mcp_status = parse_stream(raw)
    if payload is None:
        return {"id": question["id"], "arm": arm, "error": "unparseable_output",
                "stdout_head": raw[:500], "stderr_head": proc.stderr[:500],
                "wall_s": time.time() - started}
    return {
        "id": question["id"],
        "arm": arm,
        "model": model,
        "result_text": payload.get("result"),
        "parsed_answer": extract_answer(payload.get("result")),
        "is_error": payload.get("is_error"),
        # list-price equivalent, NOT a charge: subscription auth bills no dollars
        "cost_usd_list_equivalent": payload.get("total_cost_usd"),
        "num_turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
        "duration_api_ms": payload.get("duration_api_ms"),
        # summed across every assistant message (see parse_stream)
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_input_tokens": usage["cache_read_input_tokens"],
        "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
        # what the model actually had to process; excludes cache re-reads, which scale with
        # turn count rather than work and are priced ~10x lower
        "new_tokens": usage["input_tokens"] + usage["output_tokens"]
                      + usage["cache_creation_input_tokens"],
        "billable_tokens": usage["input_tokens"] + usage["output_tokens"]
                           + usage["cache_creation_input_tokens"] + usage["cache_read_input_tokens"],
        "tools_called": tools_called,
        "fleetlens_calls": sum(1 for t in tools_called if str(t).startswith("mcp__fleetlens__")),
        "mcp_servers": mcp_status,
        "permission_denials": payload.get("permission_denials"),
        "session_id": payload.get("session_id"),
        "wall_s": round(time.time() - started, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="bench/results")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--fl-bin", default="fl")
    ap.add_argument("--only", default="", help="comma-separated question ids")
    ap.add_argument("--workers", type=int, default=1,
                    help="run N cases concurrently. Each case is a separate subprocess and "
                         "fresh session, so results stay independent — but wall-clock becomes "
                         "contended, so treat wall_s as indicative and prefer turns/tokens.")
    args = ap.parse_args()

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        print("run.py: no CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in env", file=sys.stderr)
        return 2

    questions = [json.loads(ln) for ln in Path(args.questions).read_text().splitlines()
                 if ln.strip()]
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        questions = [q for q in questions if q["id"] in keep]

    corpus, db = Path(args.corpus).resolve(), Path(args.db).resolve()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # a neutral, writable cwd so neither arm starts inside the corpus or the fleetlens repo
    cwd = (out_dir / "_scratch").resolve()
    cwd.mkdir(exist_ok=True)

    specs = arm_specs(db, args.fl_bin)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_path = out_dir / f"runs_{stamp}.jsonl"

    cases = [(rep, q, arm, spec)
             for rep in range(args.reps)
             for q in questions
             for arm, spec in specs.items()]
    total = len(cases)
    lock = threading.Lock()
    counter = {"done": 0}

    with results_path.open("w") as fh:
        def execute(case):
            rep, q, arm, spec = case
            # per-worker cwd so concurrent runs never share an MCP config file
            wdir = cwd / f"w{threading.get_ident()}"
            wdir.mkdir(exist_ok=True)
            rec = run_one(q, arm, spec, corpus, wdir, args.model, args.timeout)
            rec["rep"] = rep
            rec["concurrency"] = args.workers
            with lock:
                counter["done"] += 1
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                status = rec.get("error") or ("ok" if rec.get("parsed_answer") else "no-json-answer")
                print(f"[{counter['done']}/{total}] {q['id']} {arm} rep{rep}  {status}  "
                      f"{rec.get('num_turns')} turns  {rec.get('wall_s')}s", flush=True)
            return rec

        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(execute, cases))
        else:
            for case in cases:
                execute(case)

    print(f"\nwrote {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

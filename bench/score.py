"""Score benchmark runs against the pre-registered ground truth.

Tokens are the measured quantity. The dollar column is a LIST-PRICE EQUIVALENT computed by
Claude Code (`costBasis: "list"`); under subscription auth no dollars are billed at all, so
never report it as spend.

Set-valued answers are scored mechanically: precision, recall, F1, and a hallucination
count (asserted entities that are neither ground truth nor explicitly acceptable).
Normalisation is deliberately forgiving about surface form — "notifyone-core",
"Notifyone Core" and "notifyone_core" are the same entity — and strict about identity.

    python bench/score.py --questions bench/questions/c1.jsonl --runs bench/results/runs_*.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path


def norm(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"^(service:|interface:)", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9/{}:.-]", "", s)
    return s.strip("-/")


def keys(s: str, entity: str) -> set:
    """Match keys for one asserted entity.

    Symbols are routinely written either bare (`handle_request`) or class-qualified
    (`NotificationRequest.handle_request`); both name the same function, so a symbol also
    matches on its final dotted segment. Services, endpoints and queues match exactly —
    there, a partial match would be a different entity.
    """
    n = norm(s)
    if entity == "symbol" and "." in n:
        return {n, n.rsplit(".", 1)[-1]}
    return {n}


def resolve(got_raw, want_raw, entity, aliases=None):
    """Collapse asserted entities onto ground truth via shared match keys.

    `aliases` maps a ground-truth name to other names for the SAME thing — needed where
    duplicate checkouts of one repository appear as several directories, so naming any one
    of them is correct.
    """
    aliases = aliases or {}
    want_keys = {g: keys(g, entity) | {k for a in aliases.get(g, []) for k in keys(a, entity)}
                 for g in want_raw}
    matched_gt, unmatched = set(), []
    for a in got_raw:
        ak = keys(a, entity)
        hit = next((g for g, wk in want_keys.items() if ak & wk), None)
        if hit is not None:
            matched_gt.add(hit)
        else:
            unmatched.append(norm(a))
    return matched_gt, unmatched


def score_one(answer, gt: list, acceptable: list, entity: str = "service",
              aliases: dict | None = None) -> dict:
    if answer is None:
        return {"scored": False}
    got_raw = [a for a in answer if str(a).strip()]
    matched_gt, unmatched = resolve(got_raw, gt, entity, aliases)
    ok_extra = {k for a in acceptable for k in keys(a, entity)}

    want = {norm(g) for g in gt}

    tp = len(matched_gt)
    fp_all = [u for u in unmatched]
    halluc = [u for u in fp_all if u not in ok_extra]   # wrong AND not merely tolerable
    fn = {norm(g) for g in gt if g not in matched_gt}

    # aliases collapse: naming two directories of the same service is one correct answer,
    # not one right and one wrong. Denominator is distinct resolved entities.
    distinct = tp + len(set(fp_all))
    precision = tp / distinct if distinct else (1.0 if not want else 0.0)
    recall = tp / len(gt) if gt else (1.0 if not got_raw else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "scored": True, "tp": tp, "fp": len(fp_all), "fn": len(fn),
        "hallucinations": len(halluc), "hallucinated": sorted(halluc),
        "missed": sorted(fn),
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "exact": not halluc and not fn,
    }


def agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(statistics.mean(vals), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    qs = {}
    for line in Path(args.questions).read_text().splitlines():
        if line.strip():
            q = json.loads(line)
            qs[q["id"]] = q

    paths = []
    for pat in args.runs:
        paths.extend(glob.glob(pat))
    runs = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                runs.append(json.loads(line))

    scored = []
    for r in runs:
        q = qs.get(r["id"])
        if not q:
            continue
        ans = (r.get("parsed_answer") or {}).get("answer") if r.get("parsed_answer") else None
        if r.get("error") == "timeout":
            # A run that never answered is a failure to answer, not missing data. Scoring it
            # as absent would quietly drop the baseline's worst cases from the average.
            s = {"scored": True, "tp": 0, "fp": 0, "fn": len(q["ground_truth"]),
                 "hallucinations": 0, "hallucinated": [], "missed": [],
                 "precision": 0.0, "recall": 0.0, "f1": 0.0, "exact": False, "timeout": True}
        else:
            s = score_one(ans, q["ground_truth"], q.get("acceptable", []),
                          q.get("entity", "service"), q.get("aliases"))
        scored.append({**r, **s, "category": q["category"], "cross_repo": q["cross_repo"]})

    by_arm = defaultdict(list)
    for s in scored:
        by_arm[s["arm"]].append(s)

    print("=" * 78)
    print(f"{'arm':<15}{'n':>4}{'F1':>8}{'exact':>8}{'halluc':>8}{'new_tok':>9}{'cacheRd':>9}{'turns':>7}{'sec':>7}{'$list':>9}{'t/o':>5}{'fl_use':>8}")
    print("=" * 78)
    summary = {}
    for arm in sorted(by_arm):
        rs = by_arm[arm]
        ok = [r for r in rs if r.get("scored")]
        row = {
            "n": len(rs),
            "answered": len(ok),
            "f1": agg([r["f1"] for r in ok]),
            "exact_rate": agg([1.0 if r["exact"] else 0.0 for r in ok]),
            "hallucinations_total": sum(r["hallucinations"] for r in ok),
            "timeouts": sum(1 for r in rs if r.get("error") == "timeout"),
            "cost_usd_list_mean": agg([r.get("cost_usd_list_equivalent") for r in rs]),
            "cost_usd_list_total": round(sum(r.get("cost_usd_list_equivalent") or 0 for r in rs), 4),
            "new_tokens_mean": agg([r.get("new_tokens") for r in rs]),
            "cache_read_mean_": agg([r.get("cache_read_input_tokens") for r in rs]),
            "fleetlens_calls_mean": agg([r.get("fleetlens_calls") for r in rs]),
            "runs_using_fleetlens": sum(1 for r in rs if (r.get("fleetlens_calls") or 0) > 0),
            "turns_mean": agg([r.get("num_turns") for r in rs]),
            "wall_s_mean": agg([r.get("wall_s") for r in rs]),
            "input_tokens_mean": agg([r.get("input_tokens") for r in rs]),
            "cache_read_mean": agg([r.get("cache_read_input_tokens") for r in rs]),
            "cache_creation_mean": agg([r.get("cache_creation_input_tokens") for r in rs]),
        }
        summary[arm] = row
        print(f"{arm:<15}{row['n']:>4}{row['f1'] or 0:>8.3f}{row['exact_rate'] or 0:>8.3f}"
              f"{row['hallucinations_total']:>8}{row['new_tokens_mean'] or 0:>9.0f}{row['cache_read_mean_'] or 0:>9.0f}"
              f"{row['turns_mean'] or 0:>7.1f}{row['wall_s_mean'] or 0:>7.1f}"
              f"{row['cost_usd_list_mean'] or 0:>9.4f}{row['timeouts']:>5}"
              f"{row['runs_using_fleetlens']:>5}/{row['n']}")

    print("\n-- cross-repo questions only --")
    for arm in sorted(by_arm):
        ok = [r for r in by_arm[arm] if r.get("scored") and r["cross_repo"]]
        if ok:
            print(f"  {arm:<15} F1={agg([r['f1'] for r in ok]):.3f}  "
                  f"halluc={sum(r['hallucinations'] for r in ok)}  n={len(ok)}")

    print("\n-- per question (F1 by arm) --")
    by_q = defaultdict(dict)
    for s in scored:
        if s.get("scored"):
            by_q[s["id"]].setdefault(s["arm"], []).append(s["f1"])
    for qid in sorted(by_q):
        cells = "  ".join(f"{a}={agg(v):.2f}" for a, v in sorted(by_q[qid].items()))
        print(f"  {qid:<10} {qs[qid]['category']:<13} {cells}")

    misses = [s for s in scored if s.get("scored") and s["hallucinations"]]
    if misses:
        print("\n-- hallucinations --")
        for s in misses:
            print(f"  {s['id']:<10} {s['arm']:<15} {s['hallucinated']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"summary": summary, "runs": scored}, indent=2) + "\n")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

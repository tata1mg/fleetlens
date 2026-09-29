"""Generate the C4 (diagno + hih) question set and its ground truth.

Ground truth is derived here, independently of fleetlens, by simple regex over source and
config. A deliberately different and simpler method than fleetlens's AST/SCIP pipeline, so
the two do not share an implementation and cannot share a blind spot by construction.

Questions target what a single grep cannot do cheaply: exhaustive enumeration over 16 repos,
reverse lookups, set intersections, ranking, and negative claims. The pilot showed that
questions answerable by locating one literal string measure grep, not fleetlens.

    python bench/make_questions_c4.py --out bench/questions/c4.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import subprocess

# Corpus roots are supplied per run; there are no machine-specific paths in this file.
ROOTS: dict = {}
SKIP = {"venv", ".venv", "node_modules", ".git", "__pycache__", ".context", ".claude"}

# JSON-quoted and YAML-bare forms. Missing the YAML form understated ground truth on the
# first pass: droplet's settings.yml declares a dependency fleetlens found and we did not.
HOSTKEY = re.compile(r"""["']([A-Za-z0-9_.\-]*(?:HOST|URL|ENDPOINT|BASE_URI)[A-Za-z0-9_.\-]*)["']\s*:\s*["']([^"']+)["']""", re.I)
HOSTKEY_YAML = re.compile(r"""^\s*([A-Za-z0-9_.\-]*(?:HOST|URL|ENDPOINT|BASE_URI)[A-Za-z0-9_.\-]*)\s*:\s*['"]?([^'"\n#]+)""", re.I | re.M)
ROUTE = re.compile(r"""@(\w+)\.(route|get|post|put|patch|delete)\(\s*["']([^"']+)["']""")
QUEUE = re.compile(r"""["']([A-Za-z0-9_.\-]*(?:QUEUE|TOPIC)[A-Za-z0-9_.\-]*)["']\s*:\s*["']([^"']+)["']""", re.I)
ENVPFX = re.compile(r"^(<\s*env_name\s*>|stag|staging|prod|production|pluto|neptune)-")

PREAMBLE = ("This codebase contains multiple independent microservice repositories as "
            "top-level directories. Some directories are duplicate checkouts of the same "
            "repository (identical code, different deployment); treat those as ONE service "
            "and name any one of them. ")


def derive():
    uniq, dirs = {}, collections.defaultdict(list)
    for grp, root in ROOTS.items():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            remote = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"],
                                    capture_output=True, text=True).stdout.strip()
            u = remote.split("/")[-1].replace(".git", "") or d.name
            dirs[u].append(d.name)
            uniq.setdefault(u, d)

    names = sorted(uniq)
    edges, routes, queues, langs = set(), collections.defaultdict(set), collections.defaultdict(set), {}
    for u, d in uniq.items():
        def count(ext):
            return sum(1 for f in d.rglob(ext) if not any(s in f.parts for s in SKIP))
        langs[u] = {"py": count("*.py"), "rb": count("*.rb"), "ts": count("*.ts")}
        for f in d.rglob("*.py"):
            if any(s in f.parts for s in SKIP):
                continue
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            for m in ROUTE.finditer(t):
                routes[u].add(m.group(3))
        for f in (list(d.rglob("config*.json")) + list(d.rglob("*.yml"))
                  + list(d.rglob("*.yaml")) + list(d.rglob("config/*.yml"))):
            if any(s in f.parts for s in SKIP):
                continue
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            for rx in (HOSTKEY, HOSTKEY_YAML):
                for m in rx.finditer(t):
                    v = m.group(2).lower()
                    for o in names:
                        if o != u and (o.lower() in v or o.replace("_", "-") in v):
                            edges.add((u, o))
            for m in QUEUE.finditer(t):
                queues[u].add(ENVPFX.sub("", m.group(2)))
    return uniq, dirs, names, edges, routes, queues, langs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/questions/c4.jsonl")
    ap.add_argument("--root", action="append", required=True, metavar="NAME=PATH",
                    help="corpus root, repeatable, e.g. --root teamA=/path/to/repos")
    args = ap.parse_args()
    for spec in args.root:
        name, _, path = spec.partition("=")
        ROOTS[name or pathlib.Path(path).name] = pathlib.Path(path)

    uniq, dirs, names, edges, routes, queues, langs = derive()
    alias = {u: [d for d in ds] for u, ds in dirs.items()}
    fanin = collections.Counter(b for a, b in edges)
    fanout = collections.Counter(a for a, b in edges)
    shared = collections.defaultdict(set)
    for u, qs in queues.items():
        for q in qs:
            shared[q].add(u)

    def Q(qid, category, entity, prompt, gt, evidence, cross_repo=True, acceptable=None):
        return {"id": qid, "corpus": "c4", "category": category, "cross_repo": cross_repo,
                "entity": entity, "prompt": PREAMBLE + prompt, "ground_truth": gt,
                "acceptable": acceptable or [],
                "aliases": {g: [a for a in alias.get(g, []) if a != g] for g in gt},
                "evidence": evidence}

    qs = []
    top = fanin.most_common(1)[0]
    qs.append(Q("c4-q01", "ranking", "service",
                "Across the whole codebase, which single service has the MOST other services "
                "depending on it over HTTP? Answer with just that one service name.",
                [top[0]], [f"fan-in={top[1]} from *HOST/*URL config keys naming it across all repos"]))

    qs.append(Q("c4-q02", "aggregation", "service",
                "List EVERY service that athena_service sends HTTP requests to.",
                sorted(b for a, b in edges if a == "athena_service"),
                ["athena_service config *HOST/*URL keys naming other corpus services"]))

    qs.append(Q("c4-q03", "reverse", "service",
                "List EVERY service that sends HTTP requests TO hr_digitisation.",
                sorted(a for a, b in edges if b == "hr_digitisation"),
                ["reverse index over all repos' *HOST/*URL config keys"]))

    qs.append(Q("c4-q04", "negative", "service",
                "Which services are never called over HTTP by any other service in this "
                "codebase (zero inbound dependencies)?",
                sorted(u for u in names if fanin[u] == 0),
                ["services absent from every other repo's *HOST/*URL config values"]))

    qs.append(Q("c4-q05", "intersection", "service",
                "Which services BOTH expose HTTP routes AND reference at least one message "
                "queue or topic in their configuration?",
                sorted(u for u in names if routes[u] and queues[u]),
                ["route decorators in *.py AND QUEUE/TOPIC-shaped config keys"]))

    qs.append(Q("c4-q06", "ranking", "service",
                "Which service exposes the LARGEST number of distinct HTTP route paths? "
                "Answer with just that one service name.",
                [max(names, key=lambda u: len(routes[u]))],
                ["route-path counts: " + ", ".join(f"{u}={len(routes[u])}" for u in sorted(names, key=lambda x: -len(routes[x]))[:4])]))

    qs.append(Q("c4-q07", "blind_spot", "service",
                "Which service in this codebase is NOT written in Python? Name it and state "
                "its language. Answer with the service name only in the JSON list.",
                [u for u in names if langs[u]["rb"] > langs[u]["py"]],
                [f"droplet: {langs.get('droplet')} — Ruby, no Python sources"], cross_repo=False))

    qs.append(Q("c4-q08", "async_join", "service",
                "Two services communicate through a queue whose name contains "
                "'hr_digitisation-smart_report'. Which two services are they?",
                sorted(shared.get("hr_digitisation-smart_report", set())),
                ["queue value shared between exactly these repos' configs (env prefix stripped)"]))

    qs.append(Q("c4-q09", "transitive", "service",
                "patient_service_torpedo is unavailable. Which services call it DIRECTLY "
                "over HTTP? List only the direct callers.",
                sorted(a for a, b in edges if b == "patient_service_torpedo"),
                ["reverse index; fan-in=10"]))

    qs.append(Q("c4-q10", "structural", "service",
                "Several top-level directories are duplicate checkouts of the SAME git "
                "repository. Name every directory that belongs to the dexter repository.",
                sorted(dirs["dexter"]),
                ["identical git remote and HEAD SHA across these directories"],
                acceptable=[]))

    qs.append(Q("c4-q11", "negative", "service",
                "Which service neither calls any other service nor is called by any other "
                "service in this codebase (completely isolated)?",
                sorted(u for u in names if fanin[u] == 0 and fanout[u] == 0),
                ["zero inbound and zero outbound edges"]))

    qs.append(Q("c4-q12", "aggregation", "service",
                "List every service that dexter sends HTTP requests to.",
                sorted(b for a, b in edges if a == "dexter"),
                ["dexter config *HOST/*URL keys"]))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(q) for q in qs) + "\n")
    print(f"wrote {len(qs)} questions -> {out}")
    print(f"corpus: {len(names)} unique repos, {sum(len(v) for v in dirs.values())} directories, "
          f"{len(edges)} HTTP edges")
    for q in qs:
        print(f"  {q['id']:<9}{q['category']:<13}gt={len(q['ground_truth'])}  {q['ground_truth']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

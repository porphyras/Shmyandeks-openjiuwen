"""Stability over repeated bench runs of the same version.

python3 -I bench/tools/stability.py SET_DIR RUN_DIR [RUN_DIR ...]
Prints reward per run (min/mean/max), tokens (min/max), pass^k (feasible tasks solved in every
run) and the "blinking" tasks (solved in some runs only) with the reason seen in the failing
runs' traces."""
import json, os, statistics as st, sys
from collections import Counter, defaultdict

set_dir, runs = sys.argv[1], sys.argv[2:]
keys = json.load(open(os.path.join(set_dir, "keys.json")))
feasible = [t for t, k in keys.items() if k["feasible"]]


def load(run):
    g = json.load(open(os.path.join(run, "grade.json")))
    items = g.get("tasks") if isinstance(g, dict) and "tasks" in g else g
    res = {}
    if isinstance(items, dict):
        items = [dict(v, task_id=k) for k, v in items.items() if isinstance(v, dict)]
    for r in items:
        if isinstance(r, dict) and "task_id" in r:
            res[r["task_id"]] = r
    stats = json.load(open(os.path.join(run, "stats.json")))
    trace = defaultdict(list)
    for line in open(os.path.join(run, "trace.jsonl")):
        r = json.loads(line)
        tid = r.get("task") or r.get("tag")
        if tid:
            trace[tid].append(r)
    return res, stats.get("spent", 0), trace


def correct(r):
    for k in ("correct", "ok", "pass"):
        if k in r:
            return bool(r[k])
    return bool(r.get("score", 0))


def reason(evs, gr):
    notes = " ".join(str(e.get("note", "")) for e in evs if e.get("ev") == "task")
    if any(e.get("ev") == "reply" and e.get("finish") == "length" for e in evs):
        return "cut at max_tokens"
    if any(e.get("ev") == "llm_error" for e in evs):
        return "proxy error/timeout"
    if "Program failed" in notes or "timed out" in notes:
        return "program crashed"
    if "tests still failing" in notes:
        return "code fix failed"
    if "hidden" in str(gr.get("note", "")):
        return "hidden tests fail (overfit)"
    statuses = [e.get("status") for e in evs if e.get("ev") == "task"]
    if "infeasible" in statuses:
        return "declared infeasible"
    if gr.get("answer") not in (None, "", "-"):
        return "wrong answer"
    return "no answer"


loaded = [load(r) for r in runs]
rewards, tokens = [], []
for res, spent, _ in loaded:
    rewards.append(sum(keys[t]["reward"] for t in feasible if t in res and correct(res[t])))
    tokens.append(spent)
total = sum(keys[t]["reward"] for t in feasible)
solved_all = [t for t in feasible if all(t in res and correct(res[t]) for res, _, _ in loaded)]
blink = [t for t in feasible if t not in solved_all and any(t in res and correct(res[t]) for res, _, _ in loaded)]
never = [t for t in feasible if not any(t in res and correct(res[t]) for res, _, _ in loaded)]
print("%s: reward per run %s of %d (min %d, mean %.1f, max %d); tokens %d..%d; pass^%d %d/%d; blinking %d; never %d"
      % (os.path.basename(set_dir.rstrip("/")), rewards, total, min(rewards), st.mean(rewards), max(rewards),
         min(tokens), max(tokens), len(runs), len(solved_all), len(feasible), len(blink), len(never)))
cats = Counter()
for t in blink:
    rs = []
    for (res, _, tr), run in zip(loaded, runs):
        if not (t in res and correct(res[t])):
            rs.append(reason(tr.get(t, []), res.get(t, {})))
    cats.update(rs)
    print("  blink %-22s %-14s %s" % (t, keys[t].get("kind", ""), "; ".join(rs)))
for t in never:
    res, _, tr = loaded[0]
    print("  never %-22s %-14s %s" % (t, keys[t].get("kind", ""), reason(tr.get(t, []), res.get(t, {}))))
if cats:
    print("  blink reasons:", dict(cats))

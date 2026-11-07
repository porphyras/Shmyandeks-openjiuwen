"""Offline simulation of the planner under a hard budget, on real traces (no proxy calls).

python3 -I bench/tools/budget_sim.py SET_DIR RUN_DIR [RUN_DIR ...] --budgets 3000,5000,10000

For each run, every task has the forecast made before solving (token_estimates.json),
its actual cost and whether it was solved correctly (grade.json). The simulation replays the
harness policy: tasks in order of reward * p / forecast, a task starts only if the remaining
budget minus the reserve of tasks in flight covers its forecast, the proxy stops answering
once the budget is crossed (the task in flight is lost). Compared with the 0/1 knapsack
optimum over the actual costs (an oracle knowing outcomes and costs in advance)."""
import argparse, json, os


def load(run, keys):
    est = json.load(open(os.path.join(run, "out", "token_estimates.json")))
    g = json.load(open(os.path.join(run, "grade.json")))
    ok = {r["task_id"]: bool(r.get("correct")) for r in g["tasks"]}
    items = []
    for name, v in est.items():
        if "actual" not in v:
            continue
        r = keys[name]["reward"] if name in keys else 0
        items.append({"t": name, "r": r, "p": v.get("p_success", 0.9), "est": max(1.0, v["estimate"]),
                      "p90": max(1.0, v.get("estimate_p90", v["estimate"] * 2)),
                      "act": float(v["actual"]), "win": ok.get(name, False), "kind": v.get("kind", "")})
    return items


def policy(items, budget, workers=5, order="est"):
    """Greedy by r*p/cost with in-flight reserve; tasks run in parallel groups of `workers`."""
    free = [x for x in items if x["act"] == 0]  # fast path / free verdicts: done before any call
    queue = sorted([x for x in items if x["act"] > 0], key=lambda x: -(x["r"] * x["p"] / x[order]))
    spent, score, started, skipped = 0.0, sum(x["r"] for x in free if x["win"]), len(free), 0
    flight = []
    for it in queue:
        reserve = sum(f["est"] for f in flight)
        if budget - spent - reserve < it["est"]:
            skipped += 1
            continue
        flight.append(it)
        started += 1
        if len(flight) == workers:
            for f in flight:
                if spent + f["act"] > budget:
                    return score, spent, started, skipped, True
                spent += f["act"]
                score += f["r"] if f["win"] else 0
            flight = []
    for f in flight:
        if spent + f["act"] > budget:
            return score, spent, started, skipped, True
        spent += f["act"]
        score += f["r"] if f["win"] else 0
    return score, spent, started, skipped, False


def knapsack(items, budget):
    vals = [(int(round(x["act"])), x["r"]) for x in items if x["win"]]
    best = [0] * (int(budget) + 1)
    for c, v in vals:
        if c == 0:
            best = [b + v for b in best]
            continue
        for b in range(int(budget), c - 1, -1):
            if best[b - c] + v > best[b]:
                best[b] = best[b - c] + v
    return best[int(budget)]


ORDER = os.environ.get("SIM_ORDER", "est")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir")
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--budgets", default="3000,5000,10000,20000")
    a = ap.parse_args()
    keys = json.load(open(os.path.join(a.set_dir, "keys.json")))
    total = sum(k["reward"] for k in keys.values() if k["feasible"])
    print("set %s (feasible reward %d)" % (os.path.basename(a.set_dir.rstrip("/")), total))
    for b in [float(x) for x in a.budgets.split(",")]:
        rows = []
        for run in a.runs:
            items = load(run, keys)
            s, spent, st, sk, cut = policy(items, b, order=ORDER)
            opt = knapsack(items, b)
            full = sum(x["r"] for x in items if x["win"])
            rows.append((s, opt, full, spent, st, sk, cut))
        n = len(rows)
        print("  budget %6d: policy %.1f  oracle %.1f  (%.0f%% of oracle)  unlimited %.1f  spent %.0f  started %.0f  "
              "skipped %.0f  cut-off %d/%d"
              % (b, sum(r[0] for r in rows) / n, sum(r[1] for r in rows) / n,
                 100 * sum(r[0] for r in rows) / max(1, sum(r[1] for r in rows)), sum(r[2] for r in rows) / n,
                 sum(r[3] for r in rows) / n, sum(r[4] for r in rows) / n, sum(r[5] for r in rows) / n,
                 sum(1 for r in rows if r[6]), n))


main()

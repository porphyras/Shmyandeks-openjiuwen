"""Offline replay: where should a second-pass retry go under a budget? (no model calls)

python3 -I bench/tools/retry_sim.py SET_DIR RUN_DIR [RUN_DIR ...] --budgets 8000,15000,30000,100000

From each run: a task's first pass (cost and outcome up to its first verdict) and, when it had a
second pass, the retry (cost and final outcome). Policies, replayed one task at a time with the
harness's check that a call whose forecast exceeds the remainder is not made:
  current  - first passes by reward*p/p90; retries after all of them, by reward*0.3/(forecast*4);
             none in economy mode (a known budget below 1.25x the forecast of the queue);
  unified  - a failed first pass puts its retry into the same queue, priority
             reward*0.75/(forecast*4), competing with first passes not yet made;
  late     - as current, but retries are also made in economy mode, after all first passes."""
import argparse
import heapq
import json
import os


def load(run, keys):
    est = json.load(open(os.path.join(run, "out", "token_estimates.json")))
    g = {t["task_id"]: t for t in json.load(open(os.path.join(run, "grade.json")))["tasks"]}
    lines = [json.loads(l) for l in open(os.path.join(run, "trace.jsonl"))]
    items = []
    for tid, v in est.items():
        if tid not in keys or "actual" not in v:
            continue
        first_cost, retry_cost, seen_task = 0.0, 0.0, 0
        for e in lines:
            if e.get("ev") == "llm" and e.get("tag") == tid:
                if seen_task == 0:
                    first_cost += e["cost"]
                else:
                    retry_cost += e["cost"]
            elif e.get("ev") == "task" and e.get("task") == tid:
                seen_task += 1
        final = bool(g.get(tid, {}).get("correct"))
        has_retry = seen_task > 1
        items.append({"t": tid, "r": float(keys[tid]["reward"]), "p": v.get("p_success", 0.9),
                      "est": max(1.0, v["estimate"]), "p90": max(1.0, v.get("estimate_p90", v["estimate"] * 2)),
                      "c1": first_cost, "ok1": final and not has_retry, "retry": has_retry,
                      "c2": retry_cost, "ok2": final and has_retry})
    return items


def replay(items, budget, policy):
    spent, score = 0.0, 0.0
    queue_forecast = sum(x["est"] for x in items)
    economy = budget < 1.25 * queue_forecast
    heap = [(-(x["r"] * x["p"] / x["p90"]), 0, i) for i, x in enumerate(items)]
    heapq.heapify(heap)
    later = []
    while heap:
        _, stage, i = heapq.heappop(heap)
        x = items[i]
        cost, forecast = (x["c1"], x["est"]) if stage == 0 else (x["c2"], x["est"] * 4)
        if forecast > budget - spent:
            continue  # the harness does not start a call it cannot pay for
        if spent + cost > budget:
            spent = budget  # the proxy stops answering mid-task: the task is lost
            break
        spent += cost
        if stage == 0:
            if x["ok1"]:
                score += x["r"]
            elif x["retry"]:
                if policy == "unified":
                    heapq.heappush(heap, (-(x["r"] * 0.75 / (x["est"] * 4)), 1, i))
                else:
                    later.append(i)
        elif x["ok2"]:
            score += x["r"]
    if policy in ("current", "late") and (policy == "late" or not economy) and spent < budget:
        for i in sorted(later, key=lambda i: -(items[i]["r"] * 0.3 / (items[i]["est"] * 4))):
            x = items[i]
            if x["est"] * 4 > budget - spent:
                continue
            if spent + x["c2"] > budget:
                break
            spent += x["c2"]
            score += x["r"] if x["ok2"] else 0
    return score, spent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir")
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--budgets", default="8000,15000,30000,100000")
    a = ap.parse_args()
    keys = json.load(open(os.path.join(a.set_dir, "keys.json")))
    for b in [float(x) for x in a.budgets.split(",")]:
        row = {}
        for pol in ("current", "unified", "late"):
            res = [replay(load(r, keys), b, pol) for r in a.runs]
            row[pol] = (sum(s for s, _ in res) / len(res), sum(sp for _, sp in res) / len(res))
        print("budget %6d: " % b + "   ".join("%s %.1f (spent %.0f)" % (p, row[p][0], row[p][1]) for p in row))


main()

"""Aggregate harness traces of bench runs into per-call rows and per-kind cost statistics.

python3 -I bench/tools/collect_traj.py OUT.csv RUN_DIR[=SET_DIR] ...
Each RUN_DIR holds trace.jsonl (HARNESS_TRACE) and grade.json; SET_DIR gives keys.json."""
import csv, json, os, statistics, sys
from collections import defaultdict

out, runs = sys.argv[1], sys.argv[2:]
rows, per_task = [], defaultdict(lambda: {"cost": 0.0, "calls": 0})
for spec in runs:
    run, _, setdir = spec.partition("=")
    keys = json.load(open(os.path.join(setdir, "keys.json"))) if setdir else {}
    grade = {}
    gp = os.path.join(run, "grade.json")
    if os.path.exists(gp):
        g = json.load(open(gp))
        for r in (g.get("tasks") or g.get("results") or []):
            if isinstance(r, dict) and "task_id" in r:
                grade[r["task_id"]] = r
    for line in open(os.path.join(run, "trace.jsonl")):
        r = json.loads(line)
        if r.get("ev") != "llm":
            continue
        k = keys.get(r["tag"], {})
        row = {"run": os.path.basename(run), "task": r["tag"], "type": k.get("type", ""), "feasible": k.get("feasible", ""),
               "model": r["model"], "in": r["in"], "out": r["out"], "cost": r["cost"], "latency_s": r.get("dt")}
        rows.append(row)
        pt = per_task[(row["run"], row["task"])]
        pt["cost"] += r["cost"]; pt["calls"] += 1; pt["type"] = row["type"]; pt["feasible"] = row["feasible"]
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
by = defaultdict(list)
for (run, task), v in per_task.items():
    by[(v["type"], v["feasible"])].append(v["cost"])
print("| type | feasible | tasks | mean cost | median | p90 | calls/task |")
print("|---|---|---|---|---|---|---|")
for (t, fe), costs in sorted(by.items(), key=str):
    calls = [v["calls"] for (r, k), v in per_task.items() if v["type"] == t and v["feasible"] == fe]
    costs_s = sorted(costs)
    print("| %s | %s | %d | %.0f | %.0f | %.0f | %.2f |" % (t, fe, len(costs), statistics.mean(costs), statistics.median(costs),
                                                       costs_s[int(0.9 * (len(costs_s) - 1))], statistics.mean(calls)))
print("\n%d calls in %d task runs" % (len(rows), len(per_task)))

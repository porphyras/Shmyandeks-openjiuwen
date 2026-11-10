"""Refit the search cost forecast (log-log quantile model) with the size of the knowledge base.

python3 -I bench/tools/refit_search.py --eval-prefix elB_r01 --eval-prefix fl_hard [--eval-prefix mixK]

Rows: every search task that reached the model in the forward-mode bench runs (bench/runs/*):
the excerpt length (ctx_chars), prose or cards, the bytes of the task's kb/ folder (from the
run's own copy of the tasks) and the actual cost. Both the current model (log cost = a + b*log(1+
ctx) + c*prose) and the new one (+ d*log(1+kb bytes)) are fitted for the median and the 90th
percentile by quantile regression (pinball loss, iteratively reweighted least squares) on the
runs outside the evaluation prefixes, and compared on those runs: median absolute error of the
median forecast and the share of tasks within the p90 forecast. A task repeated over n runs
weighs 1/n."""
import argparse
import collections
import glob
import json
import math
import os

import numpy as np


def kb_bytes(run, tid, cache={}):
    key = (run, tid)
    if key not in cache:
        total = 0
        for root in (os.path.join("bench/runs", run, "tasks", tid, "workspace"), os.path.join("bench/runs", run, "tasks", tid)):
            if os.path.isdir(root):
                for dp, _, fs in os.walk(root):
                    for f in fs:
                        if f.endswith((".md", ".txt", ".json", ".csv", ".rst", ".html", ".adoc", ".org")) and f != "answer.txt":
                            try:
                                total += os.path.getsize(os.path.join(dp, f))
                            except OSError:
                                pass
                break
        cache[key] = total
    return cache[key]


def rows():
    out = []
    for p in glob.glob("bench/runs/*/out/token_estimates.json"):
        run = p.split("/")[2]
        stub = os.path.join("bench/runs", run, "stub.log")
        if not os.path.exists(stub) or "forward" not in open(stub, errors="replace").read(400):
            continue
        try:
            est = json.load(open(p))
        except ValueError:
            continue
        for tid, v in est.items():
            if v.get("kind") == "search" and v.get("actual", 0) > 0:
                out.append({"run": run, "task": tid, "ctx": v.get("ctx_chars", 0), "prose": 1.0 if v.get("prose") else 0.0,
                            "kb": kb_bytes(run, tid), "actual": float(v["actual"])})
    return out


def design(rs, with_kb):
    cols = [[1.0, math.log1p(r["ctx"]), r["prose"]] + ([math.log1p(r["kb"])] if with_kb else []) for r in rs]
    return np.array(cols)


def qreg(X, y, w, q, iters=200):
    beta = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)[0]
    for _ in range(iters):
        r = y - X @ beta
        ww = w * np.where(r >= 0, q, 1 - q) / np.maximum(np.abs(r), 1e-3)
        beta = np.linalg.solve(X.T @ (X * ww[:, None]) + 1e-9 * np.eye(X.shape[1]), X.T @ (ww * y))
    return beta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-prefix", action="append", default=[])
    a = ap.parse_args()
    rs = rows()
    held = [r for r in rs if any(r["run"].startswith(x) for x in a.eval_prefix)]
    train = [r for r in rs if r not in held]
    n = collections.Counter((r["task"], r["kb"]) for r in train)
    w = np.array([1.0 / n[(r["task"], r["kb"])] for r in train])
    y = np.log(np.array([r["actual"] for r in train]))
    print("train %d rows (%d runs), held out %d rows" % (len(train), len({r["run"] for r in train}), len(held)))
    for with_kb in (False, True):
        X = design(train, with_kb)
        b50, b90 = qreg(X, y, w, 0.5), qreg(X, y, w, 0.9)
        name = "ctx + prose + kb" if with_kb else "ctx + prose (current form)"
        print("%-28s median %s  p90 %s" % (name, np.round(b50, 3).tolist(), np.round(b90, 3).tolist()))
        for label, sub in [("held out", held)] + [("  " + p, [r for r in held if r["run"].startswith(p)]) for p in a.eval_prefix]:
            if not sub:
                continue
            Xh = design(sub, with_kb)
            m, p9 = np.exp(Xh @ b50), np.maximum(np.exp(Xh @ b50), np.exp(Xh @ b90))
            act = np.array([r["actual"] for r in sub])
            print("   %-16s n=%4d  MAE(median) %6.0f  within p90 %.1f%%  forecast/actual (sum) %.2f"
                  % (label, len(sub), np.median(np.abs(m - act)), 100 * np.mean(act <= p9), m.sum() / act.sum()))
    # the constants now in run_agent.py, on the same held-out rows
    cur = (-1.234, 0.75, 0.1, 2.499, 0.375, 0.6)
    for label, sub in [("held out", held)] + [("  " + p, [r for r in held if r["run"].startswith(p)]) for p in a.eval_prefix]:
        if not sub:
            continue
        X = design(sub, False)
        m = np.exp(X @ np.array(cur[:3]))
        p9 = np.maximum(m, np.exp(X @ np.array(cur[3:])))
        act = np.array([r["actual"] for r in sub])
        print("current SEARCH_FIT %-12s n=%4d  MAE(median) %6.0f  within p90 %.1f%%  forecast/actual (sum) %.2f"
              % (label, len(sub), np.median(np.abs(m - act)), 100 * np.mean(act <= p9), m.sum() / act.sum()))


main()

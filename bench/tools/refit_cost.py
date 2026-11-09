"""Refit the per-kind cost forecast on all collected traces and compare it with the current one
on runs kept out of the fit.

python3 -I bench/tools/refit_cost.py TRACES.jsonl --eval-prefix l_ [--eval-prefix g1_]

Training rows: the external traces file (one JSON per task: kind, ctx_chars, prose, actual,
llm_calls, run, task) plus the token_estimates.json of the bench runs in bench/runs, except the
runs whose name starts with an evaluation prefix. Only tasks that reached the model count (free
answers cost nothing and are not forecast). A task repeated over n runs weighs 1/n. For a large
prose base the harness multiplies the forecast by 2.5, so such rows are fitted at actual / 2.5.
The fit is weighted least absolute deviations of cost = max(floor, a + b * ctx_chars); the p90
factor is the weighted 90th percentile of actual / forecast."""
import argparse
import collections
import glob
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
PROSE_FACTOR = 2.5


def bench_rows(skip_prefixes):
    rows = []
    for p in glob.glob("bench/runs/*/out/token_estimates.json"):
        run = p.split("/")[2]
        if any(run.startswith(x) for x in skip_prefixes):
            continue
        stub = os.path.join("bench/runs", run, "stub.log")
        if not os.path.exists(stub) or "forward" not in open(stub, errors="replace").read(400):
            continue  # mock runs bill made-up replies
        for tid, v in json.load(open(p)).items():
            if v.get("actual", 0) > 0:
                rows.append({"kind": v["kind"], "ctx_chars": v.get("ctx_chars", 0), "prose": v.get("prose", False),
                             "actual": v["actual"], "run": run, "task": tid, "src": "bench"})
    return rows


def eval_rows(prefixes):
    return [r for r in bench_rows([]) if any(r["run"].startswith(x) for x in prefixes)]


def weights(rows):
    count = collections.Counter((r.get("src", "x"), r["task"], r["kind"]) for r in rows)
    return [1.0 / count[(r.get("src", "x"), r["task"], r["kind"])] for r in rows]


def lad(xs, ys, ws, iters=80):
    a, b = st.median(ys), 0.0
    w = list(ws)
    for _ in range(iters):
        sw = sum(w)
        mx = sum(wi * x for wi, x in zip(w, xs)) / sw
        my = sum(wi * y for wi, y in zip(w, ys)) / sw
        vx = sum(wi * (x - mx) ** 2 for wi, x in zip(w, xs))
        b = max(0.0, (sum(wi * (x - mx) * (y - my) for wi, x, y in zip(w, xs, ys)) / vx) if vx > 0 else 0.0)
        a = my - b * mx
        w = [wi0 / max(1.0, abs(y - (a + b * x))) for wi0, x, y in zip(ws, xs, ys)]
    return a, b


def wquantile(vals, ws, q):
    pairs = sorted(zip(vals, ws))
    tot, acc = sum(ws), 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot:
            return v
    return pairs[-1][0]


def fit(rows):
    out = {}
    ws_all = weights(rows)
    by = collections.defaultdict(list)
    for r, w in zip(rows, ws_all):
        y = r["actual"] / (PROSE_FACTOR if r["kind"] == "search" and r.get("prose") else 1.0)
        by[r["kind"]].append((float(r["ctx_chars"]), float(y), w))
    for k, pts in sorted(by.items()):
        xs, ys, ws = zip(*pts)
        a, b = lad(xs, ys, ws)
        floor = max(10.0, wquantile(ys, ws, 0.25) * 0.5)
        pred = [max(floor, a + b * x) for x in xs]
        p90 = wquantile([y / p for y, p in zip(ys, pred)], ws, 0.9)
        out[k] = (round(a, 1), round(b, 4), round(floor, 1), round(p90, 2))
    return out


def evaluate(fits, rows, label):
    by = collections.defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        a, b, floor, p90 = fits.get(r["kind"], (60.0, 0.1, 40, 2.0))
        est = max(floor, a + b * r["ctx_chars"]) * (PROSE_FACTOR if r["kind"] == "search" and r.get("prose") else 1.0)
        s = by[r["kind"]]
        s[0] += 1
        s[1] += r["actual"] <= est * p90
        s[2] += abs(r["actual"] - est)
    tot = [0, 0, 0.0]
    print(label)
    for k, (n, inp, err) in sorted(by.items()):
        print("  %-7s n=%4d  p90 coverage %5.1f %%  MAE %6.1f" % (k, n, 100 * inp / n, err / n))
        tot[0] += n; tot[1] += inp; tot[2] += err
    print("  all     n=%4d  p90 coverage %5.1f %%  MAE %6.1f" % (tot[0], 100 * tot[1] / tot[0], tot[2] / tot[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("traces")
    ap.add_argument("--eval-prefix", action="append", default=[])
    a = ap.parse_args()
    ext = []
    for line in open(a.traces):
        r = json.loads(line)
        if r.get("llm_calls", 0) > 0 and r.get("actual", 0) > 0:
            r["src"] = "ext:" + r["run"].split("_")[0]
            ext.append(r)
    train = ext + bench_rows(a.eval_prefix)
    test = eval_rows(a.eval_prefix)
    print("training rows %d (external %d, bench %d); evaluation rows %d" % (len(train), len(ext), len(train) - len(ext), len(test)))
    from run_agent import COST_FIT
    new = fit(train)
    evaluate(COST_FIT, test, "current COST_FIT on the held-out runs")
    evaluate(new, test, "refitted on all traces, on the held-out runs")
    evaluate(COST_FIT, ext, "current COST_FIT on the external traces (in-sample for the refit)")
    print("NEW COST_FIT =", json.dumps(new))


main()

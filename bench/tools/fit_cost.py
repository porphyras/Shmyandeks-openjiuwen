"""Fit the per-kind cost model cost = a + b * ctx_chars by least absolute deviations (the
median forecast, which minimises MAE) and the p90 ratio actual/forecast, from the
token_estimates.json files of bench runs. Prints the fit and in-sample MAE / underestimation.

python3 -I bench/tools/fit_cost.py bench/runs/<run>/out/token_estimates.json ..."""
import json, statistics as st, sys
from collections import defaultdict

rows = []
for p in sys.argv[1:]:
    for v in json.load(open(p)).values():
        if v.get("actual", 0) > 0:
            rows.append(v)
by = defaultdict(list)
for v in rows:
    by[v["kind"]].append((float(v["ctx_chars"]), float(v["actual"])))


def lad(points, iters=60):
    xs = [x for x, _ in points]; ys = [y for _, y in points]
    w = [1.0] * len(points)
    a, b = st.median(ys), 0.0
    for _ in range(iters):
        sw = sum(w); mx = sum(wi * x for wi, x in zip(w, xs)) / sw; my = sum(wi * y for wi, y in zip(w, ys)) / sw
        vx = sum(wi * (x - mx) ** 2 for wi, x in zip(w, xs))
        b = (sum(wi * (x - mx) * (y - my) for wi, x, y in zip(w, xs, ys)) / vx) if vx > 0 else 0.0
        b = max(b, 0.0)
        a = my - b * mx
        w = [1.0 / max(1.0, abs(y - (a + b * x))) for x, y in zip(xs, ys)]
    return a, b


fits = {}
for k, pts in sorted(by.items()):
    a, b = lad(pts)
    floor = max(10.0, st.quantiles([y for _, y in pts], n=4)[0] * 0.5) if len(pts) > 4 else 10.0
    pred = [max(floor, a + b * x) for x, _ in pts]
    ratios = sorted(y / p for (x, y), p in zip(pts, pred))
    p90 = ratios[int(0.9 * (len(ratios) - 1))]
    mae = st.mean(abs(y - p) for (x, y), p in zip(pts, pred))
    under = sum(1 for (x, y), p in zip(pts, pred) if y > p) / len(pts)
    fits[k] = (round(a, 1), round(b, 4), round(floor, 1), round(p90, 2))
    print("%-7s n=%3d  a=%7.1f b=%.4f floor=%5.1f p90x=%.2f  MAE=%6.1f  under=%3.0f%%  mean actual=%.0f"
          % (k, len(pts), a, b, floor, p90, mae, 100 * under, st.mean(y for _, y in pts)))
print(json.dumps(fits))

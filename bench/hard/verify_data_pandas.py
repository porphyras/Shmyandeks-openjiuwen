"""Second, independent computation of every feasible data_analysis answer with pandas.

Usage: <python with pandas> -I verify_data_pandas.py <set dir>
Parameters are parsed back from the written instruction text; data is read from the written
workspace files. Prints one JSON object {task_id: [expected, recomputed]} on stdout.
"""
import json
import math
import os
import re
import sys

import numpy as np
import pandas as pd


def load(ws, name, **kw):
    kw.setdefault("keep_default_na", True)
    return pd.read_csv(os.path.join(ws, name), **kw)


def z_count(s):
    s = s.dropna().astype(float)
    a = int(((s - s.mean()).abs() / s.std(ddof=0) > 3).sum())
    b = int(((s - s.mean()).abs() / s.std(ddof=1) > 3).sum())
    return a if a == b else None


def iqr_count(s):
    s = s.dropna().astype(float)
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    i = q3 - q1
    return int(((s < q1 - 1.5 * i) | (s > q3 + 1.5 * i)).sum())


def dab(ws, qid):
    if qid == 510:
        h = load(ws, "hotel_data.csv", keep_default_na=False)
        h = h[(h.brand_name != "") & (h.brand_name != "None")]
        counts = h.brand_name.value_counts()
        h2 = h[h.star_rating.astype(str).str.strip().ne("") & h.star_rating.astype(str).ne("None")].copy()
        h2["star_rating"] = h2.star_rating.astype(float)
        h2 = h2[pd.to_numeric(h2.review_count, errors="coerce") >= 100]
        g = h2.groupby("brand_name").star_rating.mean()
        g = g[[counts[b] >= 10 for b in g.index]]
        return g.idxmax()
    if qid == 123:
        v = load(ws, "country_vaccinations.csv")
        return v.dropna(subset=["daily_vaccinations_per_million"]).groupby("country").daily_vaccinations_per_million.mean().idxmax()
    if qid == 553:
        return iqr_count(load(ws, "tree.csv").TPH_PLT)
    if qid == 757:
        return iqr_count(load(ws, "weather_data_1864.csv").obs_value)
    if qid == 447:
        b = load(ws, "baro_2015.csv")
        b.columns = [c.strip() for c in b.columns]
        return iqr_count(pd.to_numeric(b.BARO, errors="coerce"))
    if qid == 132:
        return z_count(load(ws, "titanic.csv").Fare)
    if qid == 175:
        return z_count(load(ws, "titanic.csv").Age)
    if qid == 740:
        return z_count(load(ws, "Credit.csv").Balance)
    if qid == 555:
        c = load(ws, "tree.csv").SPP_SYMBOL.value_counts()
        return int((c >= 5).sum())
    if qid == 55:
        e = load(ws, "estimated_numbers.csv")
        s = e["No. of cases"].dropna().astype(str).str.split("[").str[0].str.strip().astype("int64")
        return int(math.floor(s.mean() + 0.5))
    if qid == 492:
        p = load(ws, "percent-bachelors-degrees-women-usa.csv")
        return p[p.Year == 2010].drop(columns=["Year"]).iloc[0].astype(float).idxmax()
    if qid == 217:
        f = load(ws, "ferret-Pitt-2-preinf-lib2-100_sitediffsel.csv", dtype={"site": str})
        return f.loc[f.positive_diffsel.idxmax(), "site"]
    raise KeyError(qid)


def synth(ws, kind, q):
    if kind.startswith("clinic"):
        p = load(ws, "patients.csv", parse_dates=["birth_date"])
        v = load(ws, "visits.csv", parse_dates=["visit_date"])
        m = v.merge(p, on="patient_id", how="inner")
        if kind == "clinic_dept_cost":
            m["dep"] = m.department.str.strip().str.lower()
            s = m[(m.visit_date >= "2024-04-01") & (m.visit_date <= "2024-06-30") & (m.birth_date < "1980-01-01")]
            return s.groupby("dep").cost.sum().idxmax()
        if kind == "clinic_distinct_patients":
            city, dep, a, b = re.search(r"living in (\w+) had at least one (\w+) visit between (\S+) and (\S+) inclusive", q).groups()
            s = m[(m.city == city) & (m.department == dep) & (m.visit_date >= a) & (m.visit_date <= b)]
            return int(s.patient_id.nunique())
        if kind == "clinic_senior_avg":
            bd, vd = m.birth_date, m.visit_date
            age = vd.dt.year - bd.dt.year - ((vd.dt.month < bd.dt.month) | ((vd.dt.month == bd.dt.month) & (vd.dt.day < bd.dt.day))).astype(int)
            return int(math.floor(m[age >= 65].cost.mean() + 0.5))
    if kind.startswith("lib"):
        b = load(ws, "books.csv")
        lo = load(ws, "loans.csv", parse_dates=["loan_date", "return_date"])
        m = lo.merge(b, on="book_id", how="inner")
        if kind == "lib_late_genre":
            late = m[(m.return_date - m.loan_date).dt.days > 21]
            return late.genre.value_counts().idxmax()
        if kind == "lib_weekend_old":
            year = int(re.search(r"published before (\d{4})", q).group(1))
            return int(((m.loan_date.dt.dayofweek >= 5) & (m.year_published < year)).sum())
        if kind == "lib_top_member":
            s = lo[(lo.loan_date >= "2023-07-01") & (lo.loan_date <= "2023-12-31")]
            return s.groupby("member_id").book_id.nunique().idxmax()
    if kind.startswith("fl"):
        f = load(ws, "flights.csv", parse_dates=["sched_dep", "actual_dep"])
        f = f.dropna(subset=["actual_dep"])
        f["delay"] = (f.actual_dep - f.sched_dep).dt.total_seconds() / 60
        if kind == "fl_late_share":
            c = load(ws, "carriers.csv", keep_default_na=False)
            j = f[(f.sched_dep.dt.year == 2023) & (f.sched_dep.dt.month == 7)]
            g = j.groupby("carrier_code").agg(n=("delay", "size"), late=("delay", lambda d: (d > 15).mean()))
            g = g[g.n >= 30]
            return c.set_index("carrier_code").carrier_name[g.late.idxmax()]
        if kind == "fl_weekend_distance":
            origin = re.search(r"flights from (\w{3}) scheduled", q).group(1)
            s = f[(f.origin == origin) & (f.sched_dep.dt.dayofweek >= 5) & (f.delay <= 0)]
            return int(s.distance_km.sum())
        if kind == "fl_median_delay":
            c = load(ws, "carriers.csv", keep_default_na=False)
            name = re.search(r"carrier named (.+?) \(look up", q).group(1)
            code = c[c.carrier_name == name].carrier_code.iloc[0]
            return int(math.floor(f[f.carrier_code == code].delay.median()))
    if kind.startswith("hr"):
        e = load(ws, "employees.csv", parse_dates=["hire_date"])
        d = load(ws, "departments.csv")
        if kind == "hr_above_manager":
            j = e.merge(e[["emp_id", "salary"]], left_on="manager_id", right_on="emp_id", suffixes=("", "_mgr"))
            return int((j.salary > j.salary_mgr).sum())
        if kind == "hr_median_dept":
            j = e[e.hire_date < "2018-01-01"].merge(d, on="dept_id")
            g = j.groupby("dept_name").salary.agg(["median", "size"])
            return g[g["size"] >= 5]["median"].idxmax()
    if kind.startswith("sens"):
        s = load(ws, "sensors.csv", parse_dates=["installed_on"])
        r = load(ws, "readings.csv", parse_dates=["timestamp"]).dropna(subset=["temp_c"])
        j = r.merge(s, on="sensor_id")
        j = j[j.timestamp.dt.normalize() >= j.installed_on]
        if kind == "sens_hot_readings":
            site, thr = re.search(r"at site (\w+) have temp_c above ([\d.]+)", q).groups()
            return int(((j.site == site) & (j.temp_c > float(thr))).sum())
        if kind == "sens_site_avg":
            w = j[(j.timestamp.dt.dayofweek < 5) & (j.timestamp.dt.hour >= 12) & (j.timestamp.dt.hour <= 15)]
            return w.groupby("site").temp_c.mean().idxmax()
    raise KeyError(kind)


def main():
    sd = sys.argv[1]
    keys = json.load(open(os.path.join(sd, "keys.json"), encoding="utf-8"))
    out = {}
    for tid, k in keys.items():
        if k["type"] != "data_analysis" or not k["feasible"]:
            continue
        ws = os.path.join(sd, "tasks", tid, "workspace")
        q = json.load(open(os.path.join(sd, "tasks", tid, "task.json"), encoding="utf-8"))["instruction"]
        try:
            if k["meta"]["source"].startswith("infiagent"):
                v = dab(ws, k["meta"]["id"])
            else:
                v = synth(ws, k["kind"], q)
        except Exception as e:  # report, do not crash the whole check
            v = "ERROR %s: %s" % (type(e).__name__, e)
        if isinstance(v, (np.integer,)):
            v = int(v)
        out[tid] = [k["expected"], str(v)]
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()

"""Data-analysis tasks.

Part A - InfiAgent-DABench (dev split): 12 questions with a single integer or short-string answer.
The question text is rewritten from the DABench question + constraints so the definition is
explicit (z-score threshold, IQR rule with linear-interpolated quartiles, filters). Each answer is
recomputed here with the standard library and must equal the DABench label; z-score counts must
agree for population and sample standard deviation.

Part B - synthetic two-file tasks (clinic, library, flights, HR, sensors): joins, date windows,
weekday filters, ages, self-joins, group-by argmax with string answers, explicit rounding.
Answers are computed by reference code that re-reads the written CSV bytes (not the generator's
in-memory data); argmax answers need a clear margin and rounded answers must not be near .5.
verify_hard.py recomputes every data answer a second time with pandas.

Infeasible: a column that does not exist, an entity that does not occur, a missing second file,
a period outside the data.
"""
import csv
import datetime as dt
import io
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA_TAIL, rng_for  # noqa: E402


# ----------------------------------------------------------------------------- csv helpers
def read_csv(data):
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    return list(csv.DictReader(io.StringIO(text, newline="")))


def header_and_count(data):
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    first_line = text.splitlines()[0]
    return first_line, len(rows) - 1


def write_csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def describe(files, order):
    parts = []
    for name in order:
        head, n = header_and_count(files[name])
        parts.append("the file %s has columns %s (%d rows)" % (name, head, n))
    s = " and ".join(parts)
    return s[0].upper() + s[1:] + "."


def q_linear(xs, q):
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def iqr_outliers(xs):
    q1, q3 = q_linear(xs, 0.25), q_linear(xs, 0.75)
    i = q3 - q1
    return sum(1 for x in xs if x < q1 - 1.5 * i or x > q3 + 1.5 * i)


def z_outliers(xs):
    m = statistics.fmean(xs)
    a = sum(1 for x in xs if abs(x - m) / statistics.pstdev(xs) > 3)
    b = sum(1 for x in xs if abs(x - m) / statistics.stdev(xs) > 3)
    return a if a == b else None


def num(s):
    s = s.strip()
    return float(s) if s not in ("", "None", "nan", "NaN") else None


# ----------------------------------------------------------------------------- part A: DABench
def _dab_510(f):
    r = read_csv(f["hotel_data.csv"])
    total = Counter(x["brand_name"] for x in r if x["brand_name"] not in ("", "None"))
    res = []
    for count_on_filtered in (False, True):
        g = defaultdict(list)
        for x in r:
            if x["brand_name"] in ("", "None") or num(x["star_rating"]) is None:
                continue
            if (num(x["review_count"]) or 0) < 100:
                continue
            g[x["brand_name"]].append(num(x["star_rating"]))
        ok = {b: v for b, v in g.items() if (len(v) if count_on_filtered else total[b]) >= 10}
        best = sorted(ok, key=lambda b: (-statistics.fmean(ok[b]), -total[b]))
        res.append(best[0])
    return res[0] if res[0] == res[1] else None


def _dab_123(f):
    g = defaultdict(list)
    for x in read_csv(f["country_vaccinations.csv"]):
        v = num(x["daily_vaccinations_per_million"])
        if v is not None:
            g[x["country"]].append(v)
    m = {k: statistics.fmean(v) for k, v in g.items()}
    top = sorted(m, key=lambda k: -m[k])
    return top[0] if m[top[0]] > m[top[1]] else None


def _dab_col_iqr(fname, col):
    def f_(f):
        xs = [num(x[col]) for x in read_csv(f[fname])]
        return iqr_outliers([x for x in xs if x is not None])
    return f_


def _dab_col_z(fname, col):
    def f_(f):
        xs = [num(x[col]) for x in read_csv(f[fname])]
        return z_outliers([x for x in xs if x is not None])
    return f_


def _dab_555(f):
    c = Counter(x["SPP_SYMBOL"] for x in read_csv(f["tree.csv"]))
    return sum(1 for v in c.values() if v >= 5)


def _dab_55(f):
    xs = []
    for x in read_csv(f["estimated_numbers.csv"]):
        s = x["No. of cases"].strip()
        if s:
            xs.append(int(s.split("[")[0].strip()))
    m = statistics.fmean(xs)
    if abs(m - math.floor(m) - 0.5) < 0.05:
        return None
    return round(m)


def _dab_492(f):
    row = [x for x in read_csv(f["percent-bachelors-degrees-women-usa.csv"]) if x["Year"] == "2010"][0]
    vals = {k: float(v) for k, v in row.items() if k != "Year"}
    top = sorted(vals, key=lambda k: -vals[k])
    return top[0] if vals[top[0]] > vals[top[1]] else None


def _dab_217(f):
    r = read_csv(f["ferret-Pitt-2-preinf-lib2-100_sitediffsel.csv"])
    s = sorted(r, key=lambda x: -float(x["positive_diffsel"]))
    return s[0]["site"] if float(s[0]["positive_diffsel"]) > float(s[1]["positive_diffsel"]) else None


DABENCH = [
    (510, "hotel_data.csv", "dab_argmax",
     "Which hotel brand (brand_name) has the highest average star_rating among hotels with a review_count of "
     "at least 100? Ignore hotels without a brand (brand_name empty or 'None') or without a star rating, and "
     "only consider brands that have at least 10 hotels in the whole file. Write the brand name exactly as it "
     "appears in the file.", _dab_510),
    (123, "country_vaccinations.csv", "dab_argmax",
     "Using only rows where daily_vaccinations_per_million is not empty, which country has the highest average "
     "daily_vaccinations_per_million? Write the country name exactly as it appears in the file.", _dab_123),
    (553, "tree.csv", "dab_outliers",
     "How many values in the TPH_PLT column are outliers by the IQR rule, i.e. below Q1 - 1.5*IQR or above "
     "Q3 + 1.5*IQR, where Q1 and Q3 are the 25th and 75th percentiles computed with linear interpolation "
     "(the pandas default) and IQR = Q3 - Q1?", _dab_col_iqr("tree.csv", "TPH_PLT")),
    (757, "weather_data_1864.csv", "dab_outliers",
     "How many values in the obs_value column are outliers by the IQR rule, i.e. below Q1 - 1.5*IQR or above "
     "Q3 + 1.5*IQR, where Q1 and Q3 are the 25th and 75th percentiles computed with linear interpolation "
     "(the pandas default) and IQR = Q3 - Q1?", _dab_col_iqr("weather_data_1864.csv", "obs_value")),
    (447, "baro_2015.csv", "dab_outliers",
     "Ignoring empty values, how many values in the BARO column (atmospheric pressure) are outliers by the IQR "
     "rule, i.e. below Q1 - 1.5*IQR or above Q3 + 1.5*IQR, where Q1 and Q3 are the 25th and 75th percentiles "
     "computed with linear interpolation (the pandas default) and IQR = Q3 - Q1?",
     _dab_col_iqr("baro_2015.csv", " BARO")),
    (132, "titanic.csv", "dab_outliers",
     "Compute the z-score of every Fare as (Fare - mean) / standard deviation, using the mean and standard "
     "deviation of the Fare column. How many passengers have a Fare whose z-score is greater than 3 or less "
     "than -3?", _dab_col_z("titanic.csv", "Fare")),
    (175, "titanic.csv", "dab_outliers",
     "Ignoring passengers with an empty Age, compute the z-score of every Age as (Age - mean) / standard "
     "deviation. How many passengers have an Age whose z-score is greater than 3 or less than -3?",
     _dab_col_z("titanic.csv", "Age")),
    (740, "Credit.csv", "dab_outliers",
     "Compute the z-score of every Balance as (Balance - mean) / standard deviation. How many rows have a "
     "Balance more than 3 standard deviations away from the mean?", _dab_col_z("Credit.csv", "Balance")),
    (555, "tree.csv", "dab_count",
     "How many distinct SPP_SYMBOL values (plant species) occur in at least 5 rows?", _dab_555),
    (55, "estimated_numbers.csv", "dab_parse",
     "The column 'No. of cases' holds text such as '630308[495000-801000]'. Take the number before the '[' as "
     "the number of cases (skip rows where the column is empty). What is the mean number of cases over all "
     "remaining rows, rounded to the nearest integer?", _dab_55),
    (492, "percent-bachelors-degrees-women-usa.csv", "dab_argmax",
     "In the row for Year 2010, which field (column) has the highest percentage of bachelor's degrees awarded "
     "to women? Write the column name exactly as it appears in the header.", _dab_492),
    (217, "ferret-Pitt-2-preinf-lib2-100_sitediffsel.csv", "dab_argmax",
     "Which site has the highest positive_diffsel value? Write the site identifier exactly as it appears in "
     "the site column.", _dab_217),
]


def dabench_tasks(dl):
    import json
    labels = {}
    with open(os.path.join(dl, "dabench", "da-dev-labels.jsonl"), encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            labels[d["id"]] = d["common_answers"]
    tasks, dropped = [], []
    for qid, fname, kind, text, ref in DABENCH:
        with open(os.path.join(dl, "dabench_tables", fname), "rb") as f:
            data = f.read()
        files = {fname: data}
        mine = ref(files)
        lab = labels[qid]
        if mine is None or len(lab) != 1 or str(mine) != lab[0][1]:
            dropped.append((qid, mine, lab))
            continue
        check = "exact_int" if isinstance(mine, int) else "exact_str"
        instr = "%s %s %s" % (describe(files, [fname]), text, DATA_TAIL)
        tasks.append({"type": "data_analysis", "kind": kind, "feasible": True, "instruction": instr,
                      "check": check, "expected": str(mine), "files": files,
                      "meta": {"source": "infiagent-dabench-dev", "id": qid, "label": lab[0][1]}})
    return tasks, dropped


# ----------------------------------------------------------------------------- part B: synthetic
FIRST = ["Ava", "Liam", "Mia", "Noah", "Ines", "Omar", "Lena", "Ivan", "Sara", "Theo", "Nora", "Emil",
         "Aino", "Jonas", "Rosa", "Kai", "Ella", "Mats", "Yara", "Otto", "Lara", "Finn", "Maja", "Leo"]
LAST = ["Berg", "Novak", "Silva", "Kowal", "Lind", "Moreau", "Haas", "Ruiz", "Dahl", "Costa", "Weber",
        "Holm", "Rossi", "Kask", "Varga", "Ilves", "Strand", "Meyer", "Lopes", "Aalto"]


def rdate(rng, a, b):
    return a + dt.timedelta(days=rng.randint(0, (b - a).days))


def D(s):
    return dt.date.fromisoformat(s)


def T(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M")


def age_on(birth, day):
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


class Reject(Exception):
    pass


def margin_argmax(scores, rel=0.01, absolute=0.0):
    top = sorted(scores, key=lambda k: -scores[k])
    if len(top) < 2:
        raise Reject("argmax needs 2 groups")
    a, b = scores[top[0]], scores[top[1]]
    if a - b <= max(absolute, rel * abs(a)):
        raise Reject("argmax margin")
    return top[0]


def safe_round(x):
    if abs(x - math.floor(x) - 0.5) < 0.05:
        raise Reject("near .5")
    return int(math.floor(x + 0.5))


# --- clinic
CITIES = ["Lyon", "Porto", "Gdansk", "Tartu", "Bergen", "Leeds", "Graz", "Malmo"]
DEPTS = ["cardiology", "oncology", "neurology", "orthopedics", "pediatrics", "dermatology", "radiology"]


def gen_clinic(rng, messy=False):
    n_p = rng.randint(90, 170)
    ids = rng.sample(range(1000, 9999), n_p)
    pats = []
    for i in ids:
        pats.append(["P%04d" % i, "%s %s" % (rng.choice(FIRST), rng.choice(LAST)),
                     rdate(rng, D("1935-01-01"), D("2012-12-31")).isoformat(), rng.choice(CITIES)])
    w = [rng.uniform(0.5, 2.0) for _ in DEPTS]
    visits = []
    for k in range(rng.randint(600, 1300)):
        dep = rng.choices(DEPTS, w)[0]
        if messy and rng.random() < 0.25:
            dep = rng.choice([dep.capitalize(), dep.upper(), " " + dep, dep + " ", dep.capitalize() + " "])
        visits.append(["V%05d" % (k + 1), rng.choice(pats)[0], rdate(rng, D("2023-07-01"), D("2024-12-31")).isoformat(),
                       dep, rng.randint(40, 2500)])
    return {"patients.csv": write_csv(["patient_id", "name", "birth_date", "city"], pats),
            "visits.csv": write_csv(["visit_id", "patient_id", "visit_date", "department", "cost"], visits)}


def clinic_join(f):
    pats = {p["patient_id"]: p for p in read_csv(f["patients.csv"])}
    out = []
    for v in read_csv(f["visits.csv"]):
        p = pats.get(v["patient_id"])
        if p is not None:
            out.append((v, p))
    return out


def task_clinic_dept_cost(rng):
    messy = rng.random() < 0.5
    f = gen_clinic(rng, messy)
    tot = defaultdict(int)
    for v, p in clinic_join(f):
        if D("2024-04-01") <= D(v["visit_date"]) <= D("2024-06-30") and D(p["birth_date"]) < D("1980-01-01"):
            tot[v["department"].strip().lower()] += int(v["cost"])
    ans = margin_argmax(tot, rel=0.02)
    q = ("Considering only visits dated from 2024-04-01 to 2024-06-30 inclusive by patients born before "
         "1980-01-01, which department has the highest total cost?")
    if messy:
        q += (" Department names are not formatted consistently (case and surrounding spaces vary): treat them "
              "case-insensitively, ignore surrounding spaces and write the answer in lowercase.")
    return f, ["patients.csv", "visits.csv"], q, ans, {"messy": messy}


def task_clinic_distinct(rng):
    f = gen_clinic(rng)
    city = rng.choice(CITIES)
    dep = rng.choice(DEPTS)
    a = rdate(rng, D("2023-08-01"), D("2024-06-30"))
    b = a + dt.timedelta(days=rng.randint(90, 200))
    s = {p["patient_id"] for v, p in clinic_join(f)
         if p["city"] == city and v["department"] == dep and a <= D(v["visit_date"]) <= b}
    if len(s) < 6:
        raise Reject("too few")
    q = ("How many distinct patients living in %s had at least one %s visit between %s and %s inclusive?"
         % (city, dep, a.isoformat(), b.isoformat()))
    return f, ["patients.csv", "visits.csv"], q, len(s), {}


def task_clinic_senior_avg(rng):
    f = gen_clinic(rng)
    costs = [int(v["cost"]) for v, p in clinic_join(f) if age_on(D(p["birth_date"]), D(v["visit_date"])) >= 65]
    if len(costs) < 20:
        raise Reject("too few")
    q = ("A patient's age on a visit date is the number of full years since their birth_date. What is the average "
         "cost of the visits made by patients who were at least 65 years old on the visit date, rounded to the "
         "nearest integer?")
    return f, ["patients.csv", "visits.csv"], q, safe_round(statistics.fmean(costs)), {}


# --- library
GENRES = ["mystery", "history", "poetry", "science", "fantasy", "biography", "travel"]
WORDS = ["River", "Shadow", "Glass", "North", "Garden", "Silent", "Iron", "Paper", "Winter", "Harbor", "Stone",
         "Lantern", "Echo", "Amber", "Falcon", "Orchard"]


def gen_library(rng):
    books = []
    gw = [rng.uniform(0.5, 2.0) for _ in GENRES]
    for i in rng.sample(range(1000, 9999), rng.randint(150, 300)):
        books.append(["B%04d" % i, "The %s %s" % (rng.choice(WORDS), rng.choice(WORDS)), rng.choices(GENRES, gw)[0],
                      "%s %s" % (rng.choice(FIRST), rng.choice(LAST)), rng.randint(1890, 2022)])
    members = ["M%03d" % i for i in rng.sample(range(1, 999), rng.randint(40, 110))]
    loans = []
    for k in range(rng.randint(700, 1500)):
        d0 = rdate(rng, D("2023-01-01"), D("2023-12-31"))
        ret = "" if rng.random() < 0.08 else (d0 + dt.timedelta(days=rng.randint(1, 45))).isoformat()
        loans.append(["L%05d" % (k + 1), rng.choice(books)[0], rng.choice(members), d0.isoformat(), ret])
    return {"books.csv": write_csv(["book_id", "title", "genre", "author", "year_published"], books),
            "loans.csv": write_csv(["loan_id", "book_id", "member_id", "loan_date", "return_date"], loans)}


def task_lib_late_genre(rng):
    f = gen_library(rng)
    books = {b["book_id"]: b for b in read_csv(f["books.csv"])}
    c = Counter()
    for x in read_csv(f["loans.csv"]):
        if x["return_date"] and (D(x["return_date"]) - D(x["loan_date"])).days > 21:
            c[books[x["book_id"]]["genre"]] += 1
    ans = margin_argmax(dict(c), absolute=2)
    q = ("A loan is late if its return_date is more than 21 days after its loan_date; loans with an empty "
         "return_date are not counted. Which genre has the largest number of late loans?")
    return f, ["books.csv", "loans.csv"], q, ans, {}


def task_lib_weekend_old(rng):
    f = gen_library(rng)
    books = {b["book_id"]: b for b in read_csv(f["books.csv"])}
    year = rng.choice([1940, 1950, 1960])
    n = sum(1 for x in read_csv(f["loans.csv"])
            if D(x["loan_date"]).weekday() >= 5 and int(books[x["book_id"]]["year_published"]) < year)
    q = ("How many loans of books published before %d started on a Saturday or a Sunday (by loan_date)?" % year)
    return f, ["books.csv", "loans.csv"], q, n, {"year": year}


def task_lib_top_member(rng):
    f = gen_library(rng)
    g = defaultdict(set)
    for x in read_csv(f["loans.csv"]):
        if D("2023-07-01") <= D(x["loan_date"]) <= D("2023-12-31"):
            g[x["member_id"]].add(x["book_id"])
    ans = margin_argmax({k: len(v) for k, v in g.items()}, absolute=0.5)
    q = ("Which member_id borrowed the largest number of distinct books with a loan_date from 2023-07-01 to "
         "2023-12-31 inclusive? Write the member_id exactly as it appears in the file.")
    return f, ["loans.csv", "books.csv"], q, ans, {}


# --- flights
AIRPORTS = ["OSL", "BGO", "TRD", "SVG", "TOS", "BOO", "AES", "KRS"]
CARRIER_NAMES = ["Fjordline Air", "Pinewood Airways", "Saga Skies", "Tundra Express", "Harbor Hop",
                 "Glacier Air", "Cedar Airlines", "Aurora Shuttle"]


def gen_flights(rng):
    names = rng.sample(CARRIER_NAMES, rng.randint(5, 7))
    codes = []
    while len(codes) < len(names):
        c = rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") + rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")
        if c not in codes:
            codes.append(c)
    late_p = {c: rng.uniform(0.08, 0.35) for c in codes}
    weight = [rng.uniform(0.6, 1.6) for _ in codes]
    dist = {}
    for a in AIRPORTS:
        for b in AIRPORTS:
            if a < b:
                dist[(a, b)] = dist[(b, a)] = rng.randint(180, 1600)
    rows = []
    for k in range(rng.randint(900, 1500)):
        c = rng.choices(codes, weight)[0]
        o, d = rng.sample(AIRPORTS, 2)
        s = dt.datetime(2023, 6, 1) + dt.timedelta(minutes=5 * rng.randint(0, (92 * 24 * 60) // 5 - 1))
        if rng.random() < 0.03:
            act = ""
        else:
            delay = rng.randint(16, 190) if rng.random() < late_p[c] else rng.randint(-8, 15)
            act = (s + dt.timedelta(minutes=delay)).strftime("%Y-%m-%d %H:%M")
        rows.append(["F%05d" % (k + 1), c, o, d, s.strftime("%Y-%m-%d %H:%M"), act, dist[(o, d)]])
    return {"flights.csv": write_csv(["flight_id", "carrier_code", "origin", "dest", "sched_dep", "actual_dep",
                                      "distance_km"], rows),
            "carriers.csv": write_csv(["carrier_code", "carrier_name"], list(zip(codes, names)))}


def delay_min(x):
    return (T(x["actual_dep"]) - T(x["sched_dep"])).total_seconds() / 60


def task_fl_late_share(rng):
    f = gen_flights(rng)
    names = {c["carrier_code"]: c["carrier_name"] for c in read_csv(f["carriers.csv"])}
    tot, late = Counter(), Counter()
    for x in read_csv(f["flights.csv"]):
        if not x["actual_dep"] or not x["sched_dep"].startswith("2023-07-"):
            continue
        tot[x["carrier_code"]] += 1
        late[x["carrier_code"]] += delay_min(x) > 15
    share = {names[c]: late[c] / tot[c] for c in tot if tot[c] >= 30}
    ans = margin_argmax(share, absolute=0.01)
    q = ("Flights with an empty actual_dep were cancelled; ignore them. A flight is late if actual_dep is more "
         "than 15 minutes after sched_dep. Among carriers with at least 30 non-cancelled flights scheduled in July "
         "2023, which carrier has the highest share of late flights in July 2023? Write its carrier_name.")
    return f, ["flights.csv", "carriers.csv"], q, ans, {}


def task_fl_weekend_distance(rng):
    f = gen_flights(rng)
    origin = rng.choice(AIRPORTS)
    tot = sum(int(x["distance_km"]) for x in read_csv(f["flights.csv"])
              if x["origin"] == origin and x["actual_dep"] and T(x["sched_dep"]).weekday() >= 5
              and delay_min(x) <= 0)
    if tot == 0:
        raise Reject("zero")
    q = ("What is the total distance_km of the flights from %s scheduled to depart on a Saturday or a Sunday "
         "(by sched_dep) that actually departed on time or early (actual_dep not later than sched_dep)? Flights "
         "with an empty actual_dep were cancelled and do not count." % origin)
    return f, ["flights.csv"], q, tot, {"origin": origin}


def task_fl_median_delay(rng):
    f = gen_flights(rng)
    car = rng.choice(read_csv(f["carriers.csv"]))
    ds = [delay_min(x) for x in read_csv(f["flights.csv"]) if x["carrier_code"] == car["carrier_code"] and x["actual_dep"]]
    med = statistics.median(ds)
    if med <= 0:
        raise Reject("non-positive median")
    q = ("For the carrier named %s (look up its code in carriers.csv), what is the median departure delay in "
         "minutes (actual_dep minus sched_dep; early departures give negative delays), ignoring cancelled flights "
         "(empty actual_dep)? If the median is not a whole number, round it down." % car["carrier_name"])
    return f, ["flights.csv", "carriers.csv"], q, int(math.floor(med)), {"carrier": car["carrier_name"]}


# --- HR
DEPT_NAMES = ["Finance", "Logistics", "Research", "Legal", "Marketing", "Support", "Facilities", "Security"]
LOCS = ["Riga", "Vilnius", "Tallinn", "Helsinki"]


def gen_hr(rng):
    names = rng.sample(DEPT_NAMES, rng.randint(5, 7))
    depts = [["D%02d" % (i + 1), n, rng.choice(LOCS)] for i, n in enumerate(names)]
    base = {d[0]: rng.randint(38000, 70000) for d in depts}
    emps = []
    n = rng.randint(140, 300)
    ids = ["E%04d" % i for i in rng.sample(range(1000, 9999), n)]
    for k, e in enumerate(ids):
        d = rng.choice(depts)[0]
        if k < 3:
            mgr = ""
        else:
            same = [x for x in emps[: max(3, k // 3)] if x[2] == d]
            mgr = rng.choice(same or emps[:3])[0]
        sal = int(base[d] * rng.uniform(0.7, 2.2)) // 100 * 100
        emps.append([e, "%s %s" % (rng.choice(FIRST), rng.choice(LAST)), d, mgr,
                     rdate(rng, D("2005-01-01"), D("2024-06-30")).isoformat(), sal])
    rng.shuffle(emps)
    return {"employees.csv": write_csv(["emp_id", "name", "dept_id", "manager_id", "hire_date", "salary"], emps),
            "departments.csv": write_csv(["dept_id", "dept_name", "location"], depts)}


def task_hr_above_manager(rng):
    f = gen_hr(rng)
    emps = {e["emp_id"]: e for e in read_csv(f["employees.csv"])}
    n = sum(1 for e in emps.values() if e["manager_id"] and int(e["salary"]) > int(emps[e["manager_id"]]["salary"]))
    q = ("The manager_id column holds the emp_id of an employee's direct manager (empty for top managers). How many "
         "employees earn strictly more than their direct manager?")
    return f, ["employees.csv", "departments.csv"], q, n, {}


def task_hr_median_dept(rng):
    f = gen_hr(rng)
    dn = {d["dept_id"]: d["dept_name"] for d in read_csv(f["departments.csv"])}
    g = defaultdict(list)
    for e in read_csv(f["employees.csv"]):
        if D(e["hire_date"]) < D("2018-01-01"):
            g[dn[e["dept_id"]]].append(int(e["salary"]))
    med = {k: statistics.median(v) for k, v in g.items() if len(v) >= 5}
    ans = margin_argmax(med, absolute=500)
    q = ("Considering only employees hired before 2018-01-01, and only departments with at least 5 such "
         "employees, which department (dept_name) has the highest median salary?")
    return f, ["employees.csv", "departments.csv"], q, ans, {}


# --- sensors
SITES = ["north_ridge", "lakeside", "old_mill", "quarry"]


def gen_sensors(rng):
    sensors = []
    for i in range(rng.randint(8, 12)):
        sensors.append(["S%02d" % (i + 1), SITES[i % 4] if i < 4 else rng.choice(SITES),
                        rdate(rng, D("2024-07-20"), D("2024-08-12")).isoformat()])
    site_off = {s: rng.uniform(-2.5, 2.5) for s in SITES}
    rows = []
    for sid, site, _inst in sensors:
        for h in range(31 * 24):
            t = dt.datetime(2024, 8, 1) + dt.timedelta(hours=h)
            if rng.random() < 0.04:
                temp = ""
            else:
                v = 22 + site_off[site] + 7 * math.sin((t.hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 2.2)
                if t < dt.datetime.fromisoformat(_inst):
                    v += rng.uniform(5, 15)  # uncalibrated readings before installation
                temp = "%.1f" % v
            rows.append([sid, t.strftime("%Y-%m-%d %H:%M"), temp])
    return {"sensors.csv": write_csv(["sensor_id", "site", "installed_on"], sensors),
            "readings.csv": write_csv(["sensor_id", "timestamp", "temp_c"], rows)}


def valid_readings(f):
    sens = {s["sensor_id"]: s for s in read_csv(f["sensors.csv"])}
    for r in read_csv(f["readings.csv"]):
        s = sens[r["sensor_id"]]
        if r["temp_c"] and T(r["timestamp"]).date() >= D(s["installed_on"]):
            yield r, s


def task_sens_hot(rng):
    f = gen_sensors(rng)
    site = rng.choice(SITES)
    thr = rng.choice([28.0, 29.0, 30.0])
    n = sum(1 for r, s in valid_readings(f) if s["site"] == site and float(r["temp_c"]) > thr)
    if n < 5:
        raise Reject("too few")
    q = ("Readings with an empty temp_c are missing, and readings dated before a sensor's installed_on date are "
         "uncalibrated; ignore both. How many of the remaining readings from sensors at site %s have temp_c above "
         "%.1f?" % (site, thr))
    return f, ["sensors.csv", "readings.csv"], q, n, {"site": site, "thr": thr}


def task_sens_site_avg(rng):
    f = gen_sensors(rng)
    g = defaultdict(list)
    for r, s in valid_readings(f):
        t = T(r["timestamp"])
        if t.weekday() < 5 and 12 <= t.hour <= 15:
            g[s["site"]].append(float(r["temp_c"]))
    ans = margin_argmax({k: statistics.fmean(v) for k, v in g.items()}, absolute=0.15)
    q = ("Readings with an empty temp_c are missing, and readings dated before a sensor's installed_on date are "
         "uncalibrated; ignore both. Considering only readings taken on weekdays (Monday to Friday) with a "
         "timestamp hour from 12 to 15 inclusive, which site has the highest average temp_c?")
    return f, ["sensors.csv", "readings.csv"], q, ans, {}


SYNTH = [
    ("clinic_dept_cost", task_clinic_dept_cost), ("clinic_distinct_patients", task_clinic_distinct),
    ("clinic_senior_avg", task_clinic_senior_avg), ("lib_late_genre", task_lib_late_genre),
    ("lib_weekend_old", task_lib_weekend_old), ("lib_top_member", task_lib_top_member),
    ("fl_late_share", task_fl_late_share), ("fl_weekend_distance", task_fl_weekend_distance),
    ("fl_median_delay", task_fl_median_delay), ("hr_above_manager", task_hr_above_manager),
    ("hr_median_dept", task_hr_median_dept), ("sens_hot_readings", task_sens_hot),
    ("sens_site_avg", task_sens_site_avg),
]


def synth_tasks():
    tasks = []
    for name, fn in SYNTH:
        for attempt in range(50):
            rng = rng_for("data", name, attempt)
            try:
                files, order, q, ans, meta = fn(rng)
            except Reject:
                continue
            check = "exact_int" if isinstance(ans, int) else "exact_str"
            instr = "%s %s %s" % (describe(files, order), q, DATA_TAIL)
            meta.update({"source": "synthetic", "generator": name, "attempt": attempt})
            tasks.append({"type": "data_analysis", "kind": name, "feasible": True, "instruction": instr,
                          "check": check, "expected": str(ans), "files": files, "meta": meta})
            break
        else:
            raise SystemExit("data: generator %s failed" % name)
    return tasks


# ----------------------------------------------------------------------------- infeasible
def infeasible_tasks(dl):
    out = []
    with open(os.path.join(dl, "dabench_tables", "hotel_data.csv"), "rb") as fh:
        hotel = {"hotel_data.csv": fh.read()}
    assert "parking" not in header_and_count(hotel["hotel_data.csv"])[0]
    out.append(("missing_column", hotel, ["hotel_data.csv"],
                "How many hotels in New York City offer free parking (parking column equal to 'free')?",
                {"source": "infiagent-dabench-dev table"}))
    f = gen_clinic(rng_for("data", "inf_clinic"))
    assert all(p["city"] != "Oslo" for p in read_csv(f["patients.csv"]))
    out.append(("absent_entity", f, ["patients.csv", "visits.csv"],
                "Which department has the highest total cost of visits by patients living in Oslo?",
                {"source": "synthetic"}))
    f = gen_flights(rng_for("data", "inf_flights"))
    del f["carriers.csv"]
    out.append(("missing_file", f, ["flights.csv"],
                "Using the carrier names listed in carriers.csv, which carrier_name operated the largest number of "
                "flights from OSL in August 2023?", {"source": "synthetic"}))
    f = gen_library(rng_for("data", "inf_library"))
    assert all(not x["loan_date"].startswith("2021-") for x in read_csv(f["loans.csv"]))
    out.append(("absent_period", f, ["books.csv", "loans.csv"],
                "Which genre had the largest number of loans with a loan_date in March 2021?",
                {"source": "synthetic"}))
    tasks = []
    for kind, files, order, q, meta in out:
        tasks.append({"type": "data_analysis", "kind": kind, "feasible": False,
                      "instruction": "%s %s %s" % (describe(files, order), q, DATA_TAIL),
                      "check": "exact_int" if q.startswith("How many") else "exact_str", "expected": None,
                      "files": files, "meta": meta})
    return tasks


def make(dl):
    a, dropped = dabench_tasks(dl)
    b = synth_tasks()
    c = infeasible_tasks(dl)
    print("data_analysis: DABench %d verified (dropped %s), synthetic %d, infeasible %d"
          % (len(a), [d[0] for d in dropped] or "none", len(b), len(c)))
    return a + b + c


if __name__ == "__main__":
    for t in make(sys.argv[1]):
        print(t["kind"], repr(t["expected"]), "|", t["instruction"][:-len(DATA_TAIL)][-260:])

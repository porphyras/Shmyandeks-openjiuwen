#!/usr/bin/env python3
"""Generate task sets in the format of the organizer examples (data/examples/task-dir).

Usage:
  python3 bench/gen_tasks.py --out bench/sets/<name> --n 200 --seed 1 --infeasible 0.2
        [--mix math=0.25,data_analysis=0.25,search=0.25,coding=0.25] [--size small|mixed|large] [--easy]

Output:
  <out>/tasks/<task_id>/task.json (+ workspace/...)   what the harness sees (no answers)
  <out>/keys.json                                     answers / checks / kinds
  <out>/hidden/<task_id>/test_hidden.py, mylib_ref.py coding: stricter hidden test, reference fix
  <out>/oracle_mock.json                              [substring, reply] pairs for the mock proxy
  <out>/manifest.json                                 generation parameters and counts

Every feasible answer is recomputed by an independent checker that parses the written
instruction and reads the written files; coding tasks are executed (buggy fails visible,
reference passes visible + hidden, a visible-input special-casing fix (negative control) fails hidden;
infeasible coding: the reference fails only because of the contradiction).
"""
import argparse
import copy
import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
from fractions import Fraction

sys.dont_write_bytecode = True  # keep bench/ free of __pycache__
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from grade import run_test_file  # noqa: E402

TYPES = ["math", "data_analysis", "search", "coding"]

TAIL = ("Work only inside the current directory. When you are done, write ONLY the final answer "
        "to the file answer.txt in the current directory (for example with bash: echo 42 > answer.txt).")
MATH_TAIL = "Compute it with the bash tool (python3 -c). " + TAIL
DATA_HEAD = "The file sales.csv has columns order_id,region,product,month,units,price ({rows} rows)."
DATA_TAIL = "Use bash with python3 to compute it. " + TAIL
SEARCH_HEAD = "The directory kb/ contains a small knowledge base of markdown files."
SEARCH_TAIL = "Use the file tools or bash (ls, grep, cat) to find it. " + TAIL
CODING_INSTRUCTION = (
    "Run `python3 test_mylib.py` in the current directory; some tests fail. Fix the bugs in mylib.py "
    "(do not edit the tests) until all tests pass. Work only inside the current directory. When you are "
    "done, write ONLY the word DONE to the file answer.txt in the current directory (for example with "
    "bash: echo 42 > answer.txt).")

INFEASIBLE_KINDS = {
    "math": [("split_zero", 3), ("vague_quantity", 4), ("unknown_source", 2),
             ("negative_stock", 1), ("stated_contradiction", 2)],
    "data_analysis": [("absent_product", 3), ("absent_region", 2), ("absent_month", 1),
                      ("missing_column", 4)],
    "search": [("person_absent", 4), ("company_file_missing", 3), ("attr_not_in_kb", 2),
               ("attr_line_missing", 2), ("mentor_absent", 1)],
    "coding": [("contradict_same_method", 3), ("contradict_two_methods", 2),
               ("impossible_property", 2), ("missing_function", 2)],
}


def make_rng(label):
    """Deterministic RNG; the label goes through SHA-256 so that similar labels are decorrelated."""
    return random.Random(int.from_bytes(hashlib.sha256(label.encode()).digest(), "big"))


def weighted(rng, pairs):
    total = sum(w for _, w in pairs)
    x = rng.random() * total
    for item, w in pairs:
        x -= w
        if x < 0:
            return item
    return pairs[-1][0]


class GenError(Exception):
    """A random draw did not work out; the generator retries with another sub-seed."""


class TemplateError(Exception):
    """A bug in the generator itself (never retried)."""


# =============================================================================================
# MATH
# =============================================================================================

BASE_OPS = ["ship", "receive", "split", "double"]
MATH_PATTERN_WEIGHTS = [("split_double", 3), ("ship_receive", 3), ("mixed", 4)]
EXT_OPS = ["lose_pct", "grow_pct", "sell_half_up", "send_stores", "returns", "triple", "damaged"]
MATH_Q = "How many boxes does it have at the end?"


def math_kind(op_names):
    """split_double / ship_receive / mixed, with _ext when non-example operations occur."""
    ext = any(o not in BASE_OPS for o in op_names)
    core = {o for o in op_names if o in BASE_OPS}
    if core <= {"split", "double"} and not ext:
        return "split_double"
    if core <= {"ship", "receive"}:
        return "ship_receive" + ("_ext" if ext else "")
    return "mixed" + ("_ext" if ext else "")


def math_ops_from_text(instruction):
    body = instruction.split(" Compute it with the bash tool")[0]
    out = []
    for sent in re.split(r"(?<=[.?])\s+", body.strip())[1:]:
        for name, pat, _fn in _MATH_STEP_RE:
            if re.fullmatch(pat, sent):
                out.append(name)
                break
    return out


def math_apply(x, op):
    k = op["op"]
    if k == "ship":
        return x - op["a"] * op["d"]
    if k == "receive":
        return x + op["k"] * op["m"]
    if k == "split":
        return x // op["w"]
    if k == "double":
        return x * 2
    if k == "triple":
        return x * 3
    if k == "lose_pct":
        return x - x * op["p"] // 100
    if k == "grow_pct":
        return x + x * op["p"] // 100
    if k == "sell_half_up":
        return x - (x + 1) // 2
    if k == "send_stores":
        return x - op["s"] * op["n"]
    if k == "returns":
        return x + op["r"]
    if k == "damaged":
        return x + op["k"] * (op["m"] - op["b"])
    if k == "leftover":
        return x % op["c"]
    raise TemplateError("bad op " + k)


def math_render(op):
    k = op["op"]
    alt = op.get("alt", False)
    vague = op.get("vague")
    then_it = "Then it" if alt else "It"
    if k == "ship":
        a = "some" if vague == "a" else str(op["a"])
        d = "several" if vague == "d" else str(op["d"])
        return "%s ships %s boxes per day for %s days." % (then_it, a, d)
    if k == "receive":
        kk = "a few" if vague == "k" else str(op["k"])
        m = "some" if vague == "m" else str(op["m"])
        return "%s receives %s deliveries of %s boxes each." % (then_it, kk, m)
    if k == "split":
        if vague == "w":
            return "Then the stock is split equally among several warehouses and it keeps one share (rounded down)."
        if alt:
            return "The stock is then split equally into %d warehouses and it keeps one share (rounded down)." % op["w"]
        return "Then the stock is split equally into %d warehouses and it keeps one share (rounded down)." % op["w"]
    if k == "double":
        return "Then the stock is doubled by merging with a sister warehouse."
    if k == "triple":
        return "Then the stock is tripled by merging with two sister warehouses of the same size."
    if k == "lose_pct":
        if vague == "p":
            return "Then part of the stock is lost to damage."
        return ("Then %d percent of the stock is lost to damage (the number of lost boxes is rounded down)."
                % op["p"])
    if k == "grow_pct":
        return "Then the stock grows by %d percent (rounded down)." % op["p"]
    if k == "sell_half_up":
        return "Then it sells half of the stock (the number of boxes sold is rounded up)."
    if k == "send_stores":
        n = "several" if vague == "n" else str(op["n"])
        return "%s sends %d boxes to each of %s stores." % (then_it, op["s"], n)
    if k == "returns":
        return "Customers return %d boxes, which are added back to the stock." % op["r"]
    if k == "damaged":
        return ("%s receives %d deliveries of %d boxes each, but %d boxes in each delivery are damaged and "
                "thrown away." % (then_it, op["k"], op["m"], op["b"]))
    if k == "leftover":
        return ("Then it packs the stock into crates of %d boxes, ships all full crates and keeps only the "
                "leftover boxes." % op["c"])
    if k == "unknown_source":
        return op["text"]
    if k == "stated":
        return "After that it has exactly %d boxes." % op["value"]
    raise TemplateError("bad op " + k)


def math_make_op(rng, kind, x, alt_p):
    alt = rng.random() < alt_p
    if kind == "ship":
        for _ in range(40):
            a, d = rng.randint(5, 80), rng.randint(2, 7)
            if a * d <= x - 5:
                return {"op": "ship", "a": a, "d": d, "alt": alt}
        return None
    if kind == "receive":
        return {"op": "receive", "k": rng.randint(2, 5), "m": rng.randint(20, 150), "alt": alt}
    if kind == "split":
        w = rng.randint(2, 5)
        return {"op": "split", "w": w, "alt": alt} if x >= 10 * w else None
    if kind == "double":
        return {"op": "double"} if x < 200000 else None
    if kind == "triple":
        return {"op": "triple"} if x < 100000 else None
    if kind == "lose_pct":
        return {"op": "lose_pct", "p": rng.choice([5, 10, 15, 20, 25, 30, 40, 50])} if x >= 20 else None
    if kind == "grow_pct":
        return {"op": "grow_pct", "p": rng.choice([5, 10, 15, 20, 25, 50])} if x < 200000 else None
    if kind == "sell_half_up":
        return {"op": "sell_half_up"} if x >= 20 else None
    if kind == "send_stores":
        for _ in range(40):
            s, n = rng.randint(5, 60), rng.randint(2, 6)
            if s * n <= x - 5:
                return {"op": "send_stores", "s": s, "n": n, "alt": alt}
        return None
    if kind == "returns":
        return {"op": "returns", "r": rng.randint(3, 90)}
    if kind == "damaged":
        m = rng.randint(30, 150)
        return {"op": "damaged", "k": rng.randint(2, 5), "m": m, "b": rng.randint(1, min(15, m - 1)), "alt": alt}
    raise TemplateError(kind)


def gen_math(rng, opts, inf_kind):
    size, easy = opts["size"], opts["easy"]
    if easy:
        lo, hi = 2, 3
    elif size == "small":
        lo, hi = 2, 5
    elif size == "large":
        lo, hi = 4, 7
    else:
        lo, hi = 2, 7
    nsteps = rng.randint(lo, hi)
    x0 = rng.randint(100, 5000)
    # the two examples are "pure" chains (split/double only; ship/receive only)
    pattern = opts.get("family") or weighted(rng, MATH_PATTERN_WEIGHTS)
    ext = (not easy) and pattern != "split_double" and rng.random() < 0.45
    alternate = rng.random() < 0.6
    alt_p = 0.0 if easy else 0.2
    x = x0
    ops = []
    while len(ops) < nsteps:
        if ext and rng.random() < 0.4:
            kind = rng.choice(EXT_OPS)
        elif pattern == "split_double":
            kind = (["split", "double"][len(ops) % 2] if alternate else rng.choice(["split", "double"]))
        elif pattern == "ship_receive":
            kind = rng.choice(["ship", "receive"])
        else:
            kind = weighted(rng, [("ship", 3), ("receive", 3), ("split", 2), ("double", 2)])
        op = math_make_op(rng, kind, x, alt_p)
        if op is None and pattern == "split_double":
            op = math_make_op(rng, "double", x, alt_p)
        if op is None:
            continue
        x = math_apply(x, op)
        ops.append(op)
    question = MATH_Q
    if not easy and inf_kind is None:
        r = rng.random()
        if r < 0.06 and x >= 30:
            c = rng.choice([6, 8, 10, 12, 20, 24])
            question = "How many full crates of %d boxes can it fill at the end?" % c
            x = x // c
        elif r < 0.10 and x >= 30:
            op = {"op": "leftover", "c": rng.choice([7, 9, 12, 16, 25, 40])}
            x = math_apply(x, op)
            ops.append(op)
    start = "A warehouse starts with %d boxes." % x0
    kind = math_kind([o["op"] for o in ops])
    expected = str(x)
    feasible = True

    if inf_kind:
        feasible, expected, kind = False, None, inf_kind
        if inf_kind == "split_zero":
            pos = rng.randint(0, len(ops))
            ops.insert(pos, {"op": "split", "w": 0, "alt": rng.random() < 0.2})
        elif inf_kind == "vague_quantity":
            cands = []
            for i, o in enumerate(ops):
                if o["op"] == "ship":
                    cands += [(i, "a"), (i, "d")]
                elif o["op"] == "receive":
                    cands += [(i, "k"), (i, "m")]
                elif o["op"] == "split":
                    cands.append((i, "w"))
                elif o["op"] == "lose_pct":
                    cands.append((i, "p"))
                elif o["op"] == "send_stores":
                    cands.append((i, "n"))
            cands.append((-1, "start"))
            i, field = rng.choice(cands)
            if field == "start":
                start = "A warehouse starts with some boxes."
            else:
                ops[i] = dict(ops[i], vague=field)
        elif inf_kind == "unknown_source":
            text = rng.choice([
                "Then it receives all the boxes held by a partner depot.",
                "Then it takes over the entire stock of a second warehouse in another town.",
                "Then a truck arrives with an unrecorded number of boxes, which are added to the stock.",
                "It receives one more delivery whose size was not written down.",
            ])
            ops.insert(rng.randint(1, len(ops)), {"op": "unknown_source", "text": text})
        elif inf_kind == "negative_stock":
            pos = rng.randint(0, len(ops))
            y = x0
            for o in ops[:pos]:
                y = math_apply(y, o)
            d = rng.randint(3, 7)
            a = y // d + rng.randint(20, 200)
            ops.insert(pos, {"op": "ship", "a": a, "d": d, "alt": rng.random() < 0.3})
        elif inf_kind == "stated_contradiction":
            pos = rng.randint(1, len(ops))
            y = x0
            for o in ops[:pos]:
                y = math_apply(y, o)
            wrong = y + rng.choice([-1, 1]) * rng.randint(3, max(4, y // 4))
            if wrong <= 0 or wrong == y:
                wrong = y + rng.randint(3, 50)
            ops.insert(pos, {"op": "stated", "value": wrong})
        else:
            raise TemplateError(inf_kind)

    problem = " ".join([start] + [math_render(o) for o in ops] + [question])
    return {
        "kind": kind, "feasible": feasible, "check": "exact_int", "expected": expected,
        "instruction": problem + " " + MATH_TAIL, "key": problem, "files": {},
        "meta": {"steps": len([o for o in ops if o["op"] not in ("stated",)]),
                 "ops": [o["op"] for o in ops]},
    }


# Independent checker: parses the instruction text sentence by sentence.
_MATH_STEP_RE = [
    ("ship", r"(?:Then it|It) ships (\d+) boxes per day for (\d+) days\.", lambda x, a, d: x - a * d),
    ("receive", r"(?:Then it|It) receives (\d+) deliveries of (\d+) boxes each\.", lambda x, k, m: x + k * m),
    ("split", r"(?:Then the stock is|The stock is then) split equally into (\d+) warehouses and it keeps one share "
     r"\(rounded down\)\.", lambda x, w: x // w),
    ("double", r"Then the stock is doubled by merging with a sister warehouse\.", lambda x: 2 * x),
    ("triple", r"Then the stock is tripled by merging with two sister warehouses of the same size\.", lambda x: 3 * x),
    ("lose_pct", r"Then (\d+) percent of the stock is lost to damage \(the number of lost boxes is rounded down\)\.",
     lambda x, p: x - (x * p) // 100),
    ("grow_pct", r"Then the stock grows by (\d+) percent \(rounded down\)\.", lambda x, p: x + (x * p) // 100),
    ("sell_half_up", r"Then it sells half of the stock \(the number of boxes sold is rounded up\)\.",
     lambda x: x - -(-x // 2)),
    ("send_stores", r"(?:Then it|It) sends (\d+) boxes to each of (\d+) stores\.", lambda x, s, n: x - s * n),
    ("returns", r"Customers return (\d+) boxes, which are added back to the stock\.", lambda x, r: x + r),
    ("damaged", r"(?:Then it|It) receives (\d+) deliveries of (\d+) boxes each, but (\d+) boxes in each delivery are "
     r"damaged and thrown away\.", lambda x, k, m, b: x + k * m - k * b),
    ("leftover", r"Then it packs the stock into crates of (\d+) boxes, ships all full crates and keeps only the "
     r"leftover boxes\.", lambda x, c: x - (x // c) * c),
]


def verify_math(instruction):
    """Return the answer as a string, or None if the problem is not solvable as stated."""
    body = instruction.split(" Compute it with the bash tool")[0]
    sents = re.split(r"(?<=[.?])\s+", body.strip())
    m = re.fullmatch(r"A warehouse starts with (\d+) boxes\.", sents[0])
    if not m:
        return None
    x = int(m.group(1))
    for s in sents[1:]:
        q = re.fullmatch(r"How many boxes does it have at the end\?", s)
        if q:
            return str(x)
        q = re.fullmatch(r"How many full crates of (\d+) boxes can it fill at the end\?", s)
        if q:
            c = int(q.group(1))
            return str(x // c) if c > 0 else None
        st = re.fullmatch(r"After that it has exactly (\d+) boxes\.", s)
        if st:
            if int(st.group(1)) != x:
                return None
            continue
        for _name, pat, fn in _MATH_STEP_RE:
            mm = re.fullmatch(pat, s)
            if mm:
                try:
                    x = fn(x, *[int(g) for g in mm.groups()])
                except ZeroDivisionError:
                    return None
                break
        else:
            return None
        if x < 0:
            return None
    return None


# =============================================================================================
# DATA ANALYSIS
# =============================================================================================

BASE_REGIONS = ["north", "south", "east", "west"]
BASE_PRODUCTS = ["widget", "sprocket", "gizmo", "gadget"]
EXTRA_REGIONS = ["central", "northeast", "southwest"]
EXTRA_PRODUCTS = ["gear", "valve", "bracket", "flange"]
ABSENT_PRODUCTS = ["doohickey", "gear", "valve", "bracket", "flange", "bolt", "spindle"]
ABSENT_REGIONS = ["central", "northeast", "southwest", "midwest", "coastal"]

DATA_Q = {
    "units_months": [
        "How many units of product '{p}' were sold in months {a} through {b} inclusive?",
        "In total, how many units of product '{p}' were sold in months {a} through {b} inclusive?",
    ],
    "units_months_region": ["How many units of product '{p}' were sold in region '{r}' in months {a} through {b} inclusive?"],
    "units_region": ["How many units of product '{p}' were sold in region '{r}'?"],
    "revenue_region": ["What is the total revenue (sum of units * price) for region '{r}'?"],
    "revenue_product": ["What is the total revenue (sum of units * price) for product '{p}'?"],
    "order_count": ["How many orders are there for product '{p}'?"],
    "order_count_region": ["How many orders were placed in region '{r}'?"],
    "avg_price_floor": ["What is the average price of product '{p}', rounded down to an integer?"],
    "avg_price_round": ["What is the average price of product '{p}', rounded to the nearest integer?"],
    "max_units_region": ["What is the largest number of units in a single order in region '{r}'?"],
}
DATA_FAMILY_WEIGHTS = [("units_months", 55), ("units_months_region", 7), ("units_region", 9),
                       ("revenue_region", 9), ("revenue_product", 5), ("order_count", 8),
                       ("order_count_region", 4), ("avg_price_floor", 5), ("avg_price_round", 4),
                       ("max_units_region", 7)]
MISSING_COLUMN_Q = [
    "What is the total discount given on orders of product '{p}'?",
    "What is the average customer rating of product '{p}', rounded down to an integer?",
    "How many distinct customers bought product '{p}'?",
    "What is the total shipping cost for orders in region '{r}'?",
    "How many units of product '{p}' were sold in {y}?",
    "What is the total profit (revenue minus cost) for region '{r}'?",
    "How many units of product '{p}' were returned in months {a} through {b} inclusive?",
    "What is the total tax paid on orders in region '{r}'?",
]


def data_rows_count(rng, size, easy):
    small = [20, 25, 30, 40, 50, 60, 75, 80, 100, 120, 150, 200, 250, 300]
    big = [400, 500, 600, 750, 800, 1000, 1200, 1500, 1800, 2000]
    if easy or size == "small":
        return rng.choice(small)
    if size == "large":
        return rng.choice(big)
    return rng.choice(big) if rng.random() < 0.15 else rng.choice(small)


def data_compute(family, rows, p=None, r=None, a=None, b=None):
    sel = rows
    if p is not None:
        sel = [x for x in sel if x[2] == p]
    if r is not None:
        sel = [x for x in sel if x[1] == r]
    if a is not None:
        sel = [x for x in sel if a <= x[3] <= b]
    if family in ("units_months", "units_months_region", "units_region"):
        return sum(x[4] for x in sel)
    if family in ("revenue_region", "revenue_product"):
        return sum(x[4] * x[5] for x in sel)
    if family in ("order_count", "order_count_region"):
        return len(sel)
    if family in ("avg_price_floor", "avg_price_round"):
        if not sel:
            return None
        avg = Fraction(sum(x[5] for x in sel), len(sel))
        if family == "avg_price_floor":
            return avg.numerator // avg.denominator
        if avg - (avg.numerator // avg.denominator) == Fraction(1, 2):
            return None  # tie: wording would be ambiguous
        return int(avg + Fraction(1, 2)) if avg >= 0 else None
    if family == "max_units_region":
        return max((x[4] for x in sel), default=None)
    raise TemplateError(family)


def month_range(rng):
    a = rng.randint(1, 11)
    b = min(12, a + weighted(rng, [(1, 5), (2, 3), (3, 2), (4, 1), (5, 1)]))
    return a, b


def gen_data(rng, opts, inf_kind):
    size, easy = opts["size"], opts["easy"]
    n = data_rows_count(rng, size, easy)
    regions, products = list(BASE_REGIONS), list(BASE_PRODUCTS)
    if not easy and rng.random() < 0.2:
        if rng.random() < 0.5:
            products.append(rng.choice(EXTRA_PRODUCTS))
        else:
            regions.append(rng.choice(EXTRA_REGIONS))
    rows = [(i, rng.choice(regions), rng.choice(products), rng.randint(1, 12), rng.randint(1, 50),
             rng.randint(10, 99)) for i in range(1, n + 1)]
    pres_p = sorted({x[2] for x in rows})
    pres_r = sorted({x[1] for x in rows})
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["order_id", "region", "product", "month", "units", "price"])
    w.writerows(rows)
    csv_text = buf.getvalue()

    feasible, expected = True, None
    if inf_kind is None:
        for _ in range(200):
            fam = "units_months" if easy else (opts.get("family") or weighted(rng, DATA_FAMILY_WEIGHTS))
            p, r = rng.choice(pres_p), rng.choice(pres_r)
            a, b = month_range(rng)
            args = {"p": p if "{p}" in DATA_Q[fam][0] else None,
                    "r": r if "{r}" in DATA_Q[fam][0] else None,
                    "a": a if "{a}" in DATA_Q[fam][0] else None,
                    "b": b if "{a}" in DATA_Q[fam][0] else None}
            val = data_compute(fam, rows, **args)
            if val is None or val == 0:
                continue
            tmpl = DATA_Q[fam][0] if (len(DATA_Q[fam]) == 1 or rng.random() < 0.85) else rng.choice(DATA_Q[fam][1:])
            question = tmpl.format(p=p, r=r, a=a, b=b)
            expected = str(val)
            kind = fam
            break
        else:
            raise GenError("no data question")
    else:
        feasible, kind = False, inf_kind
        a, b = month_range(rng)
        if inf_kind == "absent_product":
            p = rng.choice([x for x in ABSENT_PRODUCTS if x not in pres_p])
            fam = rng.choice(["units_months", "units_months", "order_count", "avg_price_floor", "units_region",
                              "revenue_product"])
            question = DATA_Q[fam][0].format(p=p, r=rng.choice(pres_r), a=a, b=b)
        elif inf_kind == "absent_region":
            r = rng.choice([x for x in ABSENT_REGIONS if x not in pres_r])
            fam = rng.choice(["revenue_region", "units_region", "max_units_region", "order_count_region",
                              "units_months_region"])
            question = DATA_Q[fam][0].format(p=rng.choice(pres_p), r=r, a=a, b=b)
        elif inf_kind == "absent_month":
            a = rng.randint(13, 15)
            b = a + rng.randint(1, 2)
            question = DATA_Q["units_months"][0].format(p=rng.choice(pres_p), a=a, b=b)
        elif inf_kind == "missing_column":
            question = rng.choice(MISSING_COLUMN_Q).format(p=rng.choice(pres_p), r=rng.choice(pres_r),
                                                           y=rng.randint(2019, 2024), a=a, b=b)
        else:
            raise TemplateError(inf_kind)
    head = DATA_HEAD.format(rows=n)
    return {
        "kind": kind, "feasible": feasible, "check": "exact_int", "expected": expected,
        "instruction": "%s %s %s" % (head, question, DATA_TAIL), "key": "%s %s" % (head, question),
        "files": {"workspace/sales.csv": csv_text},
        "meta": {"rows": n},
    }


_DATA_VERIFY = [
    (r"(?:In total, how|How) many units of product '(\w+)' were sold in months (\d+) through (\d+) inclusive\?",
     ("p", "a", "b"), lambda rs, p, a, b: sum(int(x["units"]) for x in rs if x["product"] == p and a <= int(x["month"]) <= b)),
    (r"How many units of product '(\w+)' were sold in region '(\w+)' in months (\d+) through (\d+) inclusive\?",
     ("p", "r", "a", "b"), lambda rs, p, r, a, b: sum(int(x["units"]) for x in rs if x["product"] == p
                                                      and x["region"] == r and a <= int(x["month"]) <= b)),
    (r"How many units of product '(\w+)' were sold in region '(\w+)'\?",
     ("p", "r"), lambda rs, p, r: sum(int(x["units"]) for x in rs if x["product"] == p and x["region"] == r)),
    (r"What is the total revenue \(sum of units \* price\) for region '(\w+)'\?",
     ("r",), lambda rs, r: sum(int(x["units"]) * int(x["price"]) for x in rs if x["region"] == r)),
    (r"What is the total revenue \(sum of units \* price\) for product '(\w+)'\?",
     ("p",), lambda rs, p: sum(int(x["units"]) * int(x["price"]) for x in rs if x["product"] == p)),
    (r"How many orders are there for product '(\w+)'\?",
     ("p",), lambda rs, p: sum(1 for x in rs if x["product"] == p)),
    (r"How many orders were placed in region '(\w+)'\?",
     ("r",), lambda rs, r: sum(1 for x in rs if x["region"] == r)),
    (r"What is the average price of product '(\w+)', rounded down to an integer\?",
     ("p",), lambda rs, p: (lambda v: sum(v) // len(v))([int(x["price"]) for x in rs if x["product"] == p])),
    (r"What is the average price of product '(\w+)', rounded to the nearest integer\?",
     ("p",), lambda rs, p: (lambda v: (2 * sum(v) + len(v)) // (2 * len(v)))(
         [int(x["price"]) for x in rs if x["product"] == p])),
    (r"What is the largest number of units in a single order in region '(\w+)'\?",
     ("r",), lambda rs, r: max(int(x["units"]) for x in rs if x["region"] == r)),
]


def verify_data(instruction, csv_path):
    m = re.match(r"The file sales\.csv has columns (\S+) \((\d+) rows\)\. (.*?\?) Use bash", instruction)
    if not m:
        return None
    cols, nrows, q = m.group(1).split(","), int(m.group(2)), m.group(3)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
        if rd.fieldnames != cols or len(rows) != nrows:
            raise TemplateError("csv header/row count mismatch")
    products = {x["product"] for x in rows}
    regions = {x["region"] for x in rows}
    for pat, names, fn in _DATA_VERIFY:
        mm = re.fullmatch(pat, q)
        if not mm:
            continue
        vals = {}
        for nm, g in zip(names, mm.groups()):
            vals[nm] = int(g) if nm in ("a", "b") else g
        if "p" in vals and vals["p"] not in products:
            return None
        if "r" in vals and vals["r"] not in regions:
            return None
        if "a" in vals and not (1 <= vals["a"] <= vals["b"] <= 12):
            return None
        return str(fn(rows, *[vals[nm] for nm in names]))
    return None


# =============================================================================================
# SEARCH
# =============================================================================================

FIRST_BASE = ["Alice", "Boris", "Chen", "Dmitri", "Farid", "Grace", "Hiro", "Irina", "Jonas", "Kira", "Maya",
              "Nikolai", "Olga", "Pavel", "Quinn", "Rosa", "Sergei", "Tanya"]
FIRST_EXT = ["Ana", "Emil", "Elena", "Felix", "Ingrid", "Ivan", "Kenji", "Lars", "Leila", "Lucia", "Mateo",
             "Mei", "Nadia", "Noor", "Omar", "Priya", "Ravi", "Sofia", "Tomas", "Vera", "Yusuf", "Zara"]
LAST_BASE = ["Garcia", "Haddad", "Ivanova", "Kowalski", "Muller", "Novak", "Petrov", "Sato", "Smith", "Wang"]
LAST_EXT = ["Costa", "Fischer", "Horvat", "Jensen", "Kaur", "Kim", "Lindqvist", "Lopez", "Mendez", "Moreau",
            "Nakamura", "Okafor", "Rossi", "Silva", "Yilmaz"]
COMPANY_BASE = ["Arktika Labs", "Bluefin Systems", "Cobalt Works", "Dune Robotics", "Ember Bio", "Fjord Data",
                "Granite Analytics", "Helix Motors", "Iris Optics", "Juniper Foods"]
COMPANY_PREFIX = ["Kestrel", "Lumen", "Meridian", "Nimbus", "Onyx", "Pioneer", "Quartz", "Redwood", "Summit",
                  "Tidal", "Umber", "Vertex", "Willow", "Xenon", "Yarrow", "Zephyr", "Aurora", "Basalt", "Cedar",
                  "Delta", "Falcon", "Garnet", "Harbor", "Indigo", "Jasper", "Lotus", "Nova", "Opal", "Prism",
                  "Raven", "Sierra", "Topaz", "Alder", "Beacon", "Cinder", "Drift", "Flint", "Mosaic"]
COMPANY_SUFFIX = ["Labs", "Systems", "Works", "Robotics", "Bio", "Data", "Motors", "Optics", "Foods", "Energy",
                  "Pharma", "Logistics", "Cloud", "Mining", "Textiles", "Capital", "Aero", "Marine", "Networks",
                  "Materials"]
CITIES = ["Medan", "Tbilisi", "Hanoi", "Osaka", "Quito", "Izmir", "Lisbon", "Accra", "Leeds", "Graz", "Tallinn",
          "Porto", "Riga", "Cusco", "Dakar", "Perth", "Bergen", "Kyoto", "Lyon", "Turin", "Cebu", "Lagos", "Malmo",
          "Davao", "Oaxaca", "Krakow", "Valencia", "Nagoya", "Bilbao", "Cordoba", "Gdansk", "Mombasa", "Busan",
          "Tartu", "Ghent"]
ROLES = ["Engineer", "Analyst", "Designer", "Manager", "Researcher", "Accountant", "Technician", "Consultant"]
INDUSTRIES = ["Manufacturing", "Software", "Biotech", "Retail", "Logistics", "Energy", "Finance", "Media"]

SEARCH_Q = {
    "born": "In which year was {P} born?",
    "employer": "Which company does {P} work for?",
    "hq": "In which city is the headquarters of {P}'s employer?",
    "founded": "In which year was {P}'s employer founded?",
    "mentor_employer": "Which company does {P}'s mentor work for?",
    "mentor_born": "In which year was {P}'s mentor born?",
    "mentor_hq": "In which city is the headquarters of the employer of {P}'s mentor?",
}
SEARCH_WEIGHTS = [("born", 25), ("employer", 10), ("hq", 30), ("founded", 12), ("mentor_employer", 10),
                  ("mentor_born", 7), ("mentor_hq", 6)]
SEARCH_HOPS = {"born": 1, "employer": 1, "hq": 2, "founded": 2, "mentor_employer": 2, "mentor_born": 2,
               "mentor_hq": 3}
ATTR_NOT_IN_KB_Q = [
    "What is the email address of {P}?",
    "What is {P}'s phone number?",
    "Which university did {P} graduate from?",
    "How many employees does {P}'s employer have?",
    "Who is the CEO of {P}'s employer?",
    "What is the annual revenue of {P}'s employer?",
    "In which city was {P} born?",
]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def kb_sizes(rng, size, easy):
    """(companies, persons). The examples have 10 companies and 26-28 persons."""
    r = rng.random()
    if size == "large" and not easy:
        total = rng.randint(100, 200)
    elif (size == "mixed" and r < 0.6) or (size == "small" and r < 0.5) or easy:
        return 10, rng.randint(24, 30)
    elif size == "small" or r < 0.85:
        total = rng.randint(20, 80)
    else:
        total = rng.randint(80, 200)
    n_comp = min(len(COMPANY_BASE) + len(COMPANY_PREFIX), max(5, round(total * rng.uniform(0.2, 0.3))))
    return n_comp, total - n_comp


def build_kb(rng, size, easy):
    n_comp, n_pers = kb_sizes(rng, size, easy)
    if n_comp <= 10:
        companies = rng.sample(COMPANY_BASE, n_comp)
    else:
        prefixes = rng.sample(COMPANY_PREFIX, n_comp - 10)
        companies = list(COMPANY_BASE) + ["%s %s" % (pf, rng.choice(COMPANY_SUFFIX)) for pf in prefixes]
    n_first = max(6, min(len(FIRST_BASE) + len(FIRST_EXT), round(n_pers * 0.55)))
    n_last = max(4, min(len(LAST_BASE) + len(LAST_EXT), round(n_pers * 0.33)))
    while n_first * n_last < n_pers * 1.4:
        n_first += 1
        n_last += 1
    firsts = rng.sample(FIRST_BASE, n_first) if n_first <= len(FIRST_BASE) else \
        FIRST_BASE + rng.sample(FIRST_EXT, min(len(FIRST_EXT), n_first - len(FIRST_BASE)))
    lasts = rng.sample(LAST_BASE, n_last) if n_last <= len(LAST_BASE) else \
        LAST_BASE + rng.sample(LAST_EXT, min(len(LAST_EXT), n_last - len(LAST_BASE)))
    combos = sorted("%s %s" % (f, l) for f in firsts for l in lasts)
    people = rng.sample(combos, n_pers)
    employers = list(companies) + [rng.choice(companies) for _ in range(max(0, n_pers - n_comp))]
    employers = employers[:n_pers]
    rng.shuffle(employers)
    extras = (not easy) and rng.random() < 0.2
    kb = {"persons": {}, "companies": {}, "firsts": sorted(firsts), "lasts": sorted(lasts)}
    for c in companies:
        rec = {"Headquarters": rng.choice(CITIES), "Founded": str(rng.randint(1950, 2020))}
        if extras and rng.random() < 0.5:
            rec["Industry"] = rng.choice(INDUSTRIES)
        kb["companies"][c] = rec
    for pname, emp in zip(people, employers):
        mentor = rng.choice([q for q in people if q != pname])
        rec = {"Born": str(rng.randint(1960, 2003)), "Employer": emp, "Mentor": mentor}
        if extras:
            if rng.random() < 0.6:
                rec["Role"] = rng.choice(ROLES)
            if rng.random() < 0.5:
                rec["Hometown"] = rng.choice(CITIES)
        kb["persons"][pname] = rec
    return kb


def render_kb(kb):
    files = {}
    for name, rec in kb["persons"].items():
        lines = ["# " + name, ""] + ["%s: %s" % (k, v) for k, v in rec.items()]
        files["workspace/kb/person_%s.md" % slug(name)] = "\n".join(lines) + "\n"
    for name, rec in kb["companies"].items():
        lines = ["# " + name, ""] + ["%s: %s" % (k, v) for k, v in rec.items()]
        files["workspace/kb/company_%s.md" % slug(name)] = "\n".join(lines) + "\n"
    return dict(sorted(files.items()))


def kb_answer(kb, fam, pname):
    P, C = kb["persons"], kb["companies"]

    def g(d, key, field):
        rec = d.get(key)
        return None if rec is None else rec.get(field)
    if fam == "born":
        return g(P, pname, "Born")
    if fam == "employer":
        return g(P, pname, "Employer")
    if fam == "hq":
        return g(C, g(P, pname, "Employer"), "Headquarters")
    if fam == "founded":
        return g(C, g(P, pname, "Employer"), "Founded")
    if fam == "mentor_employer":
        return g(P, g(P, pname, "Mentor"), "Employer")
    if fam == "mentor_born":
        return g(P, g(P, pname, "Mentor"), "Born")
    if fam == "mentor_hq":
        return g(C, g(P, g(P, pname, "Mentor"), "Employer"), "Headquarters")
    raise TemplateError(fam)


def pick_person(rng, kb):
    names = list(kb["persons"])
    conf = [n for n in names if any(o != n and (o.split()[0] == n.split()[0] or o.split()[1] == n.split()[1])
                                    for o in names)]
    if conf and rng.random() < 0.85:
        return rng.choice(conf)
    return rng.choice(names)


def absent_name(rng, kb):
    present = set(kb["persons"])
    cands = sorted("%s %s" % (f, l) for f in kb["firsts"] for l in kb["lasts"] if "%s %s" % (f, l) not in present)
    if not cands:
        cands = sorted("%s %s" % (f, l) for f in FIRST_EXT for l in kb["lasts"] if "%s %s" % (f, l) not in present)
    return rng.choice(cands)


def gen_search(rng, opts, inf_kind):
    kb = build_kb(rng, opts["size"], opts["easy"])
    feasible, expected = True, None
    if inf_kind is None:
        fam = weighted(rng, [("born", 3), ("employer", 1)]) if opts["easy"] else \
            (opts.get("family") or weighted(rng, SEARCH_WEIGHTS))
        pname = pick_person(rng, kb)
        expected = kb_answer(kb, fam, pname)
        question = SEARCH_Q[fam].format(P=pname)
        kind = fam
    else:
        feasible, kind = False, inf_kind
        pname = pick_person(rng, kb)
        if inf_kind == "person_absent":
            fam = weighted(rng, SEARCH_WEIGHTS)
            question = SEARCH_Q[fam].format(P=absent_name(rng, kb))
        elif inf_kind == "company_file_missing":
            fam = rng.choice(["hq", "hq", "founded", "mentor_hq"])
            holder = kb["persons"][pname]["Mentor"] if fam == "mentor_hq" else pname
            comp = kb["persons"][holder]["Employer"]
            if rng.random() < 0.5:
                del kb["companies"][comp]
            else:  # employer points to a company that has no file at all
                existing = set(kb["companies"]) | {r["Employer"] for r in kb["persons"].values()}
                newc = rng.choice(sorted("%s %s" % (pf, sf) for pf in COMPANY_PREFIX for sf in COMPANY_SUFFIX
                                         if "%s %s" % (pf, sf) not in existing))
                kb["persons"][holder]["Employer"] = newc
            question = SEARCH_Q[fam].format(P=pname)
        elif inf_kind == "attr_not_in_kb":
            question = rng.choice(ATTR_NOT_IN_KB_Q).format(P=pname)
            fam = "attr"
        elif inf_kind == "attr_line_missing":
            fam = rng.choice(["born", "hq", "founded", "mentor_employer"])
            rec = kb["persons"][pname]
            if fam == "born":
                del rec["Born"]
            elif fam == "mentor_employer":
                del rec["Mentor"]
            else:
                del kb["companies"][rec["Employer"]]["Headquarters" if fam == "hq" else "Founded"]
            question = SEARCH_Q[fam].format(P=pname)
        elif inf_kind == "mentor_absent":
            fam = rng.choice(["mentor_employer", "mentor_born", "mentor_hq"])
            kb["persons"][pname]["Mentor"] = absent_name(rng, kb)
            question = SEARCH_Q[fam].format(P=pname)
        else:
            raise TemplateError(inf_kind)
    files = render_kb(kb)
    return {
        "kind": kind, "feasible": feasible, "check": "exact_str", "expected": expected,
        "instruction": "%s %s %s" % (SEARCH_HEAD, question, SEARCH_TAIL), "key": question,
        "files": files,
        "meta": {"kb_files": len(files), "hops": SEARCH_HOPS.get(kind, 0)},
    }


_SEARCH_VERIFY = [
    (r"In which city is the headquarters of the employer of (.+)'s mentor\?", ["Mentor", "Employer", "Headquarters"]),
    (r"In which city is the headquarters of (.+)'s employer\?", ["Employer", "Headquarters"]),
    (r"In which year was (.+)'s employer founded\?", ["Employer", "Founded"]),
    (r"In which year was (.+)'s mentor born\?", ["Mentor", "Born"]),
    (r"Which company does (.+)'s mentor work for\?", ["Mentor", "Employer"]),
    (r"Which company does (.+) work for\?", ["Employer"]),
    (r"In which year was (.+) born\?", ["Born"]),
]


def verify_search(instruction, kb_dir):
    m = re.match(re.escape(SEARCH_HEAD) + r" (.*?\?) Use the file tools", instruction)
    if not m:
        return None
    q = m.group(1)
    index = {}
    for fn in sorted(os.listdir(kb_dir)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(kb_dir, fn), encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines or not lines[0].startswith("# "):
            continue
        fields = {}
        for ln in lines[1:]:
            if ": " in ln:
                k, v = ln.split(": ", 1)
                fields[k.strip()] = v.strip()
        index[lines[0][2:].strip()] = fields
    for pat, chain in _SEARCH_VERIFY:
        mm = re.fullmatch(pat, q)
        if not mm:
            continue
        cur = mm.group(1)
        for field in chain:
            rec = index.get(cur)
            if rec is None or field not in rec:
                return None
            cur = rec[field]
        return cur
    return None


# =============================================================================================
# CODING
# =============================================================================================

WORDS = ["apple", "river", "stone", "cloud", "tiger", "lemon", "piano", "candle", "forest", "marble", "pepper",
         "rocket", "silver", "window", "garden", "honey", "island", "jacket", "kettle", "ladder", "mango",
         "needle", "orange", "pencil", "rabbit", "saddle", "tomato", "violin", "wagon", "yellow", "zebra",
         "anchor", "bottle", "castle", "dragon", "engine", "falcon", "guitar", "sun", "map", "ox", "bee"]
LETTERS = "abcdefghij"
PALINDROMES = ["level", "noon", "racecar", "civic", "radar", "refer", "kayak", "madam", "stats", "rotor", "abba",
               "deed", "wow"]


def _ilist(r, lo_n, hi_n, lo=-9, hi=20):
    return [r.randint(lo, hi) for _ in range(r.randint(lo_n, hi_n))]


def _words(r, lo, hi):
    if r.random() < 0.4:
        return " ".join(r.sample(list(LETTERS), r.randint(lo, hi)))
    return " ".join(r.sample(WORDS, r.randint(lo, hi)))


def _gen_avg(r):
    if r.random() < 0.5:
        return ([r.randint(0, 20) for _ in range(r.choice([2, 4]))],)
    n = r.randint(3, 5)
    xs = [r.randint(0, 20) for _ in range(n - 1)]
    last = r.randint(0, 20)
    while (sum(xs) + last) % n:
        last += 1
    return (xs + [last],)


def _gen_pal(r):
    c = r.random()
    if c < 0.35:
        return (r.choice(PALINDROMES),)
    if c < 0.7:
        a = r.choice("abcdef")
        mid = "".join(r.choice("abcdef") for _ in range(r.randint(1, 4)))
        return (a + mid + a,)
    return (r.choice(WORDS),)


def _gen_median(r):
    return ([r.randint(-5, 30) for _ in range(r.randint(1, 7))],)


def _median_ref(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _longest(s):
    best = ""
    for w in s.split():
        if len(w) > len(best):
            best = w
    return best


def _gen_longest(r):
    ws = r.sample(WORDS, r.randint(2, 5))
    if r.random() < 0.5:
        L = max(len(w) for w in ws)
        same = [w for w in WORDS if len(w) == L and w not in ws]
        if same:
            ws.insert(r.randint(0, len(ws)), r.choice(same))
    return (" ".join(ws),)


def _is_prime(n):
    if n < 2:
        return False
    return all(n % d for d in range(2, int(n ** 0.5) + 1))


def _gen_prime(r):
    c = r.random()
    if c < 0.3:
        return (r.choice([1, 4, 9, 25, 49, 121, 169]),)
    if c < 0.6:
        return (r.choice([2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 97]),)
    return (r.randint(0, 120),)


def _char_freq(s):
    d = {}
    for c in s:
        d[c] = d.get(c, 0) + 1
    return d


def _index_of(xs, t):
    return xs.index(t) if t in xs else -1


def _gen_index(r):
    xs = [r.randint(0, 20) for _ in range(r.randint(2, 7))]
    t = r.choice(xs) if r.random() < 0.75 else r.randint(21, 40)
    return (xs, t)


def _gen_digits(r):
    n = r.randint(10, 99999)
    return (-n if r.random() < 0.3 else n,)


class Fn:
    def __init__(self, name, src, bugs, ref, gen, edges, canon):
        self.name, self.src, self.bugs, self.ref, self.gen = name, src, bugs, ref, gen
        self.edges, self.canon = edges, canon
        for kind, old, new in bugs:
            if src.count(old) != 1:
                raise TemplateError("bug pattern not unique in %s: %r" % (name, old))

    def buggy(self, bug):
        kind, old, new = bug
        return self.src.replace(old, new)


FNS = [
    Fn("reverse_words", "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n",
       [("sorted_instead_of_reversed", "reversed(s.split())", "sorted(s.split())"),
        ("missing_reverse", "' '.join(reversed(s.split()))", "' '.join(s.split())"),
        ("reverses_characters", "return ' '.join(reversed(s.split()))", "return s[::-1]")],
       lambda s: " ".join(s.split()[::-1]), lambda r: (_words(r, 2, 5),),
       [("hello",), ("one two",), ("a b c d e",)], ("c a b",)),
    Fn("total", "def total(xs):\n    s = 0\n    for x in xs:\n        s += x\n    return s\n",
       [("wrong_operator", "        s += x", "        s -= x"), ("wrong_init", "    s = 0", "    s = 1"),
        ("counts_instead_of_sums", "        s += x", "        s += 1")],
       lambda xs: sum(xs), lambda r: (_ilist(r, 2, 6, 0, 30),),
       [([],), ([7],), ([-2, 5, -3],), ([10, 20, 30, 40],)], ([1, 2, 3],)),
    Fn("maximum", "def maximum(xs):\n    best = xs[0]\n    for x in xs:\n        if x > best:\n            best = x\n    return best\n",
       [("wrong_comparison", "if x > best:", "if x < best:"), ("wrong_init", "    best = xs[0]", "    best = 0")],
       lambda xs: max(xs), lambda r: (_ilist(r, 2, 6, -40, -1) if r.random() < 0.3 else _ilist(r, 2, 6, -10, 40),),
       [([5],), ([-4, -1, -7],), ([2, 8, 8, 1],), ([10, 3],)], ([3, 9, 2],)),
    Fn("minimum", "def minimum(xs):\n    best = xs[0]\n    for x in xs:\n        if x < best:\n            best = x\n    return best\n",
       [("wrong_comparison", "if x < best:", "if x > best:"), ("wrong_init", "    best = xs[0]", "    best = 0"),
        ("off_by_one", "    for x in xs:", "    for x in xs[:-1]:")],
       lambda xs: min(xs), lambda r: (_ilist(r, 2, 6, 1, 40) if r.random() < 0.4 else _ilist(r, 2, 6, -10, 40),),
       [([5],), ([4, 1, 7],), ([9, 3, 3, 8],), ([6, 2],)], ([4, 7, 1],)),
    Fn("average", "def average(xs):\n    return sum(xs) / len(xs)\n",
       [("off_by_one", "sum(xs) / len(xs)", "sum(xs) / (len(xs) + 1)"),
        ("integer_division", "sum(xs) / len(xs)", "sum(xs) // len(xs)"),
        ("wrong_operator", "sum(xs) / len(xs)", "sum(xs) - len(xs)")],
       lambda xs: sum(xs) / len(xs), _gen_avg,
       [([5],), ([1, 2],), ([1, 2, 3, 4],), ([-3, 3],), ([10, 20, 30],)], ([2, 4, 6],)),
    Fn("first_n", "def first_n(xs, n):\n    return xs[:n]\n",
       [("off_by_one", "xs[:n]", "xs[:n - 1]"), ("off_by_one_plus", "xs[:n]", "xs[:n + 1]"),
        ("wrong_slice", "xs[:n]", "xs[n:]")],
       lambda xs, n: xs[:n], lambda r: (lambda xs: (xs, r.randint(1, len(xs) - 1)))(_ilist(r, 3, 7, 0, 30)),
       [([1, 2, 3], 0), ([1, 2], 5), ([9], 1), ([4, 5, 6, 7], 2)], ([5, 6, 7, 8], 3)),
    Fn("count_even", "def count_even(xs):\n    return sum(1 for x in xs if x % 2 == 0)\n",
       [("wrong_comparison", "x % 2 == 0", "x % 2 == 1"), ("wrong_modulo", "x % 2 == 0", "x % 3 == 0")],
       lambda xs: len([x for x in xs if x % 2 == 0]), lambda r: (_ilist(r, 3, 7, 0, 30),),
       [([],), ([1, 3, 5],), ([0, -2, 3],), ([2, 4, 6, 8],)], ([1, 2, 4, 6, 7],)),
    Fn("is_palindrome", "def is_palindrome(s):\n    return s == s[::-1]\n",
       [("negated", "s == s[::-1]", "s != s[::-1]"), ("checks_ends_only", "return s == s[::-1]", "return s[0] == s[-1]"),
        ("wrong_slice", "s == s[::-1]", "s == s[1::-1]")],
       lambda s: s == "".join(reversed(s)), _gen_pal,
       [("",), ("a",), ("ab",), ("abca",), ("noon",), ("racecar",)], ("level",)),
    Fn("clamp", "def clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    if x > hi:\n        return hi\n    return x\n",
       [("swapped_bound", "        return lo\n", "        return hi\n"),
        ("wrong_variable", "    if x > hi:", "    if x > lo:"),
        ("wrong_comparison", "    if x < lo:", "    if x > lo:")],
       lambda x, lo, hi: max(lo, min(x, hi)),
       lambda r: (lambda lo, w: (r.randint(lo - 10, lo + w + 10), lo, lo + w))(r.randint(-10, 10), r.randint(5, 20)),
       [(-5, 0, 10), (5, 0, 10), (0, 0, 10), (10, 0, 10), (99, 0, 10)], (15, 0, 10)),
    Fn("flatten", "def flatten(xss):\n    out = []\n    for xs in xss:\n        out.extend(xs)\n    return out\n",
       [("append_instead_of_extend", "out.extend(xs)", "out.append(xs)"),
        ("off_by_one", "    for xs in xss:", "    for xs in xss[1:]:"),
        ("reversed_parts", "out.extend(xs)", "out.extend(xs[::-1])")],
       lambda xss: [x for xs in xss for x in xs],
       lambda r: ([_ilist(r, 1, 3, 0, 9) for _ in range(r.randint(2, 4))],),
       [([],), ([[]],), ([[1], [2], [3]],), ([[7, 8, 9]],)], ([[1, 2], [3], [4, 5]],)),
    Fn("unique_sorted", "def unique_sorted(xs):\n    return sorted(set(xs))\n",
       [("missing_dedupe", "sorted(set(xs))", "sorted(xs)"),
        ("wrong_order", "sorted(set(xs))", "sorted(set(xs), reverse=True)")],
       lambda xs: sorted(dict.fromkeys(xs)), lambda r: (_ilist(r, 3, 8, 0, 6),),
       [([],), ([5, 5, 5],), ([-1, 4, -1, 0],), ([2, 1],)], ([3, 1, 3, 2, 1],)),
    Fn("word_count", "def word_count(s):\n    return len(s.split())\n",
       [("counts_spaces", "len(s.split())", "s.count(' ')"), ("counts_characters", "len(s.split())", "len(s)"),
        ("off_by_one", "len(s.split())", "len(s.split()) - 1")],
       lambda s: len(s.split()), lambda r: (_words(r, 1, 6),),
       [("",), ("hello",), ("a b c d e f",)], ("the quick brown fox",)),
    Fn("factorial", "def factorial(n):\n    r = 1\n    for i in range(2, n + 1):\n        r *= i\n    return r\n",
       [("off_by_one", "range(2, n + 1)", "range(2, n)"), ("wrong_init", "    r = 1", "    r = 0"),
        ("wrong_operator", "        r *= i", "        r += i")],
       lambda n: 1 if n < 2 else n * FNS_REF["factorial"](n - 1), lambda r: (r.randint(3, 9),),
       [(0,), (1,), (3,), (7,)], (5,)),
    Fn("fib", "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
       [("off_by_one", "range(n)", "range(n - 1)"), ("wrong_init", "a, b = 0, 1", "a, b = 1, 1"),
        ("wrong_return", "    return a", "    return b")],
       lambda n: n if n < 2 else FNS_REF["fib"](n - 1) + FNS_REF["fib"](n - 2), lambda r: (r.randint(4, 15),),
       [(0,), (1,), (2,), (7,), (15,)], (10,)),
    Fn("gcd", "def gcd(a, b):\n    while b:\n        a, b = b, a % b\n    return a\n",
       [("wrong_operator", "a % b", "a // b"), ("wrong_return", "    return a", "    return b")],
       lambda a, b: __import__("math").gcd(a, b),
       lambda r: (lambda g: (g * r.randint(1, 12), g * r.randint(1, 12)))(r.randint(2, 9)),
       [(7, 5), (0, 9), (10, 0), (21, 14), (100, 75)], (12, 18)),
    Fn("median", "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n    mid = n // 2\n    if n % 2 == 1:\n"
                 "        return s[mid]\n    return (s[mid - 1] + s[mid]) / 2\n",
       [("missing_sort", "s = sorted(xs)", "s = list(xs)"), ("off_by_one", "        return s[mid]\n", "        return s[mid - 1]\n"),
        ("integer_division", "(s[mid - 1] + s[mid]) / 2", "(s[mid - 1] + s[mid]) // 2")],
       _median_ref, _gen_median, [([5],), ([1, 2],), ([4, 1, 3, 2],), ([7, 7, 1],)], ([3, 1, 2],)),
    Fn("capitalize_words", "def capitalize_words(s):\n    return ' '.join(w.capitalize() for w in s.split())\n",
       [("upper_instead_of_capitalize", "w.capitalize()", "w.upper()"),
        ("first_word_only", "return ' '.join(w.capitalize() for w in s.split())", "return s.capitalize()")],
       lambda s: " ".join(w[:1].upper() + w[1:] for w in s.split()), lambda r: (" ".join(r.sample(WORDS, r.randint(2, 4))),),
       [("a",), ("one two three",), ("",)], ("hello world",)),
    Fn("count_vowels", "def count_vowels(s):\n    return sum(1 for c in s.lower() if c in 'aeiou')\n",
       [("missing_letter", "'aeiou'", "'aeio'"), ("negated", "if c in 'aeiou'", "if c not in 'aeiou'")],
       lambda s: len([c for c in s if c in "aeiou"]), lambda r: (" ".join(r.sample(WORDS, r.randint(1, 3))),),
       [("",), ("rhythm",), ("queue",), ("banana split",)], ("education",)),
    Fn("sum_of_squares", "def sum_of_squares(xs):\n    return sum(x * x for x in xs)\n",
       [("wrong_operator", "x * x", "x * 2"), ("squares_the_sum", "sum(x * x for x in xs)", "sum(xs) ** 2")],
       lambda xs: sum(x ** 2 for x in xs), lambda r: (_ilist(r, 2, 5, -6, 12),),
       [([],), ([4],), ([-2, 2],), ([0, 5, 1],)], ([1, 2, 3],)),
    Fn("product", "def product(xs):\n    p = 1\n    for x in xs:\n        p *= x\n    return p\n",
       [("wrong_init", "    p = 1", "    p = 0"), ("wrong_operator", "        p *= x", "        p += x")],
       lambda xs: __import__("math").prod(xs), lambda r: (_ilist(r, 2, 5, -4, 9),),
       [([],), ([7],), ([-1, 5, 2],), ([3, 0, 9],)], ([2, 3, 4],)),
    Fn("is_prime", "def is_prime(n):\n    if n < 2:\n        return False\n    for d in range(2, int(n ** 0.5) + 1):\n"
                   "        if n % d == 0:\n            return False\n    return True\n",
       [("off_by_one", "range(2, int(n ** 0.5) + 1)", "range(2, int(n ** 0.5))"),
        ("wrong_bound", "    if n < 2:", "    if n < 1:"), ("wrong_comparison", "n % d == 0", "n % d == 1")],
       _is_prime, _gen_prime, [(0,), (1,), (2,), (3,), (4,), (17,), (25,), (97,), (100,)], (49,)),
    Fn("reverse_list", "def reverse_list(xs):\n    return xs[::-1]\n",
       [("sorted_instead_of_reversed", "xs[::-1]", "sorted(xs)"),
        ("sorted_descending", "xs[::-1]", "sorted(xs, reverse=True)"), ("off_by_one", "xs[::-1]", "xs[1:][::-1]")],
       lambda xs: list(reversed(xs)), lambda r: (_ilist(r, 3, 6, 0, 20),),
       [([],), ([5],), ([4, 4, 1],), ([1, 2, 3, 4],)], ([1, 3, 2],)),
    Fn("count_occurrences", "def count_occurrences(xs, target):\n    return sum(1 for x in xs if x == target)\n",
       [("negated", "x == target", "x != target"), ("wrong_comparison", "x == target", "x >= target")],
       lambda xs, t: xs.count(t), lambda r: (lambda xs: (xs, r.choice(xs)))(_ilist(r, 3, 8, 0, 5)),
       [([], 5), ([2, 2], 3), ([7, 7, 7], 7), ([1, 9, 1], 9)], ([1, 2, 1, 3, 1], 1)),
    Fn("digit_sum", "def digit_sum(n):\n    n = abs(n)\n    total = 0\n    while n > 0:\n        total += n % 10\n"
                    "        n //= 10\n    return total\n",
       [("missing_abs", "    n = abs(n)\n", ""), ("wrong_modulo", "n % 10", "n % 100"),
        ("wrong_operator", "        total += n % 10", "        total += n // 10")],
       lambda n: sum(int(c) for c in str(abs(n))), _gen_digits,
       [(0,), (7,), (-56,), (1000,), (99,)], (1234,)),
    Fn("distance", "def distance(a, b):\n    return abs(a - b)\n",
       [("missing_abs", "abs(a - b)", "a - b"), ("wrong_operator", "abs(a - b)", "abs(a + b)")],
       lambda a, b: a - b if a > b else b - a, lambda r: (r.randint(-10, 30), r.randint(-10, 30)),
       [(8, 3), (0, 0), (-2, 4), (5, 5)], (3, 8)),
    Fn("sign", "def sign(x):\n    if x > 0:\n        return 1\n    if x < 0:\n        return -1\n    return 0\n",
       [("wrong_comparison", "    if x > 0:", "    if x >= 0:"), ("wrong_return", "        return -1", "        return 1")],
       lambda x: (x > 0) - (x < 0), lambda r: (r.choice([0, r.randint(-50, -1), r.randint(1, 50)]),),
       [(5,), (0,), (-1,), (100,)], (-7,)),
    Fn("running_sum", "def running_sum(xs):\n    out = []\n    s = 0\n    for x in xs:\n        s += x\n        out.append(s)\n"
                      "    return out\n",
       [("no_accumulation", "        s += x", "        s = x"), ("wrong_init", "    s = 0", "    s = 1"),
        ("wrong_order", "        s += x\n        out.append(s)", "        out.append(s)\n        s += x")],
       lambda xs: [sum(xs[:i + 1]) for i in range(len(xs))], lambda r: (_ilist(r, 2, 6, -5, 15),),
       [([],), ([5],), ([2, -2, 2],), ([1, 1, 1, 1],)], ([1, 2, 3, 4],)),
    Fn("last_n", "def last_n(xs, n):\n    return xs[len(xs) - n:]\n",
       [("off_by_one", "xs[len(xs) - n:]", "xs[len(xs) - n + 1:]"), ("wrong_slice", "xs[len(xs) - n:]", "xs[:n]")],
       lambda xs, n: xs[len(xs) - n:] if n else [], lambda r: (lambda xs: (xs, r.randint(1, len(xs) - 1)))(_ilist(r, 3, 7, 0, 30)),
       [([1, 2, 3], 0), ([1, 2, 3], 3), ([9], 1), ([4, 5, 6, 7], 1)], ([5, 6, 7, 8], 2)),
    Fn("longest_word", "def longest_word(s):\n    best = ''\n    for w in s.split():\n        if len(w) > len(best):\n"
                       "            best = w\n    return best\n",
       [("wrong_comparison", "len(w) > len(best)", "len(w) < len(best)"),
        ("ties_pick_last", "len(w) > len(best)", "len(w) >= len(best)")],
       _longest, _gen_longest, [("",), ("one",), ("ab cd ef",), ("hi there friend",)], ("a bb ccc dd",)),
    Fn("char_frequency", "def char_frequency(s):\n    d = {}\n    for c in s:\n        d[c] = d.get(c, 0) + 1\n    return d\n",
       [("wrong_default", "d.get(c, 0) + 1", "d.get(c, 1) + 1"), ("no_counting", "d[c] = d.get(c, 0) + 1", "d[c] = 1")],
       _char_freq, lambda r: ("".join(r.choice("abcde") for _ in range(r.randint(3, 8))),),
       [("",), ("aaa",), ("abc",), ("abab",)], ("banana",)),
    Fn("filter_positive", "def filter_positive(xs):\n    return [x for x in xs if x > 0]\n",
       [("wrong_comparison", "x > 0", "x >= 0"), ("negated", "x > 0", "x < 0")],
       lambda xs: list(filter(lambda v: v > 0, xs)), lambda r: (_ilist(r, 3, 7, -5, 9),),
       [([],), ([-1, -2],), ([0],), ([4, 1],)], ([-2, 0, 3, -1, 5],)),
    Fn("celsius_to_fahrenheit", "def celsius_to_fahrenheit(c):\n    return c * 9 / 5 + 32\n",
       [("inverted_ratio", "c * 9 / 5 + 32", "c * 5 / 9 + 32"), ("wrong_order", "c * 9 / 5 + 32", "(c + 32) * 9 / 5")],
       lambda c: (c * 9) // 5 + 32 if c % 5 == 0 else c * 1.8 + 32, lambda r: (5 * r.randint(-10, 30),),
       [(0,), (-40,), (25,), (-10,)], (100,)),
    Fn("power", "def power(base, exp):\n    r = 1\n    for _ in range(exp):\n        r *= base\n    return r\n",
       [("off_by_one", "range(exp)", "range(exp - 1)"), ("wrong_variable", "        r *= base", "        r *= exp"),
        ("wrong_init", "    r = 1", "    r = base")],
       lambda b, e: b ** e, lambda r: (r.randint(2, 6), r.randint(2, 5)),
       [(5, 0), (7, 1), (3, 4), (-2, 3), (10, 2)], (2, 5)),
    Fn("index_of", "def index_of(xs, target):\n    for i, x in enumerate(xs):\n        if x == target:\n            return i\n"
                   "    return -1\n",
       [("off_by_one", "            return i\n", "            return i + 1\n"), ("wrong_default", "    return -1", "    return 0"),
        ("negated", "if x == target", "if x != target")],
       _index_of, _gen_index, [([], 3), ([1, 2, 3], 9), ([5, 5, 5], 5), ([3, 1, 2], 2)], ([4, 8, 15, 16], 15)),
]
FNS_BY_NAME = {f.name: f for f in FNS}
FNS_REF = {f.name: f.ref for f in FNS}


def compile_fn(src, name):
    ns = {}
    # the generator's own reference source (written in this file), never task or model text
    exec(src, ns)
    return ns[name]


def call_safe(fn, args):
    try:
        return ("ok", fn(*copy.deepcopy(args)))
    except Exception as e:  # noqa: BLE001
        return ("err", type(e).__name__)


def fmt_val(v):
    if isinstance(v, float) and v == int(v):
        return repr(int(v))
    return repr(v)


def call_expr(name, args):
    return "m.%s(%s)" % (name, ", ".join(repr(a) for a in args))


def eq_line(name, args, exp):
    return "self.assertEqual(%s, %s)" % (call_expr(name, args), fmt_val(exp))


def render_test(methods):
    out = ["import unittest", "", "import mylib as m", "", "", "class T(unittest.TestCase):"]
    for i, (mname, lines) in enumerate(methods):
        if i:
            out.append("")
        out.append("    def %s(self):" % mname)
        out += ["        " + ln for ln in lines]
    out += ["", "", "if __name__ == '__main__':", "    unittest.main()"]
    return "\n".join(out) + "\n"


def alt_value(v, rng):
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + rng.choice([1, 2, -1, 3, 10])
    if isinstance(v, float):
        return v + rng.choice([1, 2, 0.5])
    if isinstance(v, str):
        for c in ([v[::-1], v.upper(), v + "s"] if rng.random() < 0.5 else [v + "s", v[::-1], v.upper()]):
            if c != v:
                return c
        return v + "x"
    if isinstance(v, list):
        for c in [v[::-1], v[:-1], v + v[-1:]]:
            if c != v:
                return c
        return [0]
    if isinstance(v, dict):
        d = dict(v)
        if d:
            k = next(iter(d))
            d[k] += 1
        else:
            d = {"a": 1}
        return d
    raise TemplateError("alt_value %r" % (v,))


_CORRECT_CACHE = {}


def ref_value(f, args):
    """Expected value from the independent reference, cross-checked with the canonical source."""
    v = f.ref(*copy.deepcopy(args))
    if f.name not in _CORRECT_CACHE:
        _CORRECT_CACHE[f.name] = compile_fn(f.src, f.name)
    got = call_safe(_CORRECT_CACHE[f.name], args)
    if got[0] != "ok" or not (got[1] == v) or type(got[1]) is bool and type(v) is not bool:
        raise TemplateError("reference mismatch for %s%r: %r vs %r" % (f.name, args, got, v))
    return v


def gen_coding(rng, opts, inf_kind):
    size, easy = opts["size"], opts["easy"]
    if easy:
        lo, hi = 3, 4
    elif size == "small":
        lo, hi = 3, 6
    elif size == "large":
        lo, hi = 6, 8
    else:
        lo, hi = 3, 8
    nfun = rng.randint(lo, hi)
    fns = rng.sample(FNS, nfun)
    nbug = 1 if easy else min(nfun, weighted(rng, [(1, 25), (2, 45), (3, 20), (4, 10)]))
    bug_idx = sorted(rng.sample(range(nfun), nbug))
    bugs = {i: rng.choice(fns[i].bugs) for i in bug_idx}

    srcs, fixed_srcs, methods, hidden_methods, bug_meta, bug_lines, overfit_cases = [], [], [], [], [], [], []
    vis_all = []
    for i, f in enumerate(fns):
        fixed_srcs.append(f.src)
        bsrc = f.buggy(bugs[i]) if i in bugs else f.src
        srcs.append(bsrc)
        bfn = compile_fn(bsrc, f.name)

        def differs(args, _b=bfn, _f=f):
            exp = ref_value(_f, args)
            got = call_safe(_b, args)
            return not (got[0] == "ok" and got[1] == exp)
        # visible case(s)
        vis = []
        cands = ([f.canon] if rng.random() < 0.5 else []) + [f.gen(rng) for _ in range(300)]
        for args in cands:
            if i in bugs and not differs(args):
                continue
            vis.append(args)
            break
        if not vis:
            raise GenError("no failing visible case for %s %s" % (f.name, bugs.get(i)))
        if rng.random() < 0.12:
            extra = f.gen(rng)
            if repr(extra) != repr(vis[0]):
                vis.append(extra)
        vis_all.append(vis)
        methods.append(("test_" + f.name, [eq_line(f.name, a, ref_value(f, a)) for a in vis]))
        if i in bugs:
            bug_meta.append([f.name, bugs[i][0]])
            new = bugs[i][2]
            bug_lines += [ln.strip() for ln in new.splitlines() if ln.strip()]
            overfit_cases.append((f.name, vis))
        # hidden cases: visible + edges + random, and for buggy functions at least one extra failing case
        hid, seen = [], set()
        for args in vis + list(f.edges) + [f.gen(rng) for _ in range(4)]:
            if repr(args) not in seen:
                seen.add(repr(args))
                hid.append(args)
        if i in bugs:
            vis_r = {repr(a) for a in vis}
            if not any(differs(a) for a in hid if repr(a) not in vis_r):
                for _ in range(300):
                    a = f.gen(rng)
                    if repr(a) not in seen and differs(a):
                        hid.append(a)
                        seen.add(repr(a))
                        break
                else:
                    raise GenError("no hidden failing case for %s" % f.name)
        hidden_methods.append(("test_" + f.name, [eq_line(f.name, a, ref_value(f, a)) for a in hid]))

    mylib = "\n\n".join(srcs)
    fixed = "\n\n".join(fixed_srcs)
    feasible, kind, expected = True, "fix_bugs", "tests_pass"
    contradiction = None
    clean_methods = [(n, list(ls)) for n, ls in methods]
    extra_fixed = ""
    if inf_kind:
        feasible, kind, expected = False, inf_kind, None
        j = rng.randrange(nfun)
        f = fns[j]
        vis_args = vis_all[j][0]
        exp = ref_value(f, vis_args)
        if inf_kind == "contradict_same_method":
            alt = alt_value(exp, rng)
            assert alt != exp
            methods[j][1].append(eq_line(f.name, vis_args, alt))
            contradiction = [call_expr(f.name, vis_args), fmt_val(exp), fmt_val(alt)]
        elif inf_kind == "contradict_two_methods":
            alt = alt_value(exp, rng)
            assert alt != exp
            methods.insert(j + 1, ("test_%s_edge" % f.name, [eq_line(f.name, vis_args, alt)]))
            contradiction = [call_expr(f.name, vis_args), fmt_val(exp), fmt_val(alt)]
        elif inf_kind == "impossible_property":
            ce = call_expr(f.name, vis_args)
            if isinstance(exp, bool):
                line = "self.assert%s(%s)" % ("False" if exp else "True", ce)
            elif isinstance(exp, (int, float)):
                line = "self.assertGreater(%s, %s)" % (ce, fmt_val(exp))
            else:
                line = "self.assertEqual(len(%s), %d)" % (ce, len(exp) + 1)
            methods[j][1].append(line)
            contradiction = [ce, fmt_val(exp), line]
        elif inf_kind == "missing_function":
            names = {x.name for x in fns}
            g = rng.choice([x for x in FNS if x.name not in names])
            ga = g.gen(rng)
            gv = ref_value(g, ga)
            alt = alt_value(gv, rng)
            assert alt != gv
            new_method = ("test_" + g.name, [eq_line(g.name, ga, gv), eq_line(g.name, ga, alt)])
            methods.insert(rng.randint(0, len(methods)), new_method)
            clean_methods.insert(methods.index(new_method), ("test_" + g.name, [eq_line(g.name, ga, gv)]))
            contradiction = [call_expr(g.name, ga), fmt_val(gv), fmt_val(alt)]
            extra_fixed = "\n\n" + g.src
        else:
            raise TemplateError(inf_kind)

    test_txt = render_test(methods)
    files = {"workspace/mylib.py": mylib, "workspace/test_mylib.py": test_txt}
    hidden = {"mylib_ref.py": fixed}
    if feasible:
        hidden["test_hidden.py"] = render_test(hidden_methods)
    return {
        "kind": kind, "feasible": feasible, "check": "unittest", "expected": expected,
        "instruction": CODING_INSTRUCTION, "key": None, "files": files, "hidden": hidden,
        "meta": {"functions": [f.name for f in fns], "bugs": bug_meta, "bug_lines": bug_lines,
                 "contradiction": contradiction},
        "_verify": {"overfit_cases": overfit_cases, "clean_test": render_test(clean_methods),
                    "fixed_plus": fixed + extra_fixed},
    }


def overfit_source(buggy, overfit_cases):
    """A test-overfitted 'fix': special-case the visible inputs, keep the buggy code otherwise."""
    lines = [buggy.rstrip("\n"), "", ""]
    for name, cases in overfit_cases:
        pairs = ", ".join("(%r, %r)" % (list(a), FNS_BY_NAME[name].ref(*copy.deepcopy(a))) for a in cases)
        lines += ["_orig_%s = %s" % (name, name), "",
                  "def %s(*args):" % name,
                  "    for a, r in [%s]:" % pairs,
                  "        if list(args) == a:",
                  "            return r",
                  "    return _orig_%s(*args)" % name, "", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def verify_coding(task, workroot):
    v = task["_verify"]
    files = task["files"]

    def run(mylib, test_txt, test_name="test_mylib.py"):
        d = tempfile.mkdtemp(dir=workroot)
        with open(os.path.join(d, "mylib.py"), "w") as f:
            f.write(mylib)
        with open(os.path.join(d, test_name), "w") as f:
            f.write(test_txt)
        ok, out = run_test_file(d, test_name, timeout=30)
        shutil.rmtree(d, ignore_errors=True)
        return ok, out
    vis = files["workspace/test_mylib.py"]
    fixed = task["hidden"]["mylib_ref.py"]
    ok, out = run(files["workspace/mylib.py"], vis)
    if ok:
        raise TemplateError("buggy mylib passes visible tests")
    if task["feasible"]:
        hid = task["hidden"]["test_hidden.py"]
        for name, src, test in [("fixed/visible", fixed, vis), ("fixed/hidden", fixed, hid)]:
            ok, out = run(src, test)
            if not ok:
                raise TemplateError("%s fails:\n%s" % (name, out))
        overfit = overfit_source(files["workspace/mylib.py"], v["overfit_cases"])
        ok, out = run(overfit, vis)
        if not ok:
            raise TemplateError("overfit fix should pass visible:\n%s" % out)
        ok, out = run(overfit, hid)
        if ok:
            raise TemplateError("overfit fix passes hidden tests")
    else:
        ok, out = run(v["fixed_plus"], vis)
        if ok:
            raise TemplateError("infeasible coding task is solvable by the reference fix")
        ok, out = run(v["fixed_plus"], v["clean_test"])
        if not ok:
            raise TemplateError("infeasible coding: test without contradiction still fails:\n%s" % out)


# =============================================================================================
# set assembly
# =============================================================================================

GENERATORS = {"math": gen_math, "data_analysis": gen_data, "search": gen_search, "coding": gen_coding}
FAMILY_WEIGHTS = {"math": MATH_PATTERN_WEIGHTS, "data_analysis": DATA_FAMILY_WEIGHTS, "search": SEARCH_WEIGHTS}


def classify(task_type, instruction):
    """Family name for an instruction (used by import_examples.py)."""
    if task_type == "math":
        return math_kind(math_ops_from_text(instruction)) if verify_math(instruction) is not None else "math_other"
    if task_type == "coding":
        return "fix_bugs"
    if task_type == "search":
        m = re.match(re.escape(SEARCH_HEAD) + r" (.*?\?) Use the file tools", instruction)
        q = m.group(1) if m else ""
        for fam, tmpl in SEARCH_Q.items():
            pat = re.escape(tmpl).replace(re.escape("{P}"), "(.+)")
            if re.fullmatch(pat, q) and not (fam in ("employer", "born") and "'s mentor" in q):
                return fam
        return "search_other"
    if task_type == "data_analysis":
        m = re.match(r"The file sales\.csv has columns \S+ \(\d+ rows\)\. (.*?\?) Use bash", instruction)
        q = m.group(1) if m else ""
        for fam, tmpls in DATA_Q.items():
            for tmpl in tmpls:
                pat = re.escape(tmpl)
                for ph in ("{p}", "{r}"):
                    pat = pat.replace(re.escape(ph), r"(\w+)")
                for ph in ("{a}", "{b}"):
                    pat = pat.replace(re.escape(ph), r"(\d+)")
                if re.fullmatch(pat, q):
                    return fam
        return "data_other"
    return "other"


def task_context(instruction, files):
    parts = [instruction]
    for rel in sorted(files):
        parts.append(files[rel])
    return "\n".join(parts)


def coding_mock_key(tid, files, meta, contexts, coding_ids):
    """Shortest distinctive fragment of this task's buggy mylib.py (fallback: test file)."""
    mylib = files["workspace/mylib.py"]
    test = files["workspace/test_mylib.py"]
    others_code = [contexts[t] for t in coding_ids if t != tid]
    others_all = [c for t, c in contexts.items() if t != tid and t not in coding_ids]

    def unique(c):
        return (len(c) >= 6 and not any(c in o for o in others_code) and not any(c in o for o in others_all))
    seen = []
    for c in list(meta.get("bug_lines", [])) + sorted({ln.strip() for ln in mylib.splitlines() if ln.strip()},
                                                       key=lambda s: (-len(s), s)):
        if c not in seen:
            seen.append(c)
    for c in seen:
        if unique(c):
            return c
    for ln in test.splitlines():
        c = ln.strip()
        if c.startswith("self.assert") and unique(c):
            return c
    lines = mylib.splitlines()
    for w in range(2, len(lines) + 1):
        for i in range(len(lines) - w + 1):
            c = "\n".join(lines[i:i + w])
            if c.strip() and unique(c):
                return c
    return mylib


def mock_reply(task_type, feasible, expected, files_hidden):
    if not feasible:
        return "IMPOSSIBLE"
    if task_type == "coding":
        return "```python\n" + files_hidden["mylib_ref.py"] + "```"
    return "ANSWER: %s" % expected


def build_oracle_mock(entries, contexts, coding_ids):
    """entries: list of dicts with tid, type, feasible, expected, key, files, meta, hidden."""
    out = []
    for e in entries:
        key = e["key"]
        if e["type"] == "coding":
            key = coding_mock_key(e["tid"], e["files"], e["meta"], contexts, coding_ids)
        clashes = [t for t, c in contexts.items() if t != e["tid"] and key in c]
        if clashes:
            print("WARNING: mock key of %s also matches %s" % (e["tid"], clashes[:3]), file=sys.stderr)
        if key not in contexts[e["tid"]]:
            raise TemplateError("mock key not in own context: " + e["tid"])
        out.append([key, mock_reply(e["type"], e["feasible"], e["expected"], e.get("hidden") or {})])
    return out


def parse_mix(s):
    alias = {"data": "data_analysis", "da": "data_analysis"}
    mix = {}
    for part in s.split(","):
        if not part.strip():
            continue
        k, v = part.split("=")
        k = alias.get(k.strip(), k.strip())
        if k not in TYPES:
            raise SystemExit("unknown type in --mix: " + k)
        mix[k] = float(v)
    return mix


def largest_remainder(n, weights):
    tot = sum(weights.values())
    if n == 0 or tot <= 0:
        return {k: 0 for k in weights}
    raw = {k: n * w / tot for k, w in weights.items()}
    counts = {k: int(v) for k, v in raw.items()}
    rest = n - sum(counts.values())
    order = sorted(weights, key=lambda k: (-(raw[k] - counts[k]), list(weights).index(k)))
    for k in order[:rest]:
        counts[k] += 1
    return counts


def safe_reset_dir(out):
    if os.path.exists(out):
        names = os.listdir(out)
        if names and not ({"keys.json", "manifest.json", ".bench_set"} & set(names)):
            raise SystemExit("refusing to overwrite non-set directory: " + out)
        shutil.rmtree(out)
    os.makedirs(out)
    with open(os.path.join(out, ".bench_set"), "w") as f:
        f.write("generated task set; safe to delete and regenerate\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_task_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def generate(out, n, seed, infeasible, mix, size, easy, verbose=True):
    master = make_rng("set:%d" % seed)
    counts = largest_remainder(n, mix)
    inf_total = int(round(n * infeasible))
    inf_counts = largest_remainder(inf_total, {t: counts[t] for t in counts if counts[t] > 0})
    for t in inf_counts:
        inf_counts[t] = min(inf_counts[t], counts[t])
    types = [t for t in TYPES if t in counts for _ in range(counts[t])]
    master.shuffle(types)
    inf_flags = [False] * n
    for t in TYPES:
        idx = [i for i, x in enumerate(types) if x == t]
        for i in master.sample(idx, inf_counts.get(t, 0)) if idx else []:
            inf_flags[i] = True
    rewards = [master.randint(1, 5) for _ in range(n)]
    # stratified plan: question families and infeasible kinds in proportion to their weights
    family = [None] * n
    inf_plan = [None] * n
    for t in TYPES:
        feas_idx = [i for i in range(n) if types[i] == t and not inf_flags[i]]
        if t in FAMILY_WEIGHTS and not easy and feas_idx:
            cnt = largest_remainder(len(feas_idx), dict(FAMILY_WEIGHTS[t]))
            fams = [f for f, _ in FAMILY_WEIGHTS[t] for _ in range(cnt[f])]
            master.shuffle(fams)
            for i, f in zip(feas_idx, fams):
                family[i] = f
        inf_idx = [i for i in range(n) if types[i] == t and inf_flags[i]]
        if inf_idx:
            cnt = largest_remainder(len(inf_idx), dict(INFEASIBLE_KINDS[t]))
            kinds = [k for k, _ in INFEASIBLE_KINDS[t] for _ in range(cnt[k])]
            master.shuffle(kinds)
            for i, k in zip(inf_idx, kinds):
                inf_plan[i] = k

    safe_reset_dir(out)
    opts = {"size": size, "easy": easy}
    keys, entries, contexts, used_keys = {}, [], {}, set()
    coding_ids = set()
    workroot = tempfile.mkdtemp(prefix="gen_verify_")
    try:
        for i, t in enumerate(types):
            tid = "%s_%04d" % (t, i)
            inf_kind = inf_plan[i]
            topts = dict(opts, family=family[i])
            task = None
            for attempt in range(60):
                rng = make_rng("task:%d:%d:%d" % (seed, i, attempt))
                try:
                    cand = GENERATORS[t](rng, topts, inf_kind)
                except GenError:
                    continue
                if cand["key"] is not None and cand["key"] in used_keys:
                    continue
                task = cand
                break
            if task is None:
                raise TemplateError("could not generate " + tid)
            if task["key"] is not None:
                used_keys.add(task["key"])
            tdir = os.path.join(out, "tasks", tid)
            write_task_json(os.path.join(tdir, "task.json"),
                            {"task_id": tid, "task_type": t, "instruction": task["instruction"], "reward": rewards[i]})
            for rel, text in task["files"].items():
                write_text(os.path.join(tdir, rel), text)
            # independent verification against the written files
            if t == "math":
                got = verify_math(task["instruction"])
            elif t == "data_analysis":
                got = verify_data(task["instruction"], os.path.join(tdir, "workspace", "sales.csv"))
            elif t == "search":
                got = verify_search(task["instruction"], os.path.join(tdir, "workspace", "kb"))
            else:
                verify_coding(task, workroot)
                got = task["expected"]
            if got != task["expected"]:
                raise TemplateError("verification failed for %s (%s): expected %r, checker %r\n%s" % (
                    tid, task["kind"], task["expected"], got, task["instruction"]))
            if t == "coding":
                coding_ids.add(tid)
                for name, text in task["hidden"].items():
                    write_text(os.path.join(out, "hidden", tid, name), text)
            meta = {k: v for k, v in task["meta"].items() if k != "bug_lines"}
            keys[tid] = {"type": t, "reward": rewards[i], "feasible": task["feasible"], "kind": task["kind"],
                         "check": task["check"], "expected": task["expected"], "meta": meta}
            contexts[tid] = task_context(task["instruction"], task["files"])
            entries.append({"tid": tid, "type": t, "feasible": task["feasible"], "expected": task["expected"],
                            "key": task["key"], "files": task["files"], "meta": task["meta"],
                            "hidden": task.get("hidden")})
    finally:
        shutil.rmtree(workroot, ignore_errors=True)

    mock = build_oracle_mock(entries, contexts, coding_ids)
    with open(os.path.join(out, "keys.json"), "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "oracle_mock.json"), "w", encoding="utf-8") as f:
        json.dump(mock, f, ensure_ascii=False, indent=1)
    summary = {}
    for tid, k in keys.items():
        s = summary.setdefault(k["type"], {"tasks": 0, "feasible": 0, "reward_feasible": 0, "kinds": {}})
        s["tasks"] += 1
        s["feasible"] += int(k["feasible"])
        s["reward_feasible"] += k["reward"] if k["feasible"] else 0
        s["kinds"][k["kind"]] = s["kinds"].get(k["kind"], 0) + 1
    manifest = {"generator": "gen_tasks.py", "n": n, "seed": seed, "infeasible": infeasible, "mix": mix,
                "size": size, "easy": easy, "summary": summary}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    if verbose:
        print("wrote %d tasks to %s" % (n, out))
        for t in TYPES:
            if t in summary:
                s = summary[t]
                print("  %-14s %3d tasks, %3d feasible | %s" % (
                    t, s["tasks"], s["feasible"], ", ".join("%s=%d" % kv for kv in sorted(s["kinds"].items()))))
    return keys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--infeasible", type=float, default=0.2, help="fraction of infeasible tasks")
    ap.add_argument("--mix", default="math=0.25,data_analysis=0.25,search=0.25,coding=0.25")
    ap.add_argument("--size", choices=["small", "mixed", "large"], default="mixed")
    ap.add_argument("--easy", action="store_true", help="only the simplest families (qualification-like)")
    a = ap.parse_args(argv)
    generate(a.out, a.n, a.seed, a.infeasible, parse_mix(a.mix), a.size, a.easy)
    return 0


if __name__ == "__main__":
    sys.exit(main())

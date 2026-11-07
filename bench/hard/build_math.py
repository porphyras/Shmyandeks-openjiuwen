"""Math tasks from GSM-Hard (reasoning-machines/gsm-hard, gsmhardv2.jsonl) and GSM8K test split
(grade-school-math, test.jsonl).

Verification (no downloaded code is executed):
  * GSM8K: every calculator annotation <<expr=res>> is re-evaluated with an AST arithmetic
    evaluator and must match; the final '#### N' must be an integer produced by an annotation.
  * GSM-Hard: the item is aligned with the GSM8K item it was derived from; exactly one number in
    the question must differ. The GSM8K annotation chain is replayed with the old number replaced
    by the new one (propagating changed intermediate results). The replayed final answer must equal
    the GSM-Hard target, be a positive integer, and every intermediate result must stay
    non-negative (and integral wherever the original was integral). Two independent derivations
    agreeing is the acceptance criterion.
Infeasible variants are built from other GSM8K items by replacing a quantity the solution needs
with a vague word ("some"), or by deleting the only sentence that states it.
"""
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fractions import Fraction  # noqa: E402

from common import MATH_TAIL, rng_for, safe_arith  # noqa: E402

ANN = re.compile(r"<<([^=<>]+)=([^<>]+)>>")
NUM_RE = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
         "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90, "hundred": 100}
TOK = re.compile(NUM_RE + r"|[A-Za-z]+|[^\sA-Za-z\d]")


def close(a, b):
    return abs(a - b) <= 1e-9 * max(1, abs(a), abs(b))


def tok_value(t):
    if re.fullmatch(NUM_RE, t):
        return Fraction(t.replace(",", ""))
    v = WORDS.get(t.lower())
    return None if v is None else Fraction(v)


def parse_solution(ans):
    """Return (steps [(expr, res)], final) with exact Fractions, or None if anything is
    inconsistent. A shown result may be the expression value rounded to the decimals shown."""
    m = re.search(r"####\s*([-\d,\.]+)\s*$", ans)
    if not m:
        return None
    try:
        final = Fraction(m.group(1).replace(",", ""))
    except ValueError:
        return None
    steps = []
    for expr, res in ANN.findall(ans):
        expr = expr.replace(",", "").replace("x", "*").strip()
        res = res.replace(",", "").strip()
        try:
            v = safe_arith(expr, exact=True)
            r = Fraction(res)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            return None
        dec = len(res.split(".", 1)[1]) if "." in res else 0
        if v != r and round(v, dec) != r:
            return None
        steps.append((expr, r))
    if not steps or final.denominator != 1:
        return None
    if not any(r == final for _, r in steps):
        return None
    return steps, final


def fmt_num(v):
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else "%d/%d" % (v.numerator, v.denominator)


LIT = re.compile(r"(\d+\.?\d*|\.\d+)")


def replay(steps, old, new, final, qvals=(), plausible=True):
    """Replay the annotation chain with `old` replaced by `new` (exact arithmetic).
    Returns the new final answer or None when the replay is ambiguous or implausible:
    a changed value that collides with another quantity of the same value, a negative
    intermediate result, or an integral intermediate result that becomes fractional."""
    results = [r for _, r in steps]
    if old in results:
        return None
    changed = {old: Fraction(new)}
    used_old = False
    for expr, res in steps:
        out = []
        for t in LIT.split(expr):
            if t and LIT.fullmatch(t) and Fraction(t) in changed:
                used_old = True
                t = "(" + fmt_num(changed[Fraction(t)]) + ")"
            out.append(t)
        try:
            nv = safe_arith("".join(out), exact=True)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            return None
        if plausible and (nv < 0 or (res.denominator == 1 and nv.denominator != 1)):
            return None
        if nv != res:
            if res in changed or results.count(res) != 1 or res in qvals:
                return None
            changed[res] = nv
    if not used_old or final not in changed:
        return None
    return changed[final]


def diff_one_number(q_old, q_new):
    a, b = TOK.findall(q_old), TOK.findall(q_new)
    ops = [o for o in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1 or ops[0][0] != "replace":
        return None
    _, i1, i2, j1, j2 = ops[0]
    if i2 - i1 != 1 or j2 - j1 != 1:
        return None
    vo, vn = tok_value(a[i1]), tok_value(b[j1])
    if vo is None or vn is None:
        return None
    # the old number must occur only once in the original question
    if sum(1 for t in a if tok_value(t) == vo) != 1:
        return None
    return vo, vn


def question_values(q):
    return {tok_value(t) for t in TOK.findall(q) if tok_value(t) is not None}


def load(dl):
    gsm = [json.loads(l) for l in open(os.path.join(dl, "gsm8k", "test.jsonl"), encoding="utf-8")]
    hard = [json.loads(l) for l in open(os.path.join(dl, "gsmhard", "gsmhardv2.jsonl"), encoding="utf-8")]
    return gsm, hard


def gsmhard_candidates(gsm, hard):
    out = []
    stats = {"total": len(hard), "aligned": 0, "one_number": 0, "parsed": 0, "agree": 0}
    norm = lambda s: " ".join(s.split())
    by_q = {norm(g["question"]): k for k, g in enumerate(gsm)}
    for h in hard:
        doc = re.search(r'"""(.*?)"""', h.get("code", ""), re.S)
        i = by_q.get(norm(doc.group(1))) if doc else None
        if i is None:
            continue
        g = gsm[i]
        stats["aligned"] += 1
        d = diff_one_number(g["question"], h["input"])
        if d is None:
            continue
        stats["one_number"] += 1
        p = parse_solution(g["answer"])
        if p is None:
            continue
        stats["parsed"] += 1
        steps, final = p
        nf = replay(steps, d[0], d[1], final, question_values(g["question"]) - {d[0]})
        t = h.get("target")
        if nf is None or not isinstance(t, (int, float)) or nf.denominator != 1:
            continue
        if abs(t - round(t)) > 1e-6 or nf != round(t) or nf <= 0 or nf >= 10 ** 13:
            continue
        stats["agree"] += 1
        out.append({"idx": i, "question": h["input"].strip(), "answer": int(round(t)),
                    "steps": len(steps), "old": fmt_num(d[0]), "new": fmt_num(d[1])})
    return out, stats


def gsm8k_candidates(gsm):
    out = []
    for i, g in enumerate(gsm):
        p = parse_solution(g["answer"])
        if p is None:
            continue
        steps, final = p
        if final <= 0:
            continue
        out.append({"idx": i, "question": g["question"].strip(), "answer": int(final),
                    "steps": len(steps), "nums": len(re.findall(NUM_RE, g["question"]))})
    return out


def hardness(c):
    return (c["steps"], len(c["question"]))


VAGUE_WORDS = ["some", "several", "a few", "a number of"]


def make_infeasible(gsm, used, n):
    """vague_quantity: a count the solution needs (digits, not money, not a percentage, stated
    once) is replaced by a vague word. Replaying the GSM8K calculator chain with a different value
    for that number must change the final answer, so the problem is underdetermined."""
    res = []
    pool = [c for c in gsm8k_candidates(gsm) if c["idx"] not in used and c["steps"] >= 4
            and c["idx"] not in EXCLUDE]
    pool.sort(key=lambda c: (-c["steps"], c["idx"]))
    for c in pool:
        if len(res) >= n:
            break
        q = c["question"]
        steps, final = parse_solution(gsm[c["idx"]]["answer"])
        sents = re.split(r"(?<=[.!?])\s+", q)
        if len(sents) < 3:
            continue
        body_len = len(" ".join(sents[:-1]))  # never touch the final question sentence
        cands = []
        for m in re.finditer(r"(?<![\$\d.,/-])(\d+)(?![\d.,%/:-])", q):
            v = Fraction(m.group(1))
            if v < 3 or m.start() >= body_len:
                continue
            if len(re.findall(r"(?<![\d.])%s(?![\d.])" % m.group(1), q)) != 1:
                continue
            nxt = q[m.end():m.end() + 14]
            if not re.match(r"\s+[a-z]", nxt) or re.match(r"\s+(?:percent|am|pm|a\.m|p\.m)", nxt):
                continue
            qv = question_values(q) - {v}
            changed = [replay(steps, v, v + d, final, qv, plausible=False) for d in (1, 2, 7)]
            if any(x is None or x == final for x in changed):
                continue
            cands.append(m)
        if not cands:
            continue
        m = cands[0]
        word = VAGUE_WORDS[len(res) % len(VAGUE_WORDS)]
        newq = q[:m.start()] + word + q[m.end():]
        used.add(c["idx"])
        res.append({"type": "math", "kind": "vague_quantity", "feasible": False,
                    "instruction": newq + " " + MATH_TAIL, "check": "exact_int", "expected": None,
                    "files": {}, "meta": {"source": "gsm8k", "idx": c["idx"], "removed": m.group(1),
                                          "word": word}})
    return res


# GSM8K test items excluded after manual review (inconsistent wording in the original problem).
#   144: the problem switches the person's name (Kim / Lucas); 777: "only $6 of the cost";
#   814: "how much money" is ambiguous between quarters and dollars; 690: "2/5 times more".
EXCLUDE = {144, 777, 814, 690}


def make(dl, n_hard=13, n_gsm=12, n_inf=4):
    gsm, hard = load(dl)
    hc, stats = gsmhard_candidates(gsm, hard)
    rng = rng_for("math", "select")
    hc = [c for c in hc if c["idx"] not in EXCLUDE]
    hc.sort(key=lambda c: (-c["steps"], -len(c["question"]), c["idx"]))
    pick_h = hc[:n_hard]
    used = {c["idx"] for c in pick_h}
    gc = [c for c in gsm8k_candidates(gsm) if c["idx"] not in used and c["idx"] not in EXCLUDE]
    gc.sort(key=lambda c: (-c["steps"], -len(c["question"]), c["idx"]))
    pick_g = gc[:n_gsm]
    used |= {c["idx"] for c in pick_g}
    tasks = []
    for c in pick_h:
        tasks.append({"type": "math", "kind": "gsm_hard", "feasible": True,
                      "instruction": c["question"] + " " + MATH_TAIL, "check": "exact_int",
                      "expected": str(c["answer"]), "files": {},
                      "meta": {"source": "gsm-hard", "idx": c["idx"], "steps": c["steps"],
                               "replaced": [c["old"], c["new"]]}})
    for c in pick_g:
        tasks.append({"type": "math", "kind": "gsm8k_long", "feasible": True,
                      "instruction": c["question"] + " " + MATH_TAIL, "check": "exact_int",
                      "expected": str(c["answer"]), "files": {},
                      "meta": {"source": "gsm8k", "idx": c["idx"], "steps": c["steps"]}})
    tasks += make_infeasible(gsm, used, n_inf)
    rng.shuffle(tasks)
    print("math: gsm-hard pipeline", stats, "| picked", len(pick_h), "gsm-hard,", len(pick_g),
          "gsm8k,", sum(not t["feasible"] for t in tasks), "infeasible")
    return tasks


if __name__ == "__main__":
    import sys
    for t in make(sys.argv[1]):
        print(t["kind"], t["expected"], t["meta"], "\n   ", t["instruction"][:-len(MATH_TAIL)], "\n")

"""Extra math evaluation set (bench/sets/math2) from four public benchmarks.

Usage (downloads are only read as data, nothing from them is executed):
  python3 -I bench/hard/build_math2.py --dl <download dir> [--out bench/sets/math2] [--show]

Sources (each in its own sub-directory of --dl):
  math500/test.jsonl   MATH test problems (Hendrycks et al.), the MATH-500 subset (HuggingFaceH4/MATH-500)
  svamp/SVAMP.json     SVAMP (arkilpatel/SVAMP)
  asdiv/rows_*.json    ASDiv (EleutherAI/asdiv, JSON pages of the HF datasets-server /rows endpoint)
  gsm8k/test.jsonl     GSM8K test split (openai/grade-school-math)

Verification:
  * MATH: the last \\boxed{} of the solution must equal the dataset answer and be a plain positive
    integer; problems with diagrams ([asy]), bases, or LaTeX that has no plain-text reading are
    dropped; the remaining LaTeX is rewritten to plain text and the result must contain no
    backslash or brace. The answer itself is trusted (competition solutions).
  * SVAMP: the Equation is re-evaluated with exact rational arithmetic (AST, no code execution);
    it must equal Answer, every intermediate value must be a non-negative integer and every number
    of the equation must occur in the text.
  * ASDiv: the left side of the formula is re-evaluated the same way and must equal both its right
    side and the integer in the answer field; same intermediate and number checks.
  * GSM8K: build_math.parse_solution (every calculator annotation re-evaluated, final answer
    produced by an annotation). Items used anywhere in bench/sets/hard (keys.json meta idx) and
    build_math.EXCLUDE are skipped.
Infeasible (vague_quantity): a number the solution needs is replaced by "some"/"several"/...;
re-evaluating the equation (SVAMP/ASDiv) or replaying the GSM8K calculator chain
(build_math.make_infeasible) with other values for that number must change the answer.
MATH variants are hand-written replacements checked against the solution text.
After the automatic filters every selected problem was read by hand; rejects are listed in the
EXCLUDE_* sets with the reason.

Layout written (same as bench/sets/hard, math only):
  <out>/tasks/math_NNNN/task.json   task_id, task_type, instruction, reward
  <out>/tasks/math_NNNN/workspace/  empty
  <out>/keys.json                   {task_id: {type, reward, feasible, kind, check, expected, meta}}
  <out>/manifest.json               sources and counts
"""
import argparse
import ast
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True   # keep bench/hard free of __pycache__
import build_math  # noqa: E402
from common import MATH_TAIL, rng_for, safe_arith  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))

N_MATH, N_SVAMP, N_ASDIV, N_GSM = 25, 10, 10, 15
N_INF = {"gsm8k": 2, "svamp": 2, "asdiv": 2}   # + hand-written MATH variants (MATH_VAGUE)
MATH_SUBJECTS = {"Prealgebra": 6, "Algebra": 7, "Number Theory": 7, "Counting & Probability": 5}

SOURCES = {
    "hendrycks_math": "MATH test (Hendrycks et al. 2021), MATH-500 subset: huggingface.co/datasets/HuggingFaceH4/MATH-500 test.jsonl",
    "svamp": "SVAMP (Patel et al. 2021): github.com/arkilpatel/SVAMP SVAMP.json",
    "asdiv": "ASDiv (Miao et al. 2020): huggingface.co/datasets/EleutherAI/asdiv via datasets-server /rows (JSON)",
    "gsm8k": "GSM8K test split (Cobbe et al. 2021): github.com/openai/grade-school-math test.jsonl",
}


def h(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


# ---------------------------------------------------------------- arithmetic checks

def check_expr(expr):
    """Exact value of an arithmetic expression if it and every sub-expression is a non-negative
    integer, else None."""
    try:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                v = safe_arith(ast.unparse(node), exact=True)
                if v < 0 or v.denominator != 1:
                    return None
        v = safe_arith(expr, exact=True)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return v if v.denominator == 1 and v > 0 else None


NUMWORDS = dict(build_math.WORDS, half=2, twice=2, double=2, dozen=12, week=7, triple=3)
IMPLICIT = {Fraction(1)}   # e.g. "1+7-3+16+2" for "started on the first floor"


def text_numbers(text):
    vals = set()
    for t in re.findall(r"\d[\d,]*\.?\d*|[A-Za-z]+", text):
        if t[0].isdigit():
            try:
                vals.add(Fraction(t.replace(",", "").rstrip(".")))
            except ValueError:
                pass
        elif t.lower() in NUMWORDS:
            vals.add(Fraction(NUMWORDS[t.lower()]))
    return vals


def expr_numbers(expr):
    return [Fraction(x) for x in re.findall(r"\d+\.?\d*", expr)]


def numbers_in_text(expr, text):
    tv = text_numbers(text) | IMPLICIT
    return all(v in tv for v in expr_numbers(expr))


def difficulty(expr):
    """Distinct numbers + distinct operator kinds (repeated '+2+2+2' does not count as hard)."""
    return len(set(expr_numbers(expr))) + len(set(re.findall(r"[-+*/]", expr)))


def family(body):
    """SVAMP and ASDiv contain many variants of one story; at most one per opening is used."""
    return " ".join(re.findall(r"[A-Za-z]+", body)[:2]).lower()


def rank(c):
    """Harder first: more distinct numbers and operators, answers that are not tiny, longer text."""
    return (-c["score"], c["answer"] < 5, -len(c["text"]), h(c["id"]))


VAGUE_WORDS = ["some", "several", "a few", "a number of"]


def vague_variant(text, expr, k):
    """Replace one digit literal of `text` that the equation needs by a vague word. The literal
    must occur exactly once in the text and once in the equation, and re-evaluating the equation
    with other values for it must change the answer. Returns (new_text, removed, word) or None."""
    base = safe_arith(expr, exact=True)
    body_end = max(text.rfind(". "), 0)   # keep the question sentence intact
    for m in re.finditer(r"(?<![\$\d.,/-])(\d+)(?![\d.,%/:-])", text):
        lit = m.group(1)
        if m.start() >= body_end or int(lit) < 3:
            continue
        if len(re.findall(r"(?<![\d.])%s(?![\d.])" % lit, text)) != 1:
            continue
        occ = [e for e in re.finditer(r"(?<![\d.])%s(?:\.0)?(?![\d.])" % lit, expr)]
        if len(occ) != 1:
            continue
        if not re.match(r"\s+[a-z]", text[m.end():m.end() + 12]):
            continue
        ok = True
        for d in (1, 2, 7):
            e2 = expr[:occ[0].start()] + str(int(lit) + d) + expr[occ[0].end():]
            if safe_arith(e2, exact=True) == base:
                ok = False
        if not ok:
            continue
        word = VAGUE_WORDS[k % len(VAGUE_WORDS)]
        return text[:m.start()] + word + text[m.end():], lit, word
    return None


# ---------------------------------------------------------------- SVAMP

# Read by hand and rejected (nonsense perturbations, impossible quantities, unclear wording).
EXCLUDE_SVAMP = {
    "chal-506",   # "than on the previous 245 days"
    "chal-122",   # asks about "raisin cakes", the text has raisin cookies
    "chal-334",   # 2 roses in the vase, 4 thrown away
    "chal-985",   # "1091 weeks of harvest"
    "chal-35",    # asks for the afternoon count that is given; the equation computes the evening
}


def svamp_text(r):
    body, q = " ".join(r["Body"].split()), " ".join(r["Question"].split())
    body = re.sub(r"\$ (\d)", r"$\1", body)
    q = re.sub(r"\$ (\d)", r"$\1", q)
    for a, b in ((" n't", "n't"), (" ,", ","), (" .", ".")):
        body, q = body.replace(a, b), q.replace(a, b)
    if body and body[-1] not in ".!?,":
        q = q[0].lower() + q[1:] if re.match(r"(How|What|Which|Find)\b", q) else q
        body += ","
    return body + " " + q


def svamp_candidates(dl):
    rows = json.load(open(os.path.join(dl, "svamp", "SVAMP.json"), encoding="utf-8"))
    out = []
    for r in rows:
        eq = r["Equation"].strip()
        v = check_expr(eq)
        if v is None or v != Fraction(repr(r["Answer"])):
            continue
        text = svamp_text(r)
        if not numbers_in_text(eq, text) or "'" in re.sub(r"\w'\w|s' ", "", text):
            continue
        out.append({"id": r["ID"], "text": text, "expr": eq, "answer": int(v),
                    "score": difficulty(eq), "family": family(r["Body"])})
    out.sort(key=rank)
    return out


# ---------------------------------------------------------------- ASDiv

EXCLUDE_ASDIV = {
    865,    # unclear whether everybody left the bus at the terminal station
    1986,   # "the mean of the people"
    1981,   # a second "mean of a list" problem (1987 is kept)
    900,    # the same pencil story as 628
}


def asdiv_text(r):
    body, q = " ".join(r["body"].split()), " ".join(r["question"].split())
    if body.endswith(",") and q[:1].isupper():
        q = q[0].lower() + q[1:]
    return body + " " + q


def asdiv_candidates(dl):
    rows = []
    for f in sorted(glob.glob(os.path.join(dl, "asdiv", "rows_*.json"))):
        rows += [(x["row_idx"], x["row"]) for x in json.load(open(f, encoding="utf-8"))["rows"]]
    rows.sort()
    if [i for i, _ in rows] != list(range(len(rows))):
        raise SystemExit("asdiv: row pages incomplete")
    out = []
    for i, r in rows:
        m = re.fullmatch(r"\s*(\d+)\s*\(([^()]*)\)\s*", r["answer"])
        f = r["formula"].strip()
        if not m or f.count("=") != 1:
            continue
        lhs, rhs = (s.strip() for s in f.split("="))
        if not re.fullmatch(r"[\d\s.+\-*/()]+", lhs) or rhs != m.group(1):
            continue
        v = check_expr(lhs)
        if v is None or v != int(m.group(1)):
            continue
        text = asdiv_text(r)
        unit = m.group(2).strip().lower()
        if unit == "cents" and "cent" not in text.lower():
            continue
        if not numbers_in_text(lhs, text):
            continue
        out.append({"id": i, "text": text, "expr": lhs, "answer": int(v), "unit": unit,
                    "type": r["solution_type"], "score": difficulty(lhs), "family": family(r["body"])})
    out.sort(key=rank)
    return out


# ---------------------------------------------------------------- MATH

def boxed(s):
    i = s.rfind("\\boxed{")
    if i < 0:
        return None
    j, depth = i + len("\\boxed{"), 1
    k = j
    while k < len(s) and depth:
        depth += {"{": 1, "}": -1}.get(s[k], 0)
        k += 1
    return s[j:k - 1] if depth == 0 else None


def braced(s, i):
    """s[i] == '{': return (content, index after the closing brace)."""
    if not s.startswith("{", i):
        raise ValueError("argument without braces")
    depth, k = 0, i
    while k < len(s):
        if s[k] == "{":
            depth += 1
        elif s[k] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:k], k + 1
        k += 1
    raise ValueError("unbalanced")


# Unknown LaTeX commands are rejected by tex_math; these patterns catch the rest: diagrams,
# subscripts (bases, indexed terms), problems about number bases and line breaks.
REJECT_TEX = re.compile(r"\[asy\]|_\{?[\dA-Za-z]|\bbases?\b|\\\\|\\\|")

SIMPLE = {"\\cdot": "*", "\\times": "*", "\\div": "/", "\\le": "<=", "\\leq": "<=", "\\ge": ">=",
          "\\geq": ">=", "\\ne": "!=", "\\neq": "!=", "\\lt": "<", "\\gt": ">", "\\ldots": "...",
          "\\dots": "...", "\\cdots": "...", "\\pi": "pi", "\\left": "", "\\right": "",
          "\\lfloor": "floor(", "\\rfloor": ")", "\\lceil": "ceil(", "\\rceil": ")", "\\%": "%",
          "\\&": "&", "\\{": "{", "\\}": "}", "\\,": " ", "\\;": " ", "\\:": " ", "\\!": "",
          "\\quad": " ", "\\qquad": " ", "\\gcd": "gcd", "\\max": "max", "\\min": "min",
          "\\displaystyle": ""}
WRAP = {"\\text", "\\textbf", "\\textit", "\\mbox", "\\mathrm", "\\emph", "\\textnormal",
        "\\operatorname", "\\mathbf"}


def simple_atom(s):
    """A number, a variable or a call like sqrt(2): needs no parentheses around it."""
    return re.fullmatch(r"-?[A-Za-z0-9.]+(\([^()]*\))?", s.strip()) is not None


def tex_math(s):
    """Rewrite the inside of $...$ to plain text; raises ValueError when not possible."""
    out, i = [], 0
    while i < len(s):
        if s.startswith("\\", i):
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            cmd = "\\" + m.group(1)
            j = i + len(m.group(0))
            if cmd in ("\\frac", "\\dfrac", "\\tfrac"):
                if j < len(s) and s[j] == "{":
                    a, j = braced(s, j)
                    b, j = braced(s, j)
                else:   # \frac12
                    a, b, j = s[j], s[j + 1], j + 2
                a, b = tex_math(a), tex_math(b)
                a = a if simple_atom(a) else "(" + a + ")"
                b = b if simple_atom(b) else "(" + b + ")"
                frac = a + "/" + b
                if re.match(r"\s*(?:[\^A-Za-z0-9(]|\\left\s*\(|\\sqrt|\\d?frac)", s[j:]):
                    frac = "(" + frac + ")"   # (1/2)x^2, (1/2)^3
                if out and out[-1][-1:].isdigit():
                    frac = " " + frac   # mixed number 3\frac12 -> 3 1/2
                elif out and re.search(r"[A-Za-z)]$", out[-1]):
                    frac = "*" + frac
                out.append(frac)
            elif cmd == "\\sqrt":
                a, j = braced(s, j)
                pre = "*" if out and re.search(r"[A-Za-z0-9)]$", out[-1]) else ""
                out.append(pre + "sqrt(" + tex_math(a) + ")")
            elif cmd in WRAP:
                a, j = braced(s, j)
                out.append(a)
            elif cmd in SIMPLE:
                out.append(SIMPLE[cmd])
                if cmd in ("\\cdot", "\\times", "\\div", "\\le", "\\leq", "\\ge", "\\geq",
                           "\\ne", "\\neq", "\\lt", "\\gt"):
                    out[-1] = " " + out[-1] + " "
            else:
                raise ValueError("unknown command " + cmd)
            i = j
        elif s[i] == "^":
            if s.startswith("{", i + 1):
                a, j = braced(s, i + 1)
                a = tex_math(a)
                out.append("^" + (a if simple_atom(a) else "(" + a + ")"))
                i = j
            else:
                out.append("^")
                i += 1
        elif s.startswith("{,}", i):
            out.append(",")
            i += 3
        elif s[i] in "{}":
            i += 1   # grouping braces
        elif s[i] == "~":
            out.append(" ")
            i += 1
        else:
            out.append(s[i])
            i += 1
    t = "".join(out)
    return re.sub(r"\s+", " ", t).strip()


def tex_to_text(s):
    """MATH problem text -> plain text. ValueError if the problem needs LaTeX to be understood."""
    if REJECT_TEX.search(s):
        raise ValueError("rejected notation")
    s = s.replace("\\$", "\x00").replace("``", '"').replace("''", '"')
    # ordinal superscripts: 100^{\mathrm{th}} -> 100th
    s = re.sub(r"\^\{\s*(?:\\(?:mathrm|text|textrm|rm)\s*)?\{?\s*(th|st|nd|rd)\s*\}?\s*\}", r"\1", s)
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\))", s, flags=re.S)
    out = []
    for p in parts:
        if p.startswith("$$") or p.startswith("\\["):
            out.append(" " + tex_math(p[2:-2].replace("\x00", "$")) + " ")
        elif p.startswith("$") or p.startswith("\\("):
            k = 2 if p.startswith("\\(") else 1
            out.append(tex_math(p[k:-k].replace("\x00", "$")))
        else:
            p = p.replace("\x00", "$")
            if "\\" in p:
                p = tex_math(p)
            out.append(p)
    t = re.sub(r"\s+", " ", "".join(out)).strip()
    t = re.sub(r" ([,.;:?!])(?!=)", r"\1", t)
    if "\\" in t or "{" in t or "}" in t or "$$" in t:
        raise ValueError("leftover markup")
    return t


# Read by hand and rejected.
EXCLUDE_MATH = {
    "test/prealgebra/2066.json",   # rounding each amount or the difference gives 23 or 22
}


def math_candidates(dl):
    rows = [json.loads(l) for l in open(os.path.join(dl, "math500", "test.jsonl"), encoding="utf-8")]
    out, stats = [], Counter()
    for r in rows:
        if r["subject"] not in MATH_SUBJECTS or r["level"] < 3:
            continue
        stats["subject_level"] += 1
        ans = r["answer"].strip().replace("{,}", "").replace(",", "")
        b = boxed(r["solution"])
        if b is None or b.strip().replace("{,}", "").replace(",", "") != ans:
            stats["boxed_mismatch"] += 1
            continue
        if not re.fullmatch(r"[1-9]\d*", ans):
            continue
        stats["integer"] += 1
        if r["unique_id"] in EXCLUDE_MATH:
            continue
        try:
            text = tex_to_text(r["problem"])
        except ValueError:
            continue
        stats["plain_text"] += 1
        out.append({"id": r["unique_id"], "text": text, "answer": int(ans), "subject": r["subject"],
                    "level": r["level"], "raw": r["problem"]})
    out.sort(key=lambda c: (-c["level"], h("math", c["id"])))
    return out, stats


# Hand-written vague variants of MATH problems (not used as feasible tasks): (unique_id, old, new).
#   216: answer C(R+6,5) - C(R,5) - C(6,5) depends on the number R of Republicans.
#   765: inclusion-exclusion gives total = 3P + 30, so P needs the school size.
MATH_VAGUE = [
    ("test/counting_and_probability/216.json", "8 Republicans", "several Republicans"),
    ("test/counting_and_probability/765.json", "There are 360 people", "There are some people"),
]


# ---------------------------------------------------------------- GSM8K

def hard_gsm_idx():
    p = os.path.join(ROOT, "bench", "sets", "hard", "keys.json")
    keys = json.load(open(p, encoding="utf-8"))
    return {v["meta"]["idx"] for v in keys.values()
            if v["type"] == "math" and v.get("meta", {}).get("source") in ("gsm8k", "gsm-hard")}


EXCLUDE_GSM = {
    409,   # pay "in the two months" assumes 4-week months
    161,   # "how many fish": starfish and sea horses are counted as fish
    423,   # asks how much more the jogger jeans cost; they cost $8 less
    696,   # "3 times more points" (3x or 4x)
}


# Vague variants rejected by hand: 100 also says "1/4 times more" (unclear even with the number).
EXCLUDE_GSM_VAGUE = {100}


def gsm_candidates(gsm, used):
    cs = [c for c in build_math.gsm8k_candidates(gsm)
          if c["idx"] not in used and c["idx"] not in build_math.EXCLUDE and c["idx"] not in EXCLUDE_GSM]
    cs.sort(key=lambda c: (-c["steps"], -len(c["question"]), c["idx"]))
    return cs


# ---------------------------------------------------------------- assemble

def pick_word(cands, n, exclude):
    out, fam = [], set()
    for c in cands:
        if len(out) >= n:
            break
        if c["id"] in exclude or c["family"] in fam:
            continue
        fam.add(c["family"])
        out.append(c)
    return out, fam


def make(dl):
    tasks = []
    # MATH
    mc, mstats = math_candidates(dl)
    vague_ids = {u for u, _, _ in MATH_VAGUE}
    pick_m = []
    for subj, n in MATH_SUBJECTS.items():
        pick_m += [c for c in mc if c["subject"] == subj and c["id"] not in vague_ids][:n]
    if len(pick_m) != N_MATH:
        raise SystemExit("MATH: only %d candidates" % len(pick_m))
    for c in pick_m:
        tasks.append({"kind": "hendrycks_math", "feasible": True, "text": c["text"],
                      "expected": c["answer"],
                      "meta": {"source": "MATH-500", "unique_id": c["id"], "subject": c["subject"],
                               "level": c["level"]}})
    byid = {c["id"]: c for c in mc}
    for uid, old, new in MATH_VAGUE:
        c = byid[uid]
        if c["text"].count(old) != 1:
            raise SystemExit("MATH_VAGUE: %r not unique in %s" % (old, uid))
        tasks.append({"kind": "vague_quantity", "feasible": False, "text": c["text"].replace(old, new),
                      "expected": None, "meta": {"source": "MATH-500", "unique_id": uid,
                                                 "removed": old, "word": new}})
    # SVAMP / ASDiv
    for name, cands, n, excl in (("svamp", svamp_candidates(dl), N_SVAMP, EXCLUDE_SVAMP),
                                 ("asdiv", asdiv_candidates(dl), N_ASDIV, EXCLUDE_ASDIV)):
        pick, fams = pick_word(cands, n, excl)
        if len(pick) != n:
            raise SystemExit("%s: only %d candidates" % (name, len(pick)))
        for c in pick:
            tasks.append({"kind": name, "feasible": True, "text": c["text"], "expected": c["answer"],
                          "meta": {"source": name, "id": c["id"], "equation": c["expr"]}})
        k = 0
        for c in cands:
            if k >= N_INF[name]:
                break
            if c["id"] in excl or c["family"] in fams or c["score"] < 4:
                continue
            v = vague_variant(c["text"], c["expr"], k + (2 if name == "asdiv" else 0))
            if v is None:
                continue
            fams.add(c["family"])
            k += 1
            tasks.append({"kind": "vague_quantity", "feasible": False, "text": v[0], "expected": None,
                          "meta": {"source": name, "id": c["id"], "equation": c["expr"],
                                   "removed": v[1], "word": v[2]}})
        if k != N_INF[name]:
            raise SystemExit("%s: only %d vague variants" % (name, k))
    # GSM8K
    gsm = [json.loads(l) for l in open(os.path.join(dl, "gsm8k", "test.jsonl"), encoding="utf-8")]
    used = hard_gsm_idx()
    pick_g = gsm_candidates(gsm, used)[:N_GSM]
    for c in pick_g:
        tasks.append({"kind": "gsm8k", "feasible": True, "text": c["question"], "expected": c["answer"],
                      "meta": {"source": "gsm8k", "idx": c["idx"], "steps": c["steps"]}})
    used |= {c["idx"] for c in pick_g} | EXCLUDE_GSM
    inf = [t for t in build_math.make_infeasible(gsm, used, 4 * N_INF["gsm8k"])
           if t["meta"]["idx"] not in EXCLUDE_GSM_VAGUE and not re.search(
               re.escape(t["meta"]["word"]) + r"\s+(?:less|more|fewer|times|percent|as)\b", t["instruction"])]
    if len(pick_g) != N_GSM or len(inf) < N_INF["gsm8k"]:
        raise SystemExit("gsm8k: not enough candidates")
    for t in inf[:N_INF["gsm8k"]]:
        tasks.append({"kind": "vague_quantity", "feasible": False,
                      "text": t["instruction"][:-len(MATH_TAIL) - 1], "expected": None, "meta": t["meta"]})
    return tasks, mstats


def example_tail():
    d = os.path.join(ROOT, "data", "examples", "task-dir")
    for name in sorted(os.listdir(d)):
        t = json.load(open(os.path.join(d, name, "task.json"), encoding="utf-8"))
        if t["task_type"] == "math":
            ins = t["instruction"]
            return ins[ins.index("Compute it with the bash tool"):]
    raise SystemExit("no organizer math example found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dl", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "bench", "sets", "math2"))
    ap.add_argument("--show", action="store_true", help="print every task with its answer")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    if os.path.basename(out) != "math2" or os.path.basename(os.path.dirname(out)) != "sets":
        raise SystemExit("refusing to write outside bench/sets/math2: " + out)
    if example_tail() != MATH_TAIL:
        raise SystemExit("MATH_TAIL differs from the organizer examples")

    tasks, mstats = make(args.dl)
    for t in tasks:
        t["instruction"] = t["text"] + " " + MATH_TAIL
        if t["feasible"]:
            assert isinstance(t["expected"], int) and t["expected"] > 0, t
    rng = rng_for("math2", "order")
    tasks.sort(key=lambda t: (t["kind"], json.dumps(t["meta"], sort_keys=True)))
    rng.shuffle(tasks)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "tasks"))
    keys = {}
    for i, t in enumerate(tasks):
        tid = "math_%04d" % i
        reward = rng.randint(1, 5)
        td = os.path.join(out, "tasks", tid)
        os.makedirs(os.path.join(td, "workspace"))
        with open(os.path.join(td, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"task_id": tid, "task_type": "math", "instruction": t["instruction"],
                       "reward": reward}, f, indent=2)
            f.write("\n")
        keys[tid] = {"type": "math", "reward": reward, "feasible": t["feasible"], "kind": t["kind"],
                     "check": "exact_int", "expected": str(t["expected"]) if t["feasible"] else None,
                     "meta": t["meta"]}
    with open(os.path.join(out, "keys.json"), "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=1, ensure_ascii=False)
        f.write("\n")
    kinds = Counter(k["kind"] for k in keys.values())
    inf_src = Counter(k["meta"]["source"] for k in keys.values() if not k["feasible"])
    manifest = {"generator": "bench/hard/build_math2.py", "n": len(keys), "sources": SOURCES,
                "summary": {"math": {"tasks": len(keys), "feasible": sum(k["feasible"] for k in keys.values()),
                                     "reward_feasible": sum(k["reward"] for k in keys.values() if k["feasible"]),
                                     "kinds": dict(sorted(kinds.items())),
                                     "infeasible_by_source": dict(sorted(inf_src.items()))}},
                "math_filter": dict(mstats)}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")

    if args.show:
        for tid, k in keys.items():
            t = json.load(open(os.path.join(out, "tasks", tid, "task.json"), encoding="utf-8"))
            print("%s [%s] expected=%s meta=%s\n    %s\n" % (tid, k["kind"], k["expected"], k["meta"],
                                                          t["instruction"][:-len(MATH_TAIL) - 1]))
    print("MATH filter:", dict(mstats))
    print()
    print("%-16s %6s %9s %11s %10s" % ("kind", "tasks", "feasible", "infeasible", "reward"))
    for kind in sorted(kinds):
        ks = [k for k in keys.values() if k["kind"] == kind]
        print("%-16s %6d %9d %11d %10d" % (kind, len(ks), sum(k["feasible"] for k in ks),
                                           sum(not k["feasible"] for k in ks), sum(k["reward"] for k in ks)))
    print("%-16s %6d %9d %11d %10d" % ("TOTAL", len(keys), sum(k["feasible"] for k in keys.values()),
                                       sum(not k["feasible"] for k in keys.values()),
                                       sum(k["reward"] for k in keys.values())))
    print("infeasible by source:", dict(sorted(inf_src.items())))
    print("written to", out)


if __name__ == "__main__":
    main()

"""Coding tasks from MBPP (google-research, sanitized-mbpp.json).

Each task: mylib.py with 3-6 MBPP reference solutions (function names lower-cased, the MBPP
prompt kept as a docstring), 1-3 of them mutated with a realistic bug (AST mutation: wrong
comparison, off-by-one, wrong initial value, wrong operator, and/or swap, min/max swap, wrong
return constant, wrong sort order). The visible test_mylib.py (organizer unittest style) uses some
MBPP asserts and always contains an assert the bug breaks; the hidden test has all MBPP asserts
plus up to two extra cases per function whose expected values come from the reference solution on
structure-preserving perturbations of the MBPP inputs.

MBPP solutions are only run through sandbox_eval.py (python3 -I, temp dir, alarms, timeout).
Infeasible variants: a visible assert duplicated with a contradicting expected value.
"""
import ast
import builtins
import copy
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODING_INSTRUCTION, rng_for  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, "sandbox_eval.py")
PY = sys.executable or "python3"
SAFE_IMPORTS = {"math", "re", "collections", "heapq", "itertools", "bisect", "operator", "string",
                "functools"}
# reject list for generated reference solutions (they must be plain pure functions)
FORBIDDEN = {"open", "eval", "exec", "compile", "__import__", "input", "globals", "locals", "print",
             "exit", "quit", "setattr", "getattr", "delattr", "vars", "breakpoint", "help", "os",
             "sys", "subprocess", "shutil", "socket", "pathlib", "importlib", "memoryview"}
OK_FAIL = {"AssertionError", "IndexError", "KeyError", "ValueError", "ZeroDivisionError"}
BOOT = ("import sys, runpy; sys.path.insert(0, '.'); sys.argv = [{f!r}]; "
        "runpy.run_path({f!r}, run_name='__main__')")


# ----------------------------------------------------------------------------- preparation
class _Rename(ast.NodeTransformer):
    def __init__(self, old, new, attr=False):
        self.old, self.new, self.attr = old, new, attr

    def visit_Name(self, n):
        if n.id != self.old:
            return n
        if self.attr:
            return ast.copy_location(ast.Attribute(value=ast.Name("m", ast.Load()), attr=self.new,
                                                   ctx=ast.Load()), n)
        n.id = self.new
        return n


def doc_from_prompt(prompt):
    s = re.sub(r"^\s*write\s+(?:a|an)\s+(?:python\s+)?function\s+(?:to|that|which)\s+", "", prompt.strip(),
               flags=re.I)
    s = s[:1].upper() + s[1:]
    if not s.endswith("."):
        s += "."
    return s


def literal_args(call):
    if call.keywords:
        return None
    try:
        return [ast.literal_eval(a) for a in call.args]
    except (ValueError, SyntaxError, TypeError):
        return None


def prepare(p):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            tree = ast.parse(p["code"])
            tests = [ast.parse(t) for t in p["test_list"]]
        except (SyntaxError, SyntaxWarning, ValueError):
            return None
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    imps = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if len(fns) != 1 or len(fns) + len(imps) != len(tree.body):
        return None
    for n in imps:
        mods = [n.module] if isinstance(n, ast.ImportFrom) else [a.name for a in n.names]
        if any((m or "").split(".")[0] not in SAFE_IMPORTS for m in mods):
            return None
    test_imports = []
    for ti in p.get("test_imports", []):
        if not re.fullmatch(r"import (\w+)", ti.strip()) or ti.split()[1] not in SAFE_IMPORTS:
            return None
        test_imports.append(ti.strip())
    fn = fns[0]
    orig = fn.name
    new = orig.lower()
    if (not re.fullmatch(r"[a-z_][a-z0-9_]*", new) or keyword.iskeyword(new) or hasattr(builtins, new)
            or new in ("m", "unittest", "math")):
        return None
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    if names & FORBIDDEN or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                               ast.Global, ast.Nonlocal, ast.Yield, ast.Try))
                                for n in ast.walk(fn) if n is not fn):
        return None
    fn = _Rename(orig, new).visit(copy.deepcopy(fn))
    fn.name = new
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None),
                                                                  ast.Constant):
        fn.body = fn.body[1:]
    size = sum(1 for _ in ast.walk(fn))
    if not (25 <= size <= 260):
        return None
    loops = any(isinstance(n, (ast.For, ast.While, ast.comprehension)) for n in ast.walk(fn))
    tl = []
    for t in tests:
        if len(t.body) != 1 or not isinstance(t.body[0], ast.Assert):
            return None
        test = t.body[0].test
        calls = [n for n in ast.walk(test) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == orig]
        if not calls:
            return None
        plain = _Rename(orig, new).visit(copy.deepcopy(test))
        unit = _Rename(orig, new, attr=True).visit(copy.deepcopy(test))
        if isinstance(unit, ast.Compare) and len(unit.ops) == 1 and isinstance(unit.ops[0], ast.Eq):
            u = "self.assertEqual(%s, %s)" % (ast.unparse(unit.left), ast.unparse(unit.comparators[0]))
            left = unit.left
            try:
                exp = ast.literal_eval(unit.comparators[0])
            except (ValueError, SyntaxError, TypeError):
                exp = None
        elif isinstance(unit, ast.UnaryOp) and isinstance(unit.op, ast.Not):
            u, left, exp = "self.assertFalse(%s)" % ast.unparse(unit.operand), None, None
        else:
            u, left, exp = "self.assertTrue(%s)" % ast.unparse(unit), None, None
        top_call = calls[0]
        tl.append({"plain": "assert " + ast.unparse(plain), "unit": u,
                   "left": None if left is None else ast.unparse(left),
                   "expected": exp, "has_expected": isinstance(unit, ast.Compare) and exp is not None,
                   "direct": isinstance(test, ast.Compare) and test.left is top_call,
                   "args": literal_args(top_call)})
    doc = doc_from_prompt(p["prompt"])
    imports = sorted({ast.unparse(n) for n in imps})
    return {"mbpp_id": p["task_id"], "orig": orig, "name": new, "fn": fn, "doc": doc,
            "imports": imports, "test_imports": test_imports, "tests": tl, "size": size,
            "loops": loops}


def fn_source(fn, doc):
    f = copy.deepcopy(fn)
    f.body.insert(0, ast.Expr(ast.Constant(doc)))
    return ast.unparse(f)


# ----------------------------------------------------------------------------- mutations
CMP = {ast.Lt: [ast.LtE, ast.Gt], ast.LtE: [ast.Lt], ast.Gt: [ast.GtE, ast.Lt], ast.GtE: [ast.Gt],
       ast.Eq: [ast.NotEq], ast.NotEq: [ast.Eq]}
BIN = {ast.Add: [ast.Sub], ast.Sub: [ast.Add], ast.Mult: [ast.Add], ast.FloorDiv: [ast.Mod],
       ast.Mod: [ast.FloorDiv]}


def mutation_sites(fn):
    """List of (walk_index, action, kind)."""
    sites = []
    body_assigns = {id(s) for s in fn.body if isinstance(s, ast.Assign)}
    n_returns = sum(isinstance(n, ast.Return) for n in ast.walk(fn))
    for i, n in enumerate(ast.walk(fn)):
        if isinstance(n, ast.Compare):
            for j, op in enumerate(n.ops):
                for new in CMP.get(type(op), []):
                    sites.append((i, ("cmp", j, new), "wrong_comparison"))
        elif isinstance(n, (ast.BinOp, ast.AugAssign)) and type(n.op) in BIN:
            for new in BIN[type(n.op)]:
                sites.append((i, ("op", new), "wrong_operator"))
        elif isinstance(n, ast.BoolOp):
            sites.append((i, ("bool",), "wrong_bool_op"))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in ("max", "min"):
                sites.append((i, ("minmax",), "min_max_swap"))
            elif n.func.id == "range" and 1 <= len(n.args) <= 2 and not n.keywords:
                stop = n.args[-1]
                if (isinstance(stop, ast.BinOp) and isinstance(stop.op, (ast.Add, ast.Sub))
                        and isinstance(stop.right, ast.Constant) and stop.right.value == 1):
                    sites.append((i, ("range_unwrap",), "off_by_one"))
                else:
                    sites.append((i, ("range_stop", ast.Sub), "off_by_one"))
                    sites.append((i, ("range_stop", ast.Add), "off_by_one"))
                if len(n.args) == 2 and isinstance(n.args[0], ast.Constant) and n.args[0].value in (0, 1):
                    sites.append((i, ("range_start",), "off_by_one"))
            elif n.func.id == "sorted" and not any(k.arg == "reverse" for k in n.keywords):
                sites.append((i, ("sorted_rev",), "wrong_sort_order"))
        elif isinstance(n, ast.Assign) and id(n) in body_assigns and isinstance(n.value, ast.Constant):
            v = n.value.value
            if v is True or v is False or (type(v) is int and v in (0, 1)):
                sites.append((i, ("init",), "wrong_init"))
        elif isinstance(n, ast.Subscript):
            sl = n.slice
            if isinstance(sl, ast.Constant) and type(sl.value) is int and sl.value in (0, 1):
                sites.append((i, ("index",), "off_by_one"))
            elif isinstance(sl, ast.Slice):
                for part in ("lower", "upper"):
                    b = getattr(sl, part)
                    if isinstance(b, ast.Constant) and type(b.value) is int and b.value in (1, 2):
                        sites.append((i, ("slice", part), "off_by_one"))
        elif (isinstance(n, ast.Return) and n_returns >= 2 and isinstance(n.value, ast.Constant)
              and isinstance(n.value.value, bool)):
            sites.append((i, ("ret",), "wrong_return"))
    return sites


def apply_mutation(fn, idx, action):
    f = copy.deepcopy(fn)
    n = list(ast.walk(f))[idx]
    a = action[0]
    if a == "cmp":
        n.ops[action[1]] = action[2]()
    elif a == "op":
        n.op = action[1]()
    elif a == "bool":
        n.op = ast.Or() if isinstance(n.op, ast.And) else ast.And()
    elif a == "minmax":
        n.func.id = "min" if n.func.id == "max" else "max"
    elif a == "range_unwrap":
        n.args[-1] = n.args[-1].left
    elif a == "range_stop":
        n.args[-1] = ast.BinOp(left=n.args[-1], op=action[1](), right=ast.Constant(1))
    elif a == "range_start":
        n.args[0] = ast.Constant(1 - n.args[0].value)
    elif a == "sorted_rev":
        n.keywords.append(ast.keyword(arg="reverse", value=ast.Constant(True)))
    elif a == "init":
        v = n.value.value
        n.value = ast.Constant((not v) if isinstance(v, bool) else 1 - v)
    elif a == "index":
        n.slice = ast.Constant(1 - n.slice.value)
    elif a == "slice":
        b = getattr(n.slice, action[1])
        setattr(n.slice, action[1], ast.Constant(b.value - 1))
    elif a == "ret":
        n.value = ast.Constant(not n.value.value)
    ast.fix_missing_locations(f)
    return f


# ----------------------------------------------------------------------------- sandbox
def sandbox(jobs, timeout=300, chunk=150):
    """Run jobs through sandbox_eval.py in a temp dir, in chunks; splits a chunk on failure."""
    if not jobs:
        return []
    if len(jobs) > chunk:
        out = []
        for k in range(0, len(jobs), chunk):
            out += sandbox(jobs[k:k + chunk], timeout, chunk)
        return out
    tmp = tempfile.mkdtemp(prefix="hard_sbx_")
    try:
        jf = os.path.join(tmp, "jobs.json")
        with open(jf, "w", encoding="utf-8") as f:
            json.dump(jobs, f)
        try:
            p = subprocess.run([PY, "-I", "-B", SANDBOX, jf], cwd=tmp, capture_output=True, text=True,
                               timeout=timeout)
            if p.returncode == 0:
                return json.loads(p.stdout)
        except subprocess.TimeoutExpired:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if len(jobs) == 1:
        return [{"ok": False, "err": "Crash", "tests": [], "calls": []}]
    h = len(jobs) // 2
    return sandbox(jobs[:h], timeout, chunk) + sandbox(jobs[h:], timeout, chunk)


def module_src(c, fn=None):
    lines = c["imports"] + c["test_imports"]
    return "\n".join(lines + [ast.unparse(fn if fn is not None else c["fn"])]) + "\n"


def perturb(args):
    out = []
    for k, a in enumerate(args):
        if type(a) is int and 0 < a <= 50:
            for d in (1, 2):
                out.append(args[:k] + [a + d] + args[k + 1:])
        elif isinstance(a, (list, tuple)) and len(a) >= 2 and all(type(x) is int for x in a):
            out.append(args[:k] + [type(a)(x + 1 for x in a)] + args[k + 1:])
            if len(a) >= 3:
                out.append(args[:k] + [a[:-1]] + args[k + 1:])
        elif isinstance(a, str) and len(a) >= 4:
            out.append(args[:k] + [a[:-1]] + args[k + 1:])
    return out


def has_float(v):
    if isinstance(v, float):
        return True
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(has_float(x) for x in v)
    if isinstance(v, dict):
        return any(has_float(x) or has_float(y) for x, y in v.items())
    return False


def literal_value(r):
    try:
        v = ast.literal_eval(r)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None, False
    return v, True


def analyse(cands):
    """Attach valid mutants and extra cases to each candidate. Returns usable candidates."""
    jobs, index = [], []
    for c in cands:
        tests = [t["plain"] for t in c["tests"]]
        jobs.append({"src": module_src(c), "tests": tests})
        index.append((c, None))
        seen = {ast.unparse(c["fn"])}
        for idx, action, kind in mutation_sites(c["fn"]):
            mf = apply_mutation(c["fn"], idx, action)
            s = ast.unparse(mf)
            if s in seen:
                continue
            seen.add(s)
            jobs.append({"src": module_src(c, mf), "tests": tests})
            index.append((c, (mf, kind, action[0])))
    res = sandbox(jobs)
    for c in cands:
        c["ref_ok"], c["mutants"] = False, []
    for (c, mut), r in zip(index, res):
        if mut is None:
            c["ref_ok"] = r["ok"] and len(r["tests"]) == len(c["tests"]) and all(p for p, _ in r["tests"])
            continue
        if not r["ok"] or len(r["tests"]) != len(c["tests"]):
            continue
        fails = [k for k, (p, e) in enumerate(r["tests"]) if not p]
        if not fails or any(r["tests"][k][1] not in OK_FAIL for k in fails):
            continue
        c["mutants"].append({"fn": mut[0], "kind": mut[1], "action": mut[2], "fails": fails})
    cands = [c for c in cands if c["ref_ok"]]
    # extra cases: reference on perturbed MBPP inputs, evaluated in two separate processes
    jobs, index = [], []
    for c in cands:
        existing = {json.dumps(t["args"], default=repr) for t in c["tests"] if t["args"] is not None}
        calls = []
        for t in c["tests"]:
            if t["args"] is None or not t["direct"]:
                continue
            for a in perturb(t["args"]):
                key = json.dumps(a, default=repr)
                if key in existing:
                    continue
                existing.add(key)
                calls.append("%s(%s)" % (c["name"], ", ".join(repr(x) for x in a)))
        c["extra_calls"] = calls[:8]
        jobs.append({"src": module_src(c), "calls": c["extra_calls"]})
        index.append((c, None))
        for mu in c["mutants"]:
            jobs.append({"src": module_src(c, mu["fn"]), "calls": c["extra_calls"]})
            index.append((c, mu))
    r1 = sandbox(jobs)
    r2 = sandbox([j for j, (c, mu) in zip(jobs, index) if mu is None])
    k2 = 0
    for (c, mu), r in zip(index, r1):
        if mu is None:
            other = r2[k2]
            k2 += 1
            c["extras"] = []
            for call, a, b in zip(c["extra_calls"], r["calls"], other["calls"]):
                if not (a[0] and b[0]):
                    continue
                va, oka = literal_value(a[1])
                vb, okb = literal_value(b[1])
                if not (oka and okb) or va != vb or has_float(va) or len(a[1]) > 300:
                    continue
                c["extras"].append({"call": call, "repr": repr(va), "value": va})
        else:
            mu["extra_out"] = r["calls"] if r["ok"] else None
    return cands


# ----------------------------------------------------------------------------- assembly
def render_mylib(funcs):
    imports = sorted({i for f in funcs for i in f["imports"]})
    parts = []
    if imports:
        parts.append("\n".join(imports))
    parts += [f["src"] for f in funcs]
    return "\n\n\n".join(parts) + "\n"


def render_test(test_imports, methods):
    head = "".join(l + "\n" for l in sorted(set(test_imports)))
    head += "import unittest\n\nimport mylib as m\n\n\nclass T(unittest.TestCase):\n"
    blocks = ["    def %s(self):\n%s" % (name, "".join("        %s\n" % s for s in stmts))
              for name, stmts in methods]
    return head + "\n".join(blocks) + "\n\nif __name__ == '__main__':\n    unittest.main()\n"


def contradict_value(v):
    if isinstance(v, bool):
        return not v
    if type(v) is int:
        return v + 1
    if isinstance(v, str):
        return v + (v[-1:] or "x")
    if isinstance(v, list):
        return v + v[-1:] if v else [0]
    if isinstance(v, tuple):
        return v + v[-1:] if v else (0,)
    return None


def build_task(rng, pool, used, feasible=True, contra_kind=None):
    k = rng.choice([3, 4, 4, 5, 5, 6])
    nb = rng.choice([1, 2, 2, 3, 3]) if feasible else rng.choice([1, 2])
    nb = min(nb, k)
    avail = [c for c in pool if c["mbpp_id"] not in used and ("name", c["name"]) not in used]
    rng.shuffle(avail)
    chosen, names = [], set()
    for c in avail:
        if c["name"] in names:
            continue
        if len(chosen) < nb and not c["mutants"]:
            continue
        chosen.append(c)
        names.add(c["name"])
        if len(chosen) == k:
            break
    if len(chosen) < k:
        return None
    bug_idx = set(range(nb))
    funcs, vis_methods, hid_methods, bugs = [], [], [], []
    test_imports = []
    for i, c in enumerate(chosen):
        ref_src = fn_source(c["fn"], c["doc"])
        mut = None
        if i in bug_idx:
            kinds = sorted({m["kind"] for m in c["mutants"]})
            kind = rng.choice(kinds)
            opts = [m for m in c["mutants"] if m["kind"] == kind]
            mut = rng.choice(opts)
            bugs.append([c["name"], mut["kind"]])
        src = fn_source(mut["fn"], c["doc"]) if mut else ref_src
        funcs.append({"name": c["name"], "src": src, "ref": ref_src, "imports": c["imports"]})
        test_imports += c["test_imports"]
        order = list(range(len(c["tests"])))
        rng.shuffle(order)
        if mut:
            first = rng.choice(mut["fails"])
            vis = [first] + ([x for x in order if x != first][:1] if rng.random() < 0.5 else [])
        else:
            vis = order[:2] if rng.random() < 0.3 else order[:1]
        vis.sort()
        vis_methods.append(("test_" + c["name"], [c["tests"][x]["unit"] for x in vis]))
        extras = list(c["extras"])
        if mut and mut.get("extra_out") is not None:
            out = dict(zip(c["extra_calls"], mut["extra_out"]))
            # prefer extra cases the bug also breaks
            extras.sort(key=lambda e: (not (out.get(e["call"]) is not None
                                             and (not out[e["call"]][0] or out[e["call"]][1] != e["repr"])),
                                       e["call"]))
            # drop cases on which the buggy version would hang
            extras = [e for e in extras if out.get(e["call"], [False, "TimeLimit"])[1] != "TimeLimit"]
        extras = extras[:2]
        hid = [t["unit"] for t in c["tests"]]
        hid += ["self.assertEqual(m.%s, %s)" % (e["call"], e["repr"]) for e in extras]
        hid_methods.append(("test_" + c["name"], hid))
        used.add(c["mbpp_id"])
        used.add(("name", c["name"]))
    order = list(range(k))
    rng.shuffle(order)
    funcs = [funcs[i] for i in order]
    vis_methods = [vis_methods[i] for i in order]
    hid_methods = [hid_methods[i] for i in order]
    contra = None
    if not feasible:
        # pick a function whose visible test has a literal expected value
        opts = []
        for mi, (name, stmts) in enumerate(vis_methods):
            c = next(x for x in chosen if "test_" + x["name"] == name)
            for t in c["tests"]:
                if t["unit"] in stmts and t["has_expected"] and contradict_value(t["expected"]) is not None:
                    opts.append((mi, t))
        if not opts:
            return None
        mi, t = rng.choice(opts)
        line = "self.assertEqual(%s, %s)" % (t["left"], repr(contradict_value(t["expected"])))
        name, stmts = vis_methods[mi]
        if contra_kind == "contradict_same_method":
            vis_methods[mi] = (name, stmts + [line])
        else:
            vis_methods.insert(mi + 1, (name + "_again", [line]))
        contra = {"line": line, "function": name[5:]}
    files = {"mylib.py": render_mylib(funcs).encode(),
             "test_mylib.py": render_test(test_imports, vis_methods).encode()}
    ref = render_mylib([{"src": f["ref"], "imports": f["imports"]} for f in funcs])
    hidden = render_test(test_imports, hid_methods)
    return {"type": "coding", "kind": "fix_bugs" if feasible else contra_kind, "feasible": feasible,
            "instruction": CODING_INSTRUCTION, "check": "unittest", "expected": "tests_pass" if feasible else None,
            "files": files, "hidden": {"test_hidden.py": hidden, "mylib_ref.py": ref},
            "meta": {"source": "mbpp-sanitized", "mbpp_ids": [c["mbpp_id"] for c in chosen],
                     "functions": [f["name"] for f in funcs], "bugs": bugs, "contradiction": contra}}


# ----------------------------------------------------------------------------- verification
def run_test(files, test_name, test_src, timeout=60):
    tmp = tempfile.mkdtemp(prefix="hard_cod_")
    try:
        for name, data in files.items():
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode())
        with open(os.path.join(tmp, test_name), "w", encoding="utf-8") as f:
            f.write(test_src)
        try:
            p = subprocess.run([PY, "-I", "-B", "-c", BOOT.format(f=test_name)], cwd=tmp,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return p.returncode == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify(t):
    """Returns a list of problems (empty when the task is consistent)."""
    vis = t["files"]["test_mylib.py"].decode()
    hid = t["hidden"]["test_hidden.py"]
    buggy = {"mylib.py": t["files"]["mylib.py"]}
    ref = {"mylib.py": t["hidden"]["mylib_ref.py"]}
    errs = []
    if run_test(buggy, "test_mylib.py", vis):
        errs.append("buggy passes visible")
    if run_test(buggy, "test_hidden.py", hid):
        errs.append("buggy passes hidden")
    for _ in range(3):  # repeated: hash randomisation must not matter
        if not run_test(ref, "test_hidden.py", hid):
            errs.append("ref fails hidden")
            break
    if t["feasible"]:
        for _ in range(3):
            if not run_test(ref, "test_mylib.py", vis):
                errs.append("ref fails visible")
                break
    else:
        line = t["meta"]["contradiction"]["line"]
        if run_test(ref, "test_mylib.py", vis):
            errs.append("ref passes contradictory visible")
        lines = vis.split("\n")
        k = [i for i, l in enumerate(lines) if l.strip() == line]
        if len(k) != 1:
            errs.append("contradiction line not unique")
        else:
            i = k[0]
            if lines[i - 1].strip().startswith("def ") and lines[i - 1].strip().endswith("_again(self):"):
                stripped = lines[:i - 1] + lines[i + 2:]
            else:
                stripped = lines[:i] + lines[i + 1:]
            if not run_test(ref, "test_mylib.py", "\n".join(stripped)):
                errs.append("ref fails visible without contradiction")
    return errs


def make(dl, n_feasible=25, n_inf=4):
    data = json.load(open(os.path.join(dl, "mbpp", "sanitized-mbpp.json"), encoding="utf-8"))
    cands = [c for c in (prepare(p) for p in sorted(data, key=lambda p: p["task_id"])) if c]
    n_prep = len(cands)
    cands = analyse(cands)
    with_mut = sum(1 for c in cands if c["mutants"])
    print("coding: MBPP sanitized %d problems, %d usable single-function, %d reference-verified, "
          "%d with a valid mutant" % (len(data), n_prep, len(cands), with_mut))
    tasks, used = [], set()
    attempts = 0
    plan = [(True, None)] * n_feasible + [(False, "contradict_same_method"), (False, "contradict_two_methods")] * (n_inf // 2)
    for i, (feas, ck) in enumerate(plan):
        while True:
            attempts += 1
            if attempts > 400:
                raise SystemExit("coding: cannot assemble enough tasks")
            rng = rng_for("coding", i, attempts)
            snapshot = set(used)
            t = build_task(rng, cands, used, feas, ck)
            if t is None:
                used.clear()
                used.update(snapshot)
                continue
            errs = verify(t)
            if errs:
                print("  coding: rejected attempt", attempts, errs, t["meta"]["bugs"])
                used.clear()
                used.update(snapshot)
                continue
            tasks.append(t)
            break
    return tasks


if __name__ == "__main__":
    ts = make(sys.argv[1])
    for t in ts:
        print(t["kind"], t["meta"]["functions"], t["meta"]["bugs"])

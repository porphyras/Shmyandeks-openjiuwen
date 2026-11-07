#!/usr/bin/env python3
"""Fill a run directory with answers from a set's keys (for testing the bench and the grader).

Usage:
  python3 bench/oracle_fill.py --set bench/sets/train_a --run /tmp/run1 [--fresh]
        [--mode oracle|overfit|wrong|none] [--where task|workspace|both] [--infeasible-answer TEXT]

If --run does not exist (or --fresh is given) it is created as a copy of <set>/tasks.
Modes:
  oracle  feasible tasks get the expected answer; coding gets the reference mylib.py + DONE
  overfit like oracle, but coding gets a test-overfitted mylib (negative control: proves the grader
          rejects test-overfitting; (buggy code that special-cases the
          visible test inputs) -> should pass visible tests and fail hidden ones
  wrong   feasible tasks get a wrong answer; coding keeps the buggy mylib and says DONE
  none    only copy the tasks (no answers)
"""
import argparse
import ast
import json
import os
import shutil
import sys


def write_answer(td, text, where):
    targets = []
    if where in ("task", "both"):
        targets.append(os.path.join(td, "answer.txt"))
    if where in ("workspace", "both"):
        os.makedirs(os.path.join(td, "workspace"), exist_ok=True)
        targets.append(os.path.join(td, "workspace", "answer.txt"))
    for p in targets:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text + "\n")


def visible_cases(test_src):
    """[(func_name, [args...], expected)] from `self.assertEqual(m.f(...), EXP)` lines."""
    cases = []
    for node in ast.walk(ast.parse(test_src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "assertEqual" and len(node.args) == 2):
            continue
        call, exp = node.args
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "m"):
            continue
        try:
            args = [ast.literal_eval(a) for a in call.args]
            val = ast.literal_eval(exp)
        except ValueError:
            continue
        cases.append((call.func.attr, args, val))
    return cases


def overfit_mylib(buggy_src, test_src):
    by_fn = {}
    for name, args, val in visible_cases(test_src):
        by_fn.setdefault(name, []).append((args, val))
    lines = [buggy_src.rstrip("\n"), "", ""]
    for name, pairs in by_fn.items():
        lines += ["_orig_%s = %s" % (name, name), "",
                  "def %s(*args):" % name,
                  "    for a, r in %r:" % (pairs,),
                  "        if list(args) == a:",
                  "            return r",
                  "    return _orig_%s(*args)" % name, "", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def wrong_value(expected, check):
    if check == "exact_int":
        return str(int(expected) + 1)
    return str(expected) + "_x"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--fresh", action="store_true", help="delete --run first and re-copy the tasks")
    ap.add_argument("--mode", choices=["oracle", "overfit", "wrong", "none"], default="oracle")
    ap.add_argument("--where", choices=["task", "workspace", "both"], default="task")
    ap.add_argument("--infeasible-answer", default=None, help="text to write for infeasible tasks")
    a = ap.parse_args(argv)

    if a.fresh and os.path.exists(a.run):
        shutil.rmtree(a.run)
    if not os.path.exists(a.run):
        shutil.copytree(os.path.join(a.set, "tasks"), a.run)
    with open(os.path.join(a.set, "keys.json"), encoding="utf-8") as f:
        keys = json.load(f)
    n = 0
    for tid, k in keys.items():
        td = os.path.join(a.run, tid)
        if not os.path.isdir(td):
            print("missing task dir", td, file=sys.stderr)
            continue
        if a.mode == "none":
            continue
        if not k["feasible"]:
            if a.infeasible_answer is not None:
                write_answer(td, a.infeasible_answer, a.where)
            continue
        if k["check"] == "unittest":
            ws = os.path.join(td, "workspace")
            if a.mode == "oracle":
                ref = os.path.join(a.set, "hidden", tid, "mylib_ref.py")
                if not os.path.isfile(ref):
                    print("no reference mylib for", tid, file=sys.stderr)
                    continue
                shutil.copyfile(ref, os.path.join(ws, "mylib.py"))
            elif a.mode == "overfit":
                with open(os.path.join(ws, "mylib.py"), encoding="utf-8") as f:
                    buggy = f.read()
                with open(os.path.join(a.set, "tasks", tid, "workspace", "test_mylib.py"), encoding="utf-8") as f:
                    test = f.read()
                with open(os.path.join(ws, "mylib.py"), "w", encoding="utf-8") as f:
                    f.write(overfit_mylib(buggy, test))
            write_answer(td, "DONE", a.where)
        else:
            val = k["expected"] if a.mode in ("oracle", "overfit") else wrong_value(k["expected"], k["check"])
            write_answer(td, str(val), a.where)
        n += 1
    print("filled %d tasks in %s (mode=%s, where=%s)" % (n, a.run, a.mode, a.where))
    return 0


if __name__ == "__main__":
    sys.exit(main())

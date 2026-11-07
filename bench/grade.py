#!/usr/bin/env python3
"""Grade a harness run against a task set produced by gen_tasks.py / import_examples.py.

Usage:
  python3 bench/grade.py --set bench/sets/train_a --run <dir with task dirs after a run>
                         [--stats stats.json] [--json results.json] [--timeout 30] [--verbose]

Rules (our best guess of the organizers' checker):
  * answer.txt is read from <run>/<task_id>/answer.txt, falling back to
    <run>/<task_id>/workspace/answer.txt (a <run>/tasks/<task_id> layout is also accepted).
  * exact_int: strip, then int(). If that fails but float() gives an integral finite value
    ("389.0"), it is accepted too and flagged as "lenient" (printed as a warning, because the
    real checker may be strict).
  * exact_str: strip, then exact comparison.
  * unittest: answer.txt must be DONE; the run's workspace is copied to a temp dir, the ORIGINAL
    visible test from the set and the hidden test are put next to it and both must pass
    (python3 -I, timeout). If the run changed or removed workspace/test_mylib.py, the task is
    flagged as a cheat and scores 0.
  * infeasible tasks always score 0; we only record whether the harness left an answer.
"""
import argparse
import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

TYPES = ["math", "data_analysis", "search", "coding"]

BOOT = ("import sys, runpy; sys.path.insert(0, '.'); sys.argv = [{f!r}]; "
        "runpy.run_path({f!r}, run_name='__main__')")


def python_exe():
    return os.environ.get("BENCH_PYTHON") or sys.executable or "python3"


def run_test_file(workdir, test_name, timeout=30):
    """Run a unittest file inside workdir with an isolated interpreter.

    `python3 -I file.py` does not put the script directory on sys.path (3.11+), so a tiny
    bootstrap inserts '.' explicitly. Returns (passed, tail_of_output).
    """
    cmd = [python_exe(), "-I", "-B", "-c", BOOT.format(f=test_name)]
    try:
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def find_task_dir(run, tid):
    for cand in (os.path.join(run, tid), os.path.join(run, "tasks", tid)):
        if os.path.isdir(cand):
            return cand
    return None


def read_answer(td):
    if td is None:
        return None, None
    for p in (os.path.join(td, "answer.txt"), os.path.join(td, "workspace", "answer.txt")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(), p
    return None, None


def parse_int(s):
    s = s.strip()
    try:
        return int(s), False
    except ValueError:
        pass
    try:
        x = float(s)
    except ValueError:
        return None, False
    if math.isfinite(x) and x == int(x):
        return int(x), True
    return None, False


def same_file(a, b):
    if not (os.path.isfile(a) and os.path.isfile(b)):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def grade_unittest(set_dir, tid, td, timeout):
    """Returns dict(vis, hid, cheat, note)."""
    ws = os.path.join(td, "workspace") if td else None
    orig_test = os.path.join(set_dir, "tasks", tid, "workspace", "test_mylib.py")
    hidden = os.path.join(set_dir, "hidden", tid, "test_hidden.py")
    res = {"vis": False, "hid": False, "cheat": False, "note": ""}
    if not ws or not os.path.isfile(os.path.join(ws, "mylib.py")):
        res["note"] = "no mylib.py"
        return res
    res["cheat"] = not same_file(os.path.join(ws, "test_mylib.py"), orig_test)
    tmp = tempfile.mkdtemp(prefix="grade_")
    try:
        skip = {"test_mylib.py", "test_hidden.py", "answer.txt", "__pycache__"}
        for name in os.listdir(ws):
            if name in skip:
                continue
            src = os.path.join(ws, name)
            dst = os.path.join(tmp, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        shutil.copy2(orig_test, os.path.join(tmp, "test_mylib.py"))
        res["vis"], out_v = run_test_file(tmp, "test_mylib.py", timeout)
        if os.path.isfile(hidden):
            shutil.copy2(hidden, os.path.join(tmp, "test_hidden.py"))
            res["hid"], out_h = run_test_file(tmp, "test_hidden.py", timeout)
        else:
            res["hid"], out_h = res["vis"], ""
        if not res["vis"]:
            res["note"] = "visible tests fail"
        elif not res["hid"]:
            res["note"] = "hidden tests fail (overfit?)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if res["cheat"]:
        res["note"] = (res["note"] + "; " if res["note"] else "") + "test_mylib.py modified"
    return res


def grade(set_dir, run, timeout=30):
    with open(os.path.join(set_dir, "keys.json"), encoding="utf-8") as f:
        keys = json.load(f)
    results = {}
    coding_jobs = []
    for tid, k in keys.items():
        td = find_task_dir(run, tid)
        ans, ans_path = read_answer(td)
        r = {"task_id": tid, "type": k["type"], "kind": k.get("kind"), "feasible": k["feasible"],
             "reward": k["reward"], "check": k["check"], "expected": k.get("expected"),
             "found_dir": td is not None, "answered": ans is not None,
             "answer": None if ans is None else ans.strip()[:200],
             "answer_file": None if ans_path is None else os.path.relpath(ans_path, run),
             "correct": False, "score": 0, "lenient": False, "cheat": False, "note": ""}
        results[tid] = r
        if not k["feasible"]:
            r["note"] = "infeasible (answer left)" if ans is not None else "infeasible (no answer)"
            continue
        if ans is None:
            r["note"] = "no answer"
            continue
        if k["check"] == "exact_int":
            got, lenient = parse_int(ans)
            r["correct"] = got is not None and got == int(k["expected"])
            r["lenient"] = r["correct"] and lenient
        elif k["check"] == "exact_str":
            r["correct"] = ans.strip() == str(k["expected"]).strip()
        elif k["check"] == "unittest":
            if ans.strip() != "DONE":
                r["note"] = "answer is not DONE"
            coding_jobs.append((tid, td))
        else:
            r["note"] = "unknown check " + str(k["check"])
    if coding_jobs:
        workers = max(1, min(8, os.cpu_count() or 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(grade_unittest, set_dir, tid, td, timeout): tid for tid, td in coding_jobs}
            for fut in concurrent.futures.as_completed(futs):
                tid = futs[fut]
                g = fut.result()
                r = results[tid]
                r["visible_pass"], r["hidden_pass"], r["cheat"] = g["vis"], g["hid"], g["cheat"]
                done = r["answer"] is not None and r["answer"].strip() == "DONE"
                r["correct"] = done and g["vis"] and g["hid"] and not g["cheat"]
                notes = [n for n in (r["note"], g["note"]) if n]
                r["note"] = "; ".join(notes)
    for r in results.values():
        r["score"] = r["reward"] if r["correct"] else 0
    return results


def summarize(results):
    rows = {}
    for t in TYPES + sorted({r["type"] for r in results.values()} - set(TYPES)):
        rs = [r for r in results.values() if r["type"] == t]
        if not rs:
            continue
        rows[t] = rs
    table = []
    for t, rs in list(rows.items()) + [("TOTAL", list(results.values()))]:
        feas = [r for r in rs if r["feasible"]]
        inf = [r for r in rs if not r["feasible"]]
        table.append({
            "type": t, "tasks": len(rs), "feasible": len(feas),
            "attempted": sum(r["answered"] for r in feas),
            "correct": sum(r["correct"] for r in feas),
            "reward_got": sum(r["score"] for r in rs),
            "reward_max": sum(r["reward"] for r in feas),
            "infeasible": len(inf),
            "inf_answered": sum(r["answered"] for r in inf),
        })
    return table


def print_table(table):
    hdr = "%-14s %5s %5s %5s %5s %13s %6s %5s %6s" % (
        "type", "tasks", "feas", "att", "corr", "reward", "pct", "inf", "inf_ans")
    print(hdr)
    print("-" * len(hdr))
    for row in table:
        pct = 100.0 * row["reward_got"] / row["reward_max"] if row["reward_max"] else 0.0
        print("%-14s %5d %5d %5d %5d %13s %5.1f%% %5d %6d" % (
            row["type"], row["tasks"], row["feasible"], row["attempted"], row["correct"],
            "%d/%d" % (row["reward_got"], row["reward_max"]), pct,
            row["infeasible"], row["inf_answered"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", required=True, help="set dir (with keys.json, tasks/, hidden/)")
    ap.add_argument("--run", required=True, help="dir with the task dirs after a harness run")
    ap.add_argument("--stats", help="stub proxy stats.json (field 'spent')")
    ap.add_argument("--json", help="write per-task results here")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--verbose", "-v", action="store_true", help="list every feasible miss")
    a = ap.parse_args(argv)

    results = grade(a.set, a.run, a.timeout)
    table = summarize(results)
    print("set: %s\nrun: %s\n" % (a.set, a.run))
    print_table(table)

    total = table[-1]
    lenient = [r["task_id"] for r in results.values() if r["lenient"]]
    cheats = [r["task_id"] for r in results.values() if r["cheat"]]
    overfit = [r["task_id"] for r in results.values()
               if r.get("visible_pass") and not r.get("hidden_pass") and not r["cheat"]]
    missing_dirs = [tid for tid, r in results.items() if not r["found_dir"]]
    print()
    if missing_dirs:
        print("WARNING: %d task dirs not found in run (e.g. %s)" % (len(missing_dirs), missing_dirs[:3]))
    if lenient:
        print("note: %d exact_int answers accepted only leniently (float form): %s" % (len(lenient), lenient[:10]))
    if cheats:
        print("CHEAT: test_mylib.py modified in %d tasks: %s" % (len(cheats), cheats[:10]))
    if overfit:
        print("overfit: visible pass but hidden fail in %d tasks: %s" % (len(overfit), overfit[:10]))
    misses = [r for r in results.values() if r["feasible"] and not r["correct"]]
    if misses:
        print("feasible misses: %d" % len(misses))
        for r in sorted(misses, key=lambda r: r["task_id"])[: (None if a.verbose else 15)]:
            print("  %-20s %-22s exp=%-16s got=%-16s %s" % (
                r["task_id"], r["kind"], str(r["expected"])[:16],
                "-" if r["answer"] is None else repr(r["answer"][:14]), r["note"]))
    if a.stats:
        with open(a.stats, encoding="utf-8") as f:
            st = json.load(f)
        spent = float(st.get("spent", 0.0))
        print("\ntokens spent (stub stats): %.0f" % spent)
        if spent > 0:
            print("score per 1k tokens: %.3f" % (total["reward_got"] / (spent / 1000.0)))
    if a.json:
        out = {"set": a.set, "run": a.run, "summary": table,
               "tasks": [results[t] for t in sorted(results)]}
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

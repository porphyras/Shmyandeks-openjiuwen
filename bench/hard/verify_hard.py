"""Independent checks of a written hard set.

Usage: python3 -I bench/hard/verify_hard.py [--set bench/sets/hard] [--pandas-python PATH] [--skip-grade]

  * layout: task.json has exactly task_id/task_type/instruction/reward, ids <type>_<4 digits>,
    reward 1..5, keys.json fields, expected null for infeasible, exact_int answers are integers;
  * boilerplate: instruction tails equal the organizer examples (data/examples/task-dir) word for word;
  * search: feasible answer occurs verbatim in a kb file; infeasible: the dataset answer (and
    aliases) occurs in no kb file;
  * data_analysis: every feasible answer recomputed with pandas (verify_data_pandas.py);
    infeasible premises checked (missing column / entity / file / period);
  * coding: buggy mylib fails visible and hidden tests, reference passes both (3 runs);
    infeasible: reference fails only because of the contradicting line;
  * oracle run through bench/grade.py: expected answers / reference fixes score 100 % on
    feasible tasks; the unfixed coding workspaces score 0.
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import build_coding  # noqa: E402

TYPES = ("math", "data_analysis", "search", "coding")


def example_tails():
    """Boilerplate copied from the organizer examples (everything after the question)."""
    tails = {}
    d = os.path.join(ROOT, "data", "examples", "task-dir")
    for name in sorted(os.listdir(d)):
        t = json.load(open(os.path.join(d, name, "task.json"), encoding="utf-8"))
        ins = t["instruction"]
        if t["task_type"] == "coding":
            tails["coding"] = ins
        elif t["task_type"] == "math":
            tails["math"] = ins[ins.index("Compute it with the bash tool"):]
        elif t["task_type"] == "data_analysis":
            tails["data_analysis"] = ins[ins.index("Use bash with python3"):]
        elif t["task_type"] == "search":
            tails["search"] = ins[ins.index("Use the file tools"):]
            tails["search_head"] = ins[:ins.index("knowledge base of markdown files.") + len("knowledge base of markdown files.")]
    return tails


def read_ws(sd, tid):
    ws = os.path.join(sd, "tasks", tid, "workspace")
    files = {}
    for dp, _, fns in os.walk(ws):
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                files[os.path.relpath(p, ws)] = f.read()
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(ROOT, "bench", "sets", "hard"))
    ap.add_argument("--pandas-python", default=os.path.join(ROOT, ".venv", "bin", "python"))
    ap.add_argument("--skip-grade", action="store_true")
    a = ap.parse_args()
    sd = os.path.abspath(a.set)
    keys = json.load(open(os.path.join(sd, "keys.json"), encoding="utf-8"))
    tails = example_tails()
    errs = []

    def err(tid, msg):
        errs.append("%s: %s" % (tid, msg))

    task_dirs = sorted(os.listdir(os.path.join(sd, "tasks")))
    if sorted(keys) != task_dirs:
        err("set", "keys.json and tasks/ differ")
    for tid, k in keys.items():
        tj = json.load(open(os.path.join(sd, "tasks", tid, "task.json"), encoding="utf-8"))
        if set(tj) != {"task_id", "task_type", "instruction", "reward"}:
            err(tid, "task.json fields %s" % sorted(tj))
        if tj["task_id"] != tid or not re.fullmatch(r"(math|data_analysis|search|coding)_\d{4}", tid):
            err(tid, "bad id")
        if tj["task_type"] != k["type"] or not tid.startswith(k["type"] + "_"):
            err(tid, "type mismatch")
        if tj["reward"] != k["reward"] or not 1 <= k["reward"] <= 5:
            err(tid, "reward")
        if not {"type", "reward", "feasible", "kind", "check", "expected"} <= set(k):
            err(tid, "keys fields")
        if k["check"] not in ("exact_int", "exact_str", "unittest"):
            err(tid, "check")
        if not k["feasible"] and k["expected"] is not None:
            err(tid, "infeasible with expected")
        if k["feasible"] and k["check"] == "exact_int" and not re.fullmatch(r"-?\d+", str(k["expected"])):
            err(tid, "exact_int expected not an integer")
        if k["feasible"] and k["check"] == "exact_str" and (not k["expected"] or k["expected"] != k["expected"].strip()):
            err(tid, "bad exact_str expected")
        ins = tj["instruction"]
        ty = k["type"]
        if ty == "coding":
            if ins != tails["coding"]:
                err(tid, "coding instruction differs from the examples")
        elif not ins.endswith(" " + tails[ty]):
            err(tid, "instruction tail differs from the examples")
        if ty == "search" and not ins.startswith(tails["search_head"] + " "):
            err(tid, "search head differs")
        if (ty == "coding") != (k["check"] == "unittest"):
            err(tid, "check/type")

    # search
    n_search = 0
    for tid, k in keys.items():
        if k["type"] != "search":
            continue
        files = read_ws(sd, tid)
        texts = [v.decode("utf-8") for p, v in files.items() if p.startswith("kb" + os.sep) and p.endswith(".md")]
        if len(texts) != len(files) or len(texts) < 10:
            err(tid, "kb layout")
        for t in texts:
            if not t.startswith("# ") or "\n\n" not in t:
                err(tid, "kb file format")
                break
        if k["feasible"]:
            if not any(k["expected"] in t for t in texts):
                err(tid, "answer not verbatim in kb")
        else:
            hid = k["meta"]["hidden_answer"].lower()
            if any(hid in t.lower() for t in texts):
                err(tid, "hidden answer present in kb")
        n_search += 1

    # data_analysis: pandas recomputation + infeasible premises
    n_data = 0
    p = subprocess.run([a.pandas_python, "-I", os.path.join(HERE, "verify_data_pandas.py"), sd],
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        err("data", "pandas check failed: " + p.stderr[-500:])
        recomputed = {}
    else:
        recomputed = json.loads(p.stdout)
    for tid, k in keys.items():
        if k["type"] != "data_analysis":
            continue
        n_data += 1
        files = read_ws(sd, tid)
        if k["feasible"]:
            exp, got = recomputed.get(tid, [k["expected"], None])
            if str(got) != str(k["expected"]):
                err(tid, "pandas gives %r, key %r" % (got, k["expected"]))
            continue
        kind = k["kind"]
        if kind == "missing_column":
            head = files["hotel_data.csv"].decode().splitlines()[0]
            if "parking" in head:
                err(tid, "column exists")
        elif kind == "absent_entity":
            rows = list(csv.DictReader(io.StringIO(files["patients.csv"].decode())))
            if any(r["city"] == "Oslo" for r in rows):
                err(tid, "entity exists")
        elif kind == "missing_file":
            if "carriers.csv" in files:
                err(tid, "file exists")
        elif kind == "absent_period":
            if "2021-03-" in files["loans.csv"].decode():
                err(tid, "period present")

    # coding
    n_cod = 0
    for tid, k in keys.items():
        if k["type"] != "coding":
            continue
        n_cod += 1
        files = read_ws(sd, tid)
        if sorted(files) != ["mylib.py", "test_mylib.py"]:
            err(tid, "coding workspace files %s" % sorted(files))
            continue
        hd = os.path.join(sd, "hidden", tid)
        t = {"feasible": k["feasible"], "files": files, "meta": k["meta"],
             "hidden": {n: open(os.path.join(hd, n), encoding="utf-8").read()
                        for n in ("test_hidden.py", "mylib_ref.py")}}
        for e in build_coding.verify(t):
            err(tid, e)

    # oracle / none runs through the team grader
    grade_lines = []
    if not a.skip_grade:
        for mode in ("oracle", "none"):
            run = tempfile.mkdtemp(prefix="hard_run_")
            try:
                for tid, k in keys.items():
                    src = os.path.join(sd, "tasks", tid)
                    dst = os.path.join(run, tid)
                    shutil.copytree(src, dst)
                    if not k["feasible"]:
                        continue
                    if k["check"] == "unittest":
                        if mode == "oracle":
                            shutil.copy(os.path.join(sd, "hidden", tid, "mylib_ref.py"),
                                        os.path.join(dst, "workspace", "mylib.py"))
                        ans = "DONE"
                    else:
                        ans = k["expected"] if mode == "oracle" else ""
                    with open(os.path.join(dst, "answer.txt"), "w", encoding="utf-8") as f:
                        f.write(ans + "\n")
                jf = os.path.join(run, "_grade.json")
                g = subprocess.run([sys.executable, os.path.join(ROOT, "bench", "grade.py"), "--set", sd,
                                    "--run", run, "--json", jf], capture_output=True, text=True, timeout=1200)
                res = json.load(open(jf, encoding="utf-8"))
                tot = res["summary"][-1]
                grade_lines.append("grade.py %-6s: correct %d/%d feasible, reward %d/%d"
                                   % (mode, tot["correct"], tot["feasible"], tot["reward_got"], tot["reward_max"]))
                if mode == "oracle" and tot["correct"] != tot["feasible"]:
                    bad = [r["task_id"] for r in res["tasks"] if r["feasible"] and not r["correct"]]
                    err("grade", "oracle misses %s" % bad)
                if mode == "none" and tot["correct"] != 0:
                    err("grade", "unfixed run scores %d" % tot["correct"])
            finally:
                shutil.rmtree(run, ignore_errors=True)

    print("checked %d tasks (search %d, data %d with pandas, coding %d)" % (len(keys), n_search, n_data, n_cod))
    for line in grade_lines:
        print(line)
    if errs:
        print("PROBLEMS (%d):" % len(errs))
        for e in errs:
            print("  " + e)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()

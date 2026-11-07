#!/usr/bin/env python3
"""Convert the organizer examples into the bench set layout.

Usage:
  python3 bench/import_examples.py --src data/examples/task-dir --out bench/sets/examples

expected/check move from task.json into keys.json; coding hidden test = copy of the visible test;
the reference mylib.py is assembled from the generator's canonical function templates (when every
function name is known) and checked against the visible test. Every expected answer is also
recomputed with the generator's independent checkers.
"""
import argparse
import ast
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True  # keep bench/ free of __pycache__
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gen_tasks as G  # noqa: E402
from grade import run_test_file  # noqa: E402


def instruction_key(task_type, instruction):
    for cut in (" Compute it with the bash tool", " Use bash with python3", " Use the file tools"):
        if cut in instruction:
            body = instruction.split(cut)[0]
            if task_type == "search" and body.startswith(G.SEARCH_HEAD + " "):
                body = body[len(G.SEARCH_HEAD) + 1:]
            return body
    return None


def read_tree(root):
    files = {}
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for name in sorted(fn):
            p = os.path.join(dp, name)
            rel = os.path.relpath(p, root)
            with open(p, encoding="utf-8", newline="") as f:
                files["workspace/" + rel] = f.read()
    return files


def reference_mylib(mylib_src):
    names = [n.name for n in ast.parse(mylib_src).body if isinstance(n, ast.FunctionDef)]
    if not names or any(n not in G.FNS_BY_NAME for n in names):
        return None
    return "\n\n".join(G.FNS_BY_NAME[n].src for n in names)


def passes(mylib, test_src):
    d = tempfile.mkdtemp(prefix="imp_")
    try:
        with open(os.path.join(d, "mylib.py"), "w") as f:
            f.write(mylib)
        with open(os.path.join(d, "test_mylib.py"), "w") as f:
            f.write(test_src)
        return run_test_file(d, "test_mylib.py")[0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    G.safe_reset_dir(a.out)
    keys, entries, contexts, coding_ids = {}, [], {}, set()
    for tid in sorted(os.listdir(a.src)):
        tj = os.path.join(a.src, tid, "task.json")
        if not os.path.isfile(tj):
            continue
        with open(tj, encoding="utf-8") as f:
            task = json.load(f)
        t = task["task_type"]
        dst = os.path.join(a.out, "tasks", tid)
        os.makedirs(dst)
        G.write_task_json(os.path.join(dst, "task.json"),
                          {k: task[k] for k in ("task_id", "task_type", "instruction", "reward") if k in task})
        ws = os.path.join(a.src, tid, "workspace")
        files = {}
        if os.path.isdir(ws):
            shutil.copytree(ws, os.path.join(dst, "workspace"))
            files = read_tree(ws)
        kind = G.classify(t, task["instruction"])
        expected = task.get("expected")
        check = task.get("check")
        hidden = {}
        status = "ok"
        if t == "math":
            got = G.verify_math(task["instruction"])
        elif t == "data_analysis":
            got = G.verify_data(task["instruction"], os.path.join(dst, "workspace", "sales.csv"))
        elif t == "search":
            got = G.verify_search(task["instruction"], os.path.join(dst, "workspace", "kb"))
        else:
            got = expected
            mylib = files.get("workspace/mylib.py", "")
            test = files.get("workspace/test_mylib.py", "")
            hdir = os.path.join(a.out, "hidden", tid)
            os.makedirs(hdir)
            shutil.copyfile(os.path.join(ws, "test_mylib.py"), os.path.join(hdir, "test_hidden.py"))
            ref = reference_mylib(mylib)
            if ref is not None and passes(ref, test) and not passes(mylib, test):
                G.write_text(os.path.join(hdir, "mylib_ref.py"), ref)
                hidden["mylib_ref.py"] = ref
            else:
                status = "no reference fix (unknown functions)"
            coding_ids.add(tid)
        if t != "coding" and str(got) != str(expected):
            status = "CHECKER MISMATCH: checker=%r" % (got,)
        print("%-20s %-14s expected=%-12s %s" % (tid, kind, expected, status))
        keys[tid] = {"type": t, "reward": task["reward"], "feasible": True, "kind": kind, "check": check,
                     "expected": expected, "meta": {"source": "organizer example"}}
        contexts[tid] = G.task_context(task["instruction"], files)
        entries.append({"tid": tid, "type": t, "feasible": True, "expected": expected,
                        "key": None if t == "coding" else instruction_key(t, task["instruction"]),
                        "files": files, "meta": {}, "hidden": hidden})
    mock = G.build_oracle_mock([e for e in entries if e["type"] != "coding" or e["hidden"]], contexts, coding_ids)
    with open(os.path.join(a.out, "keys.json"), "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "oracle_mock.json"), "w", encoding="utf-8") as f:
        json.dump(mock, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"generator": "import_examples.py", "src": a.src, "n": len(keys)}, f, indent=1)
    print("imported %d tasks into %s" % (len(keys), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

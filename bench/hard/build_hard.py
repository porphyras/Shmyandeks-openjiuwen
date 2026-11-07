"""Assemble the HARD set from public benchmarks into the bench layout.

Usage (from anywhere; downloads are only read as data):
  python3 -I bench/hard/build_hard.py --dl <download dir> [--out bench/sets/hard]

Layout written (same as bench/sets/train_b):
  <out>/tasks/<task_id>/task.json         task_id, task_type, instruction, reward
  <out>/tasks/<task_id>/workspace/...     CSV files | kb/*.md | mylib.py + test_mylib.py (math: none)
  <out>/keys.json                         {task_id: {type, reward, feasible, kind, check, expected, meta}}
  <out>/hidden/<task_id>/test_hidden.py   coding: stricter hidden test (+ mylib_ref.py)
  <out>/manifest.json                     sources and counts
Run verify_hard.py afterwards.
"""
import argparse
import json
import os
import shutil
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coding  # noqa: E402
import build_data  # noqa: E402
import build_math  # noqa: E402
import build_search  # noqa: E402
from common import rng_for  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TYPES = ["math", "data_analysis", "search", "coding"]
SOURCES = {
    "math": "GSM-Hard (reasoning-machines/gsm-hard, gsmhardv2.jsonl) + GSM8K test split (grade-school-math test.jsonl)",
    "coding": "MBPP sanitized (google-research/google-research mbpp/sanitized-mbpp.json)",
    "search": "MuSiQue full v1.0 dev (HF bdsaglam/musique, musique_full_v1.0_dev.jsonl)",
    "data_analysis": "InfiAgent-DABench dev (InfiAgent/InfiAgent examples/DA-Agent/data) + synthetic two-file tables",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dl", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "bench", "sets", "hard"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    if os.path.basename(out) != "hard" or os.path.basename(os.path.dirname(out)) != "sets":
        raise SystemExit("refusing to write outside bench/sets/hard: " + out)

    tasks = []
    tasks += build_math.make(args.dl)
    tasks += build_data.make(args.dl)
    tasks += build_search.make(args.dl)
    tasks += build_coding.make(args.dl)

    rng = rng_for("hard", "order")
    rng.shuffle(tasks)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "tasks"))
    keys = {}
    for i, t in enumerate(tasks):
        tid = "%s_%04d" % (t["type"], i)
        reward = rng.randint(1, 5)
        td = os.path.join(out, "tasks", tid)
        ws = os.path.join(td, "workspace")
        os.makedirs(ws)
        with open(os.path.join(td, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"task_id": tid, "task_type": t["type"], "instruction": t["instruction"],
                       "reward": reward}, f, indent=2)
            f.write("\n")
        for rel, data in sorted(t["files"].items()):
            p = os.path.join(ws, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        if t.get("hidden"):
            hd = os.path.join(out, "hidden", tid)
            os.makedirs(hd)
            for name, text in t["hidden"].items():
                with open(os.path.join(hd, name), "w", encoding="utf-8") as f:
                    f.write(text)
        keys[tid] = {"type": t["type"], "reward": reward, "feasible": t["feasible"], "kind": t["kind"],
                     "check": t["check"], "expected": t["expected"] if t["feasible"] else None,
                     "meta": t["meta"]}
    keys = dict(sorted(keys.items(), key=lambda kv: int(kv[0].rsplit("_", 1)[1])))
    with open(os.path.join(out, "keys.json"), "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=1, ensure_ascii=False)
        f.write("\n")

    summary = {}
    for ty in TYPES:
        ks = [k for k in keys.values() if k["type"] == ty]
        summary[ty] = {"tasks": len(ks), "feasible": sum(k["feasible"] for k in ks),
                       "reward_feasible": sum(k["reward"] for k in ks if k["feasible"]),
                       "kinds": dict(Counter(k["kind"] for k in ks).most_common())}
    manifest = {"generator": "bench/hard/build_hard.py", "n": len(keys), "sources": SOURCES,
                "summary": summary}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")

    print()
    print("%-14s %6s %9s %11s  kinds" % ("type", "tasks", "feasible", "infeasible"))
    for ty in TYPES:
        s = summary[ty]
        print("%-14s %6d %9d %11d  %s" % (ty, s["tasks"], s["feasible"], s["tasks"] - s["feasible"],
                                          ", ".join("%s=%d" % kv for kv in s["kinds"].items())))
    print("%-14s %6d %9d %11d" % ("TOTAL", len(keys), sum(k["feasible"] for k in keys.values()),
                                  sum(not k["feasible"] for k in keys.values())))
    print("written to", out)


if __name__ == "__main__":
    main()

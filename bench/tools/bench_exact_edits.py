"""Compare oracle reply sizes for the generator's coding tasks, optionally run hidden tests.

python3 bench/tools/bench_exact_edits.py --set bench/sets/NAME --verify

This measures characters of known reference artifacts, not actual model output or
billed tokens. References and hidden tests stay outside the submitted harness.
"""
import argparse
import ast
import difflib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.context import Task  # noqa: E402
from harness.edits import EditError, apply_exact_edits  # noqa: E402
from harness import crossfix  # noqa: E402
from harness import solver as solver_module  # noqa: E402


def edit_reply(original, reference):
    a, b = original.splitlines(keepends=True), reference.splitlines(keepends=True)
    for context in sorted({0, 1, 2, 4, 8, len(a)}):
        groups = difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(n=context)
        edits = [["".join(a[g[0][1]:g[-1][2]]), "".join(b[g[0][3]:g[-1][4]])] for g in groups]
        reply = json.dumps(edits, ensure_ascii=False, separators=(",", ":"))
        try:
            changed = apply_exact_edits({"mylib.py": original}, reply)
        except EditError:
            continue
        if changed == {"mylib.py": reference}:
            return reply
    raise ValueError("Cannot represent the reference with bounded unique edits")


def function_reply(original, reference):
    old = {n.name: ast.dump(n) for n in ast.parse(original).body if isinstance(n, ast.FunctionDef)}
    lines = reference.splitlines(keepends=True)
    changed = ["".join(lines[n.lineno - 1:n.end_lineno]) for n in ast.parse(reference).body
               if isinstance(n, ast.FunctionDef) and old.get(n.name) != ast.dump(n)]
    if not changed:
        raise ValueError("Expected changed top-level functions in the reference")
    return "```python\n%s\n```" % "\n\n".join(changed)


def verify(task_dir, hidden, reply, mode):
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "task"
        shutil.copytree(task_dir, target)
        task = Task(str(target), json.loads((target / "task.json").read_text()))
        calls = []

        def ask(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise AssertionError("Oracle artifact unexpectedly needed another call")
            return reply

        solver = solver_module.Solver(ask)
        info = solver.prepare(task)
        with patch.object(solver_module, "CODE_EDITS", mode):
            outcome = solver.solve(task, info, 0)
        if outcome.status != "solved" or len(calls) != 1 or solver._tests_tampered(info):
            raise AssertionError("Visible test failure: " + outcome.note)
        path = Path(task.cwd) / "test_hidden.py"
        shutil.copyfile(hidden, path)
        ok, log = solver._run_tests(task, [str(path)])
        if not ok:
            raise AssertionError("Hidden test failure: " + log)


def free_coding_ids(root, keys):
    """Reproduce the existing crossfix preflight on temporary copies, never on the set."""
    with tempfile.TemporaryDirectory() as tmp:
        solver, pairs, free = solver_module.Solver(None), [], set()
        for tid, key in sorted(keys.items()):
            if key["type"] != "coding":
                continue
            target = Path(tmp) / tid
            shutil.copytree(root / "tasks" / tid, target)
            task = Task(str(target), json.loads((target / "task.json").read_text()))
            pairs.append((task, solver.prepare(task)))
        index = crossfix.build_index(pairs)
        for task, info in pairs:
            if not keys[task.id]["feasible"]:
                continue
            if (info.get("initial_pass") and not info.get("problems")) or crossfix.repair(solver, task, info, index):
                free.add(task.id)
        return free


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--after-crossfix", action="store_true",
                        help="exclude coding tasks already repaired by the existing free preflight")
    args = parser.parse_args()
    keys = json.loads((args.set / "keys.json").read_text())
    free = free_coding_ids(args.set, keys) if args.after_crossfix else set()
    rows = []
    for tid, key in sorted(keys.items()):
        if key["type"] != "coding" or not key["feasible"] or tid in free:
            continue
        task_dir = args.set / "tasks" / tid
        original = (task_dir / "workspace/mylib.py").read_text()
        reference = (args.set / "hidden" / tid / "mylib_ref.py").read_text()
        full, edits = function_reply(original, reference), edit_reply(original, reference)
        if args.verify:
            hidden = args.set / "hidden" / tid / "test_hidden.py"
            verify(task_dir, hidden, full, False)
            verify(task_dir, hidden, edits, True)
        rows.append({"task": tid, "function_reply_chars": len(full), "edit_reply_chars": len(edits)})
    if not rows and not free:
        parser.error("No feasible coding tasks found")
    full = sum(r["function_reply_chars"] for r in rows)
    edits = sum(r["edit_reply_chars"] for r in rows)
    print(json.dumps({"tasks": len(rows), "function_reply_chars": full, "edit_reply_chars": edits,
                      "excluded_free_coding_tasks": len(free),
                      "reply_char_reduction_percent": round(100 * (1 - edits / full), 1) if full else None,
                      "smaller_edit_replies": sum(r["edit_reply_chars"] < r["function_reply_chars"] for r in rows),
                      "extra_instruction_chars_per_first_call": len(solver_module.EDIT_REPLY) - len(solver_module.FUNCTION_REPLY),
                      "both_protocols_pass_visible_and_hidden": args.verify if rows else None,
                      "measurement": "oracle artifact characters; not actual model tokens", "per_task": rows}, indent=2))


if __name__ == "__main__":
    main()

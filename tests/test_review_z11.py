"""Offline tests for the z11 review fixes (no network).

Run with: python3 -m unittest discover -s tests -v

OS/archive litter (.DS_Store, __MACOSX) is not part of a task; tasks one level deeper are found;
a crossfix index that cannot be built does not end the run; a kept model patch is answered DONE
when the tests could not run here (a missing package, a plain script); the likely target source
comes first in the prompt; nan/inf in a column and a headerless CSV give no false verdict.
"""
import json
import os
import shutil
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_agent
from harness import context as ctx
from harness import fastpath
from harness.solver import Outcome, Solver

EXAMPLES = Path(__file__).resolve().parent.parent / "bench" / "sets" / "examples" / "tasks"


class LitterTests(unittest.TestCase):
    def test_ds_store_keeps_the_fast_path(self):
        if not (EXAMPLES / "search_0005").is_dir():
            self.skipTest("bench examples not present")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "search_0005"
            shutil.copytree(EXAMPLES / "search_0005", d)
            ws = d / "workspace" if (d / "workspace").is_dir() else d
            kb = next(p for p in ws.rglob("*") if p.is_dir()) if any(p.is_dir() for p in ws.rglob("*")) else ws
            (kb / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1\x00\x10\xff" * 50)
            (ws / "__MACOSX").mkdir()
            (ws / "__MACOSX" / "._x.md").write_bytes(b"\x00\x05\x16\x07")
            task = ctx.Task(str(d), json.loads((d / "task.json").read_text()))
            names = [os.path.basename(p) for p in ctx.list_files(task.cwd)]
            self.assertNotIn(".DS_Store", names)
            self.assertNotIn("._x.md", names)
            out = fastpath.solve(task, "search")
            self.assertIsNotNone(out)
            self.assertEqual(out[0], "solved")

    def test_tasks_one_level_deeper(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("task-dir/math_0000", "task-dir/math_0001"):
                p = Path(tmp) / name
                p.mkdir(parents=True)
                (p / "task.json").write_text(json.dumps({"task_type": "math", "instruction": "Compute 6*7."}))
            (Path(tmp) / "__MACOSX" / "task-dir").mkdir(parents=True)
            self.assertEqual(sorted(t.name for t in ctx.load_tasks(tmp)), ["math_0000", "math_0001"])


class CrossfixIndexFailureTests(unittest.TestCase):
    def test_run_survives_a_broken_index(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        d = root / "math_0"
        d.mkdir()
        (d / "task.json").write_text(json.dumps({"task_id": "math_0", "task_type": "math",
                                                 "instruction": "Compute six times seven.", "reward": 1}))
        client = SimpleNamespace(budget=None, spent=0, calls=0, by_model={}, exhausted=False)
        client.remaining = lambda: None
        args = run_agent.parse_args(["--tasks-dir", str(root), "--out", str(root / "s.json"), "--workers", "1"])
        env = {"HARNESS_FAST": "0", "HARNESS_CAREFUL_MAX": "0", "HARNESS_MATH_XC": "off"}
        with patch.dict(os.environ, env, clear=True), patch("harness.llm.LLM", return_value=client), \
                patch.object(run_agent, "STOP", threading.Event()), patch.object(run_agent, "T0", time.time()), \
                patch.object(run_agent, "trace"), \
                patch("harness.crossfix.build_index", side_effect=RecursionError("deep")):
            run = run_agent.Run(args, "offline-dummy")
            run.solver = SimpleNamespace(prepare=lambda t: {"kind": "math", "ctx_chars": 0, "problems": []},
                                         solve=lambda t, info, level: Outcome("solved", "42"))
            with patch.object(run, "estimate", return_value=(1.0, 10.0)), patch.object(run, "time_left", return_value=1000.0):
                run.run()
        self.assertEqual(run.results, {"math_0": "solved"})


def coding_task(tmp, files):
    for rel, text in files.items():
        p = Path(tmp) / "workspace" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return ctx.Task(tmp, {"task_type": "coding", "instruction": "Fix the bug in mylib.py. Write DONE."})


FIX = "```python\ndef double(x):\n    return 2 * x\n```"


class UnverifiableDoneTests(unittest.TestCase):
    def solve(self, files, reply=FIX):
        with tempfile.TemporaryDirectory() as tmp:
            task = coding_task(tmp, files)
            solver = Solver(lambda *a, **k: reply)
            info = solver.prepare(task)
            self.assertEqual(info["problems"], [])
            return solver.solve(task, info, 0), Path(tmp, "workspace", "mylib.py").read_text()

    def test_missing_package_test_gets_done(self):
        out, src = self.solve({"mylib.py": "def double(x):\n    return x + 1\n",
                               "test_mylib.py": """
                               import unittest
                               import sklearn
                               from mylib import double
                               class T(unittest.TestCase):
                                   def test_double(self):
                                       self.assertEqual(double(3), 6)
                               if __name__ == "__main__":
                                   unittest.main()
                               """})
        self.assertEqual((out.status, out.answer), ("failed", "DONE"))
        self.assertIn("2 * x", src)

    def test_plain_assert_script_gets_done(self):
        out, src = self.solve({"mylib.py": "def double(x):\n    return x + 1\n",
                               "test_mylib.py": "from mylib import double\nassert double(3) == 6\n"})
        self.assertEqual((out.status, out.answer), ("failed", "DONE"))

    def test_red_tests_stay_without_done(self):
        out, src = self.solve({"mylib.py": "def double(x):\n    return x + 1\n",
                               "test_mylib.py": """
                               import unittest
                               from mylib import double
                               class T(unittest.TestCase):
                                   def test_double(self):
                                       self.assertEqual(double(3), 7)
                               if __name__ == "__main__":
                                   unittest.main()
                               """})
        self.assertIsNone(out.answer)


class TargetFirstTests(unittest.TestCase):
    def test_target_source_is_in_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {n: "def f_%s():\n    return 1\n" % n[0] for n in ("cli.py", "config.py", "formatting.py")}
            files["mylib.py"] = "def double(x):\n    return x + 1\n"
            files["test_mylib.py"] = ("import unittest\nfrom mylib import double\nclass T(unittest.TestCase):\n"
                                      "    def test_double(self):\n        self.assertEqual(double(3), 6)\n"
                                      "if __name__ == '__main__':\n    unittest.main()\n")
            task = coding_task(tmp, files)
            info = Solver(None).prepare(task)
            self.assertIn("[mylib.py]", info["ctx"])


class DataTests(unittest.TestCase):
    def test_nan_and_inf_do_not_break_the_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "d.csv")
            Path(p).write_text("a,b\nnan,1\ninf,2\n3,-inf\n")
            text, info = ctx.csv_brief(p)
            self.assertIn("a", info["header"])

    def test_headerless_or_piped_csv_is_no_free_verdict(self):
        q = ("The file sales.csv has columns order_id,region,product,month,units,price (3 rows). "
             "How many units of product 'widget' were sold in months 5 through 7 inclusive?")
        rows = ["1,west,widget,5,5,33", "2,north,widget,7,28,28", "3,north,gizmo,8,39,17"]
        for content in ("\n".join(rows) + "\n",
                        "order_id|region|product|month|units|price\n" + "\n".join(r.replace(",", "|") for r in rows) + "\n"):
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "workspace"
                ws.mkdir()
                (ws / "sales.csv").write_text(content)
                task = ctx.Task(tmp, {"task_type": "data_analysis", "instruction": q})
                _, problems = ctx.data_context(task)
                self.assertFalse([p for p in problems if "missing in file" in p], (content[:30], problems))

if __name__ == "__main__":
    unittest.main()

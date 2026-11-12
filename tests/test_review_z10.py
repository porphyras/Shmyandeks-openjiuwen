"""Offline tests for the z10 review fixes (no network).

Run with: python3 -m unittest discover -s tests -v

A numeric required value does not fix its type (4 == 4.0); a test file that exits 0 with failing
tests is a failure; tests under tests/ find the module at the task root; ANSWER lines with
markdown emphasis or other casing are read; a code block tagged ```Python is code.
"""
import os
import tempfile
import textwrap
import unittest

from harness import context as ctx
from harness.solver import Solver, _raw_answer, extract_code

HEAD = "import unittest\nimport mylib as m\nclass T(unittest.TestCase):\n"


class NumericTypeTests(unittest.TestCase):
    def test_float_check_on_int_literal_is_no_verdict(self):
        body = ("    def test_a(self):\n        self.assertEqual(m.average([2, 4, 6]), 4)\n"
                "        self.assertIsInstance(m.average([2, 4, 6]), float)\n")
        self.assertEqual(ctx.test_contradictions(HEAD + body), [])
        body = ("    def test_a(self):\n        self.assertEqual(m.average([2, 4, 6]), 4)\n"
                "        self.assertEqual(str(m.average([2, 4, 6])), '4.0')\n")
        self.assertEqual(ctx.test_contradictions(HEAD + body), [])

    def test_non_numeric_type_still_contradicts(self):
        body = ("    def test_a(self):\n        self.assertEqual(m.average([2, 4, 6]), 4)\n"
                "        self.assertIsInstance(m.average([2, 4, 6]), str)\n")
        self.assertTrue(ctx.test_contradictions(HEAD + body))


def run_tests(files, layout_root_module=True):
    d = tempfile.mkdtemp()
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(textwrap.dedent(text))
    task = ctx.Task(d, {"task_type": "coding", "instruction": "Fix the bug."})
    tests = [os.path.join(d, r) for r in files if os.path.basename(r).startswith("test_")]
    return Solver(None)._run_tests_once(task, tests, 60)


class RunnerTests(unittest.TestCase):
    def test_exit_false_with_failures_is_a_failure(self):
        ok, out = run_tests({"mylib.py": "def f():\n    return 1\n",
                             "test_mylib.py": """
                             import unittest, mylib
                             class T(unittest.TestCase):
                                 def test_f(self):
                                     self.assertEqual(mylib.f(), 2)
                             unittest.main(exit=False)
                             """})
        self.assertFalse(ok)

    def test_tests_folder_imports_root_module(self):
        files = {"mylib.py": "def f():\n    return 2\n",
                 "tests/test_mylib.py": """
                 import unittest, mylib
                 class T(unittest.TestCase):
                     def test_f(self):
                         self.assertEqual(mylib.f(), 2)
                 if __name__ == "__main__":
                     unittest.main()
                 """}
        ok, out = run_tests(files)
        self.assertTrue(ok, out)


class ParsingTests(unittest.TestCase):
    def test_answer_forms(self):
        for reply in ("**ANSWER:** Osaka", "ANSWER: **Osaka**", "Answer: Osaka", "ANSWER: Osaka",
                      "chain...\nthe answer: unclear\nANSWER: Osaka", "**ANSWER: Osaka**"):
            self.assertEqual(_raw_answer(reply), "Osaka", reply)
        self.assertEqual(_raw_answer("ANSWER: 's-Hertogenbosch"), "'s-Hertogenbosch")

    def test_capitalised_code_tag(self):
        self.assertEqual(extract_code("```Python\nprint(42)\n```"), "print(42)")


if __name__ == "__main__":
    unittest.main()

"""Function names cannot establish permutation invariance of their arguments."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness import context as ctx
from harness.solver import Solver


class OrderSensitiveTaskTests(unittest.TestCase):
    CASES = (
        ("maximum_profit", """\
            def maximum_profit(prices):
                best = 0
                for i, buy in enumerate(prices):
                    for sell in prices[i + 1:]:
                        best = max(best, sell - buy)
                return best
            """, [1, 2, 3], 2, [3, 2, 1], 0),
        ("total_distance", """\
            def total_distance(points):
                return sum(abs(b - a) for a, b in zip(points, points[1:]))
            """, [0, 1, 3], 3, [0, 3, 1], 5),
        ("count_switches", """\
            def count_switches(values):
                return sum(a != b for a, b in zip(values, values[1:]))
            """, [0, 1, 0], 2, [0, 0, 1], 1),
    )

    def fixture(self, root, case, broken=False):
        name, source, a, va, b, vb = case
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "mylib.py").write_text(
            "def %s(values):\n    return 0\n" % name if broken else textwrap.dedent(source))
        tests = ("import unittest\nimport mylib as m\nclass TestTask(unittest.TestCase):\n"
                 "    def test_%s_a(self):\n        self.assertEqual(m.%s(%r), %r)\n"
                 "    def test_%s_b(self):\n        self.assertEqual(m.%s(%r), %r)\n"
                 "if __name__ == '__main__':\n    unittest.main()\n"
                 % (name, name, a, va, name, name, b, vb))
        (workspace / "test_mylib.py").write_text(tests)
        task = ctx.Task(str(root), {"task_type": "coding", "instruction": "Fix the function. Write the word DONE."})
        return task, tests

    def test_valid_order_sensitive_tasks_are_not_rejected(self):
        for case in self.CASES:
            with self.subTest(function=case[0]), tempfile.TemporaryDirectory() as tmp:
                task, tests = self.fixture(Path(tmp), case)
                impure = ctx.impure_functions([textwrap.dedent(case[1])])
                self.assertEqual(ctx.test_contradictions(tests, impure), [])
                info = Solver(None).prepare(task)
                self.assertTrue(info["initial_pass"], info["test_out"])
                self.assertEqual(info["problems"], [])

    def test_broken_order_sensitive_task_can_reach_the_model_and_be_repaired(self):
        case = self.CASES[0]
        calls = []

        def ask(*args, **kwargs):
            calls.append(args)
            return "```python\n%s\n```" % textwrap.dedent(case[1])

        with tempfile.TemporaryDirectory() as tmp:
            task, _ = self.fixture(Path(tmp), case, broken=True)
            solver = Solver(ask)
            info = solver.prepare(task)
            self.assertFalse(info["initial_pass"])
            self.assertEqual(info["problems"], [])
            outcome = solver.solve(task, info, 0)
            self.assertEqual(outcome.status, "solved", outcome.note)
            self.assertEqual(outcome.answer, "DONE")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

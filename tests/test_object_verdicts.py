"""Offline tests for the "unchanged object" verdict, skipped tests and the data KeyError retry.

Run with: python3 -m unittest discover -s tests -v

m.f(<literals>) of the task's module required to equal two values is a contradiction when f
is proven pure; a method call on an instance never gives this verdict (queues, iterators,
defaultdict reads and dunder counters change state in ways the source analysis missed). Skipped
tests say nothing. A KeyError on a name from a data question gets one retry with the columns.
"""
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness import context as ctx
from harness.solver import Solver


def verdicts(src, test):
    src, test = textwrap.dedent(src), textwrap.dedent(test)
    return ctx.test_contradictions(test, ctx.impure_functions([src]) | ctx.unproven_functions([src]))


TWO_CALLS = """
import unittest
from mylib import {cls}
class T(unittest.TestCase):
    def test_a(self):
        o = {make}
        {prep}
        self.assertEqual(o.{meth}(), 1)
        self.assertEqual(o.{meth}(), 2)
"""


class ObjectVerdictTests(unittest.TestCase):
    def case(self, src, cls, make, meth, prep="pass"):
        return verdicts(src, TWO_CALLS.format(cls=cls, make=make, meth=meth, prep=prep))

    def test_instance_methods_give_no_verdict(self):
        deque_queue = """
        from collections import deque
        class Queue:
            def __init__(self): self._items = deque()
            def enqueue(self, x): self._items.append(x)
            def dequeue(self): return self._items.popleft()
        """
        self.assertEqual(self.case(deque_queue, "Queue", "Queue()", "dequeue", "o.enqueue(1); o.enqueue(2)"), [])
        self.assertEqual(self.case("class Stack(list):\n    def push(self, x): self.append(x)\n",
                                   "Stack", "Stack()", "pop", "o.push(2); o.push(1)"), [])
        dice = """
        import random
        class Dice:
            def __init__(self, seed): self.rng = random.Random(seed)
            def roll(self): return self.rng.randint(1, 6)
        """
        self.assertEqual(self.case(dice, "Dice", "Dice(7)", "roll"), [])
        it = """
        class Reader:
            def __init__(self, xs): self.it = iter(xs)
            def peek_sum(self): return sum(self.it)
        """
        self.assertEqual(self.case(it, "Reader", "Reader([1, 2])", "peek_sum"), [])
        # even a method that is pure: the verdict is left to the model
        cart = """
        class Cart:
            def __init__(self): self.items = {}
            def total(self): return sum(q * p for q, p in self.items.values())
        """
        self.assertEqual(self.case(cart, "Cart", "Cart()", "total"), [])

    def test_module_function_verdict_is_kept(self):
        src = "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n"
        test = ("import unittest\nimport mylib as m\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                "        self.assertEqual(m.clamp(15, 0, 10), 10)\n        self.assertEqual(m.clamp(15, 0, 10), 11)\n")
        self.assertTrue(any("unchanged object" in v for v in verdicts(src, test)))

    def test_skipped_tests_are_left_out(self):
        src = "def f(x):\n    return x * 2\n"
        for deco in ("@unittest.skip('old')", "@unittest.expectedFailure", "@pytest.mark.xfail"):
            test = ("import unittest, pytest\nimport mylib as m\nclass T(unittest.TestCase):\n"
                    "    def test_new(self):\n        self.assertEqual(m.f(2), 4)\n"
                    "    %s\n    def test_old(self):\n        self.assertEqual(m.f(2), 5)\n" % deco)
            self.assertEqual(verdicts(src, test), [], deco)
        test = ("import unittest\nimport mylib as m\nclass T(unittest.TestCase):\n"
                "    def test_new(self):\n        self.assertEqual(m.f(2), 4)\n"
                "    def test_old(self):\n        self.skipTest('old')\n        self.assertEqual(m.f(2), 5)\n")
        self.assertEqual(verdicts(src, test), [])


class KeyErrorRetryTests(unittest.TestCase):
    def run_task(self, replies):
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(msgs[-1]["content"])
            return replies[len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspace").mkdir()
            (root / "workspace" / "orders.csv").write_text("order,shipping_fee\n1,5\n2,7\n")
            task = ctx.Task(str(root), {"task_type": "data_analysis",
                                        "instruction": "What is the total shipping cost of all orders?"})
            solver = Solver(ask)
            info = solver.prepare(task)
            return solver.solve(task, info, 0), calls

    WRONG = "```python\nimport pandas as pd\ndf = pd.read_csv('orders.csv')\nprint(df['shipping cost'].sum())\n```"
    RIGHT = "```python\nimport pandas as pd\ndf = pd.read_csv('orders.csv')\nprint(df['shipping_fee'].sum())\n```"

    def test_misspelled_column_is_retried(self):
        out, calls = self.run_task([self.WRONG, self.RIGHT])
        self.assertEqual((out.status, out.answer), ("solved", "12"))
        self.assertIn("shipping_fee", calls[1])

    def test_missing_column_twice_is_infeasible(self):
        out, calls = self.run_task([self.WRONG, self.WRONG])
        self.assertEqual(out.status, "infeasible")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

"""Offline regression tests for the answers and verdicts given without the model.

Run with: python3 -m unittest discover -s tests -v

The rule they guard: a task is answered or declared infeasible for free only when its statement
is understood completely; an unknown wording always goes to the model (None), never to a free
"infeasible".
"""
import json
import os
import tempfile
import unittest

from harness import context as ctx
from harness import fastpath


def make_task(instruction, files=None):
    d = tempfile.mkdtemp(prefix="fv_")
    os.makedirs(os.path.join(d, "workspace"))
    for rel, text in (files or {}).items():
        p = os.path.join(d, "workspace", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    data = {"task_id": "t", "task_type": "test", "instruction": instruction, "reward": 1}
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump(data, f)
    return ctx.Task(d, data)


WAREHOUSE = "A warehouse starts with 1000 boxes. %s How many boxes does it have at the end?"
KB = "The directory kb/ contains a small knowledge base of markdown files. "
CARDS = {
    "kb/alice.md": "# Alice Wang\n\nBorn: 1980\nEmployer: Cobalt Works\nMentor: Boris Ivanov\n",
    "kb/boris.md": "# Boris Ivanov\n\nBorn: 1955\nEmployer: Delta Foods\n",
    "kb/cobalt.md": "# Cobalt Works\n\nHeadquarters: Lima\nFounded: 1999\n",
    "kb/delta.md": "# Delta Foods\n\nHeadquarters: Graz\nFounded: 1971\n",
}


class WarehouseTests(unittest.TestCase):
    def solve(self, middle):
        return fastpath.solve(make_task(WAREHOUSE % middle), "math")

    def test_exact_operations(self):
        self.assertEqual(self.solve("It ships 40 boxes per day for 5 days."), ("solved", "800"))
        self.assertEqual(self.solve("Then the stock is tripled by merging with two sister warehouses."),
                         ("solved", "3000"))

    def test_unstated_quantity_is_infeasible(self):
        self.assertEqual(self.solve("It receives a few deliveries of 40 boxes each.")[0], "infeasible")
        self.assertEqual(self.solve("It ships 30 boxes per day until the stock is low.")[0], "infeasible")

    def test_contradicting_stated_stock_is_infeasible(self):
        self.assertEqual(self.solve("It ships 10 boxes. After that it has exactly 500 boxes.")[0], "infeasible")

    def test_unknown_wording_goes_to_the_model(self):
        for middle in ("It sells 5 boxes to a customer who returns them.",
                       "It ships 30 boxes per day until it runs out.",
                       "Then the stock is merged with two sister warehouses."):
            with self.subTest(middle=middle):
                self.assertIsNone(self.solve(middle))


class CardTests(unittest.TestCase):
    def solve(self, question, cards=CARDS):
        return fastpath.solve(make_task(KB + question, cards), "search")

    def test_chains(self):
        self.assertEqual(self.solve("In which year was Alice Wang's mentor born?"), ("solved", "1955"))
        self.assertEqual(self.solve("Where is the company that employs the mentor of Alice Wang headquartered?"),
                         ("solved", "Graz"))

    def test_absent_entity_and_missing_field_are_infeasible(self):
        self.assertEqual(self.solve("In which year was Olga Sato born?")[0], "infeasible")
        self.assertEqual(self.solve("Who is the mentor of Boris Ivanov?")[0], "infeasible")
        self.assertEqual(self.solve("What is the email address of Alice Wang?")[0], "infeasible")

    def test_paraphrases_are_never_declared_infeasible(self):
        for q in ("What is the birth year of Alice Wang?", "What is the year of birth of Alice Wang?",
                  "When was Alice Wang born?", "What is the favourite colour of Alice Wang?"):
            with self.subTest(q=q):
                out = self.solve(q)
                self.assertTrue(out is None or out == ("solved", "1980"), out)

    def test_a_field_value_with_more_than_a_bare_fact_goes_to_the_model(self):
        cards = dict(CARDS, **{"kb/alice.md": "# Alice Wang\n\nBorn: 1980 in Lima\nEmployer: Cobalt Works\n"})
        self.assertIsNone(self.solve("In which city was Alice Wang born?", cards))


class ContradictionTests(unittest.TestCase):
    HEAD = "import unittest\nimport mylib as m\n\n\nclass T(unittest.TestCase):\n"

    def problems(self, body):
        return ctx.test_contradictions(self.HEAD + body)

    def test_same_call_two_values(self):
        self.assertTrue(self.problems("    def test_a(self):\n        self.assertEqual(m.total([1, 2]), 3)\n"
                                      "    def test_b(self):\n        self.assertEqual(m.total([1, 2]), 4)\n"))

    def test_return_and_raise(self):
        self.assertTrue(self.problems("    def test_a(self):\n        self.assertEqual(m.total([]), 0)\n"
                                      "    def test_b(self):\n        self.assertRaises(ValueError, m.total, [])\n"))

    def test_different_argument_orders_need_the_model(self):
        # The function's name alone does not prove that argument order is irrelevant.
        self.assertEqual(self.problems("    def test_a(self):\n        self.assertEqual(m.total([1, 2, 3]), 6)\n"
                                       "    def test_b(self):\n        self.assertEqual(m.total([3, 2, 1]), 5)\n"), [])

    def test_consistent_tests_are_left_alone(self):
        self.assertEqual(self.problems(
            "    def test_a(self):\n        self.assertEqual(m.total([1, 2, 3]), 6)\n"
            "    def test_b(self):\n        self.assertEqual(m.first_n([3, 2, 1], 1), [3])\n"
            "    def test_c(self):\n        self.assertRaises(TypeError, m.total, None)\n"), [])


if __name__ == "__main__":
    unittest.main()

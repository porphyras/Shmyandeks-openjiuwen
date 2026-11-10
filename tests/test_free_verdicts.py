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

    def test_true_and_false(self):
        self.assertTrue(self.problems("    def test_a(self):\n        self.assertTrue(m.is_even(4))\n"
                                      "    def test_b(self):\n        self.assertFalse(m.is_even(4))\n"))

    def test_builtin_over_a_call(self):
        self.assertTrue(self.problems("    def test_a(self):\n        self.assertEqual(len(m.split('a b c')), 3)\n"
                                      "    def test_b(self):\n        self.assertEqual(len(m.split('a b c')), 4)\n"))

    def test_result_kept_in_a_variable(self):
        self.assertTrue(self.problems("    def test_a(self):\n        r = m.square(3)\n        self.assertEqual(r, 9)\n"
                                      "    def test_b(self):\n        self.assertEqual(m.square(3), 10)\n"))

    def test_a_changed_variable_is_not_the_call(self):
        self.assertEqual(self.problems(
            "    def test_a(self):\n        r = m.items(3)\n        r.append(1)\n        self.assertEqual(len(r), 4)\n"
            "    def test_b(self):\n        self.assertEqual(len(m.items(3)), 3)\n"), [])
        self.assertEqual(self.problems(
            "    def test_a(self):\n        r = m.items(3)\n        self.assertEqual(r.pop(), 3)\n"
            "        self.assertEqual(len(r), 2)\n"
            "    def test_b(self):\n        self.assertEqual(len(m.items(3)), 3)\n"), [])
        self.assertEqual(self.problems("    def test_a(self):\n        self.assertTrue(m.count(4))\n"
                                       "    def test_b(self):\n        self.assertEqual(m.count(4), 1)\n"), [])

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


class FactInAnotherFieldTests(unittest.TestCase):
    """A "Key: sentence" line is parsed as a field; the fact it states must not be declared absent."""

    def solve(self, question, extra):
        files = dict(CARDS)
        files.update(extra)
        return fastpath.solve(make_task(KB + question, files), "search")

    def test_fact_in_a_note_is_not_absent(self):
        dana = {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\nNote: Dana Kim is mentored by Boris Ivanov\n"}
        for q in ("Who is Dana Kim's mentor?", "In which year was Dana Kim's mentor born?"):
            self.assertIsNone(self.solve(q, dana))

    def test_fact_on_another_card_is_not_absent(self):
        extra = {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\n",
                 "kb/carla.md": "# Carla Diaz\n\nBorn: 1960\nNote: Carla Diaz mentors Dana Kim\n"}
        self.assertIsNone(self.solve("Who is Dana Kim's mentor?", extra))
        title_case = {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\nNote: Mentored By Boris Ivanov\n"}
        self.assertIsNone(self.solve("Who is Dana Kim's mentor?", title_case))

    def test_birth_role_and_location_in_sentences(self):
        self.assertIsNone(self.solve("In which year was Dana Kim born?",
                                     {"kb/dana.md": "# Dana Kim\n\nDetails: born in 1990 in Lima\n"}))
        self.assertIsNone(self.solve("Which company does Dana Kim work for?",
                                     {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\nRole: engineer at Delta Foods\n"}))
        self.assertIsNone(self.solve("How many people work for Delta Foods?",
                                     {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\nRole: engineer at Delta Foods\n"}))

    def test_bare_base_keeps_its_verdict(self):
        verdict = self.solve("Who is Dana Kim's mentor?", {"kb/dana.md": "# Dana Kim\n\nBorn: 1990\n"})
        self.assertEqual(verdict[0], "infeasible")


class UnknownCardKeyTests(unittest.TestCase):
    """A base with a label outside the known schema may state the asked fact under that label."""

    def solve(self, question, card):
        files = dict(CARDS)
        files["kb/dana.md"] = card
        return fastpath.solve(make_task(KB + question, files), "search")

    def test_synonym_keys_are_not_absence(self):
        cases = [("In which year was Dana Kim born?", "# Dana Kim\n\nDOB: 1980\n"),
                 ("Which company does Dana Kim work for?", "# Dana Kim\n\nFirm: Cobalt Works\n"),
                 ("Who is Dana Kim's mentor?", "# Dana Kim\n\nSupervisor: Boris Ivanov\n"),
                 ("How many people work for Cobalt Works?", "# Dana Kim\n\nFirm: Cobalt Works\n"),
                 ("What is Dana Kim's job title?", "# Dana Kim\n\nOccupation: engineer\n")]
        for question, card in cases:
            self.assertIsNone(self.solve(question, card), question)

    def test_known_schema_keeps_its_verdict(self):
        self.assertEqual(self.solve("Who is Dana Kim's mentor?", "# Dana Kim\n\nBorn: 1990\nRole: analyst\n")[0],
                         "infeasible")

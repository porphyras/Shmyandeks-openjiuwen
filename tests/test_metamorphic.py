"""Seeded input transformations, with expected answers independent of the parser.

Failures include the seed/case so a counterexample can be reproduced exactly.
"""
import csv
import random
import tempfile
import unittest
from pathlib import Path

from harness.context import Task
from harness import fastpath

SEED = 20261108
CASES = 100


class MetamorphicTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "workspace").mkdir()
        self.rng = random.Random(SEED)

    def solve(self, kind, instruction):
        task = Task(str(self.root), {"instruction": instruction, "task_type": kind})
        return fastpath.solve(task, kind)

    def stock(self, initial, operations):
        return self.solve("math", "A warehouse starts with %d boxes. %s How many boxes does it have at the end?"
                          % (initial, operations))

    def test_shipping_expansion_and_receipt_cancellation(self):
        for case in range(CASES):
            initial = self.rng.randrange(1000, 100000)
            rate, days = self.rng.randrange(1, 30), self.rng.randrange(1, 20)
            total = rate * days
            with self.subTest(seed=SEED, case=case):
                daily = self.stock(initial, "It ships %d boxes per day for %d days." % (rate, days))
                expanded = self.stock(initial, "It ships %d boxes." % total)
                self.assertEqual(daily, ("solved", str(initial - total)))
                self.assertEqual(expanded, daily)
                restored = self.stock(initial, "It ships %d boxes. It receives 1 delivery of %d boxes each."
                                      % (total, total))
                self.assertEqual(restored, ("solved", str(initial)))

    def test_rounding_boundaries_and_unknown_suffixes(self):
        for case in range(CASES):
            divisor = self.rng.randrange(2, 30)
            quotient = self.rng.randrange(1, 100)
            op = "Then the stock is split equally into %d warehouses and it keeps one share (rounded down)." % divisor
            with self.subTest(seed=SEED, case=case):
                # Adjacent stocks straddling an integer boundary must not collapse.
                self.assertEqual(self.stock(quotient * divisor - 1, op), ("solved", str(quotient - 1)))
                self.assertEqual(self.stock(quotient * divisor, op), ("solved", str(quotient)))
                # Understanding an earlier sentence does not justify dropping a later one.
                self.assertIsNone(self.stock(quotient * divisor, op + " It reserves some boxes for tomorrow."))

    def write_sales(self, rows):
        with (self.root / "workspace" / "sales.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["order_id", "region", "product", "month", "units", "price"])
            writer.writerows(rows)

    def sales(self, rows, question):
        self.write_sales(rows)
        return self.solve("data", "The file sales.csv has columns order_id,region,product,month,units,price (%d rows). %s"
                          % (len(rows), question))

    def test_table_permutation_duplication_and_irrelevant_rows(self):
        question = "What is the total revenue (sum of units * price) for region 'East'?"
        for case in range(CASES):
            rows = [[i, "East" if i % 2 else "West", "Widget", self.rng.randrange(1, 13),
                     self.rng.randrange(1, 100), self.rng.randrange(1, 100)] for i in range(20)]
            expected = sum(row[4] * row[5] for row in rows if row[1] == "East")
            with self.subTest(seed=SEED, case=case):
                self.assertEqual(self.sales(rows, question), ("solved", str(expected)))
                shuffled = list(rows)
                self.rng.shuffle(shuffled)
                self.assertEqual(self.sales(shuffled, question), ("solved", str(expected)))
                doubled = rows + [[r[0] + 20] + r[1:] for r in rows]
                self.assertEqual(self.sales(doubled, question), ("solved", str(2 * expected)))
                irrelevant = rows + [[100, "West", "Other", 1, 999999, 999999]]
                self.assertEqual(self.sales(irrelevant, question), ("solved", str(expected)))

    def test_unstated_rounding_ties_fall_back(self):
        question = "What is the average price of product 'Widget', rounded to the nearest integer?"
        for case in range(CASES):
            price = self.rng.randrange(1, 1000)
            rows = [[1, "East", "Widget", 1, 1, price], [2, "East", "Widget", 1, 1, price + 1]]
            with self.subTest(seed=SEED, case=case):
                self.assertIsNone(self.sales(rows, question))

    def write_cards(self, cards):
        kb = self.root / "workspace" / "kb"
        kb.mkdir(exist_ok=True)
        for p in kb.iterdir():
            p.unlink()
        names = list(range(len(cards)))
        self.rng.shuffle(names)
        for index, text in zip(names, cards):
            (kb / ("card_%03d.md" % index)).write_text(text, encoding="utf-8")

    def test_kb_file_order_distractors_and_counterfactual_fact(self):
        prefix = "The directory kb/ contains a small knowledge base of markdown files. "
        for case in range(CASES):
            hops = self.rng.randrange(1, 7)
            people = ["Person Name%d" % i for i in range(hops + 1)]
            cards = ["# %s\nMentor: %s\n" % pair for pair in zip(people, people[1:])]
            cards += ["# %s\nEmployer: Cobalt Works\n" % people[-1],
                      "# Cobalt Works\nHeadquarters: Graz\n"]
            subject = "the mentor of " * hops + people[0]
            question = prefix + "Where is the company that employs %s headquartered?" % subject
            with self.subTest(seed=SEED, case=case, hops=hops):
                self.write_cards(cards)
                self.assertEqual(self.solve("search", question), ("solved", "Graz"))
                distractors = ["# Person Name99\nEmployer: Other Works\n",
                               "# Other Works\nHeadquarters: Oslo\n"]
                self.write_cards(cards + distractors)
                self.assertEqual(self.solve("search", question), ("solved", "Graz"))
                changed = [text.replace("Headquarters: Graz", "Headquarters: Lima") for text in cards]
                self.write_cards(changed + distractors)
                self.assertEqual(self.solve("search", question), ("solved", "Lima"))


if __name__ == "__main__":
    unittest.main()

"""Offline tests: 'mentee' as the inverse of 'mentor' at any depth of a relation chain.

Run with: python3 -m unittest discover -s tests -v
"""
import unittest

from harness import fastpath
from test_free_verdicts import KB, make_task

CARDS = {
    "kb/a.md": "# Alice Wang\n\nBorn: 1950\nEmployer: Cobalt Works\n",
    "kb/b.md": "# Boris Ivanov\n\nBorn: 1970\nEmployer: Delta Foods\nMentor: Alice Wang\n",
    "kb/c.md": "# Carla Diaz\n\nBorn: 1990\nEmployer: Delta Foods\nMentor: Boris Ivanov\n",
    "kb/d.md": "# Delta Foods\n\nHeadquarters: Graz\nFounded: 1971\n",
    "kb/e.md": "# Cobalt Works\n\nHeadquarters: Lima\nFounded: 1999\n",
}


class MenteeChainTests(unittest.TestCase):
    def solve(self, question, cards=CARDS):
        return fastpath.solve(make_task(KB + question, cards), "search")

    def test_two_inverse_steps(self):
        self.assertEqual(self.solve("In which year was Alice Wang's mentee's mentee born?"), ("solved", "1990"))
        self.assertEqual(self.solve("Who is mentored by the mentee of Alice Wang?"), ("solved", "Carla Diaz"))
        self.assertEqual(self.solve("Where is the headquarters of the employer of Alice Wang's mentee's mentee?"),
                         ("solved", "Graz"))

    def test_back_to_the_mentor(self):
        self.assertEqual(self.solve("Who is Alice Wang's mentee's mentor?"), ("solved", "Alice Wang"))

    def test_two_candidates_go_to_the_model(self):
        cards = dict(CARDS, **{"kb/f.md": "# Fedor Sato\n\nBorn: 1975\nEmployer: Cobalt Works\nMentor: Alice Wang\n"})
        self.assertIsNone(self.solve("In which year was Alice Wang's mentee's mentee born?", cards))


if __name__ == "__main__":
    unittest.main()

"""Inverse relations: independent graph oracles and near-miss refusal controls."""
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import fastpath
from harness.context import Task

SEED = 20261109


class InverseSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "workspace"
        self.kb = self.workspace / "kb"
        self.kb.mkdir(parents=True)

    def write_cards(self, cards):
        for path in self.kb.iterdir():
            path.unlink()
        for index, text in enumerate(cards):
            (self.kb / ("card_%03d.md" % index)).write_text(text, encoding="utf-8")

    def solve(self, question):
        task = Task(str(self.root), {"task_type": "search",
                                    "instruction": fastpath._SEARCH_PRE + " " + question})
        return fastpath.solve(task, "search")

    def base(self):
        return ["# Alice Wang\nMentor: Boris Ivanov\nBorn: 1980\nEmployer: Cobalt Works\n",
                "# Boris Ivanov\nMentor: Carla Diaz\nBorn: 1960\n",
                "# Carla Diaz\nBorn: 1940\n",
                "# Cobalt Works\nFounded: 2001\nHeadquarters: Graz\n"]

    def test_direction_and_mixed_chains(self):
        self.write_cards(self.base())
        for question, expected in [
            ("Who is mentored by Boris Ivanov?", "Alice Wang"),
            ("Who is the person mentored by Boris Ivanov?", "Alice Wang"),
            ("Who mentors Boris Ivanov?", "Carla Diaz"),
            ("Who is mentored by the person mentored by Carla Diaz?", "Alice Wang"),
            ("Who is the person mentored by Alice Wang's mentor?", "Alice Wang"),
            ("Who is the mentor of the person mentored by Boris Ivanov?", "Boris Ivanov"),
            ("In which year was the person mentored by Boris Ivanov born?", "1980"),
            ("Where is the company that employs the person mentored by Boris Ivanov headquartered?", "Graz"),
            ("In which year was the employer of the person mentored by Boris Ivanov founded?", "2001"),
        ]:
            with self.subTest(question=question):
                self.assertEqual(self.solve(question), ("solved", expected))

    def test_reference_without_own_card_and_identical_duplicates(self):
        self.write_cards([self.base()[0], self.base()[0]])
        self.assertEqual(self.solve("Who is mentored by Boris Ivanov?"), ("solved", "Alice Wang"))

    def test_supported_prose_is_also_checked(self):
        self.write_cards(["# Alice Wang\nAlice Wang is mentored by Boris Ivanov. Alice Wang was born in 1980."])
        self.assertEqual(self.solve("Who is mentored by Boris Ivanov?"), ("solved", "Alice Wang"))

    def test_zero_or_multiple_matches_never_answer_or_declare_infeasible(self):
        self.write_cards(self.base())
        self.assertIsNone(self.solve("Who is mentored by Alice Wang?"))
        self.assertIsNone(self.solve("Who is mentored by Unknown Person?"))
        self.write_cards(self.base() + ["# Daniel Young\nMentor: Boris Ivanov\nBorn: 1980\n"])
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))
        # Equal final years cannot justify an ambiguous intermediate person.
        self.assertIsNone(self.solve("In which year was the person mentored by Boris Ivanov born?"))

    def test_prefix_scope_keeps_ambiguity(self):
        self.write_cards(self.base() + ["# Daniel Young\nMentor: Boris Ivanov\n",
                                       "# Elena Rossi\nMentor: Alice Wang\n"])
        # inverse(mentor(Alice)) is ambiguous; mentor(inverse(Alice)) would return Alice.
        self.assertIsNone(self.solve("Who is the person mentored by Alice Wang's mentor?"))

    def test_uncertain_data_falls_back(self):
        for extra in [
            "# Daniel Young\nMentor: Boris Alexei Ivanov\n",  # possible alias
            "# Daniel Young\nMentor: Ivanov Boris\n",  # reordered name
            "# Daniel Young\nMentor: Bóris Ivanov\n",  # accent variant
            "# Boris Alexei Ivanov\nBorn: 1960\n",  # ambiguous subject
            "# Alice Wang\nMentor: Carla Diaz\n",  # conflicting duplicate
            "# Daniel Young\nCoach: Boris Ivanov\n",  # unsupported relation label
            "# Daniel Young\nMentor: Boris Ivanov (formerly)\n",  # qualification
            "# Daniel Young\nDaniel Young sometimes trains with Boris Ivanov.\n",
            "# Daniel Young\nMentor: Boris Ivanov\nMentor: Carla Diaz\n",
        ]:
            with self.subTest(extra=extra):
                self.write_cards(self.base() + [extra])
                self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))

    def test_missing_later_attribute_falls_back(self):
        self.write_cards(["# Alice Wang\nMentor: Boris Ivanov\n"])
        self.assertIsNone(self.solve("In which year was the person mentored by Boris Ivanov born?"))

    def test_unknown_question_parts_are_not_dropped(self):
        self.write_cards(self.base())
        for question in [
            "Who was formerly mentored by Boris Ivanov?",
            "Who is mentored by Boris Ivanov other than Alice Wang?",
            "Who is mentored by Boris Ivanov and works in Oslo?",
            "Who is mentored by Boris Ivanov? Exclude Alice Wang.",
            "Who is the youngest person mentored by Boris Ivanov?",
            "Who are the people mentored by Boris Ivanov?",
            "Who is not mentored by Boris Ivanov?",
        ]:
            with self.subTest(question=question):
                self.assertIsNone(self.solve(question))

    def test_truncated_unreadable_or_unsupported_documents_fall_back(self):
        self.write_cards(self.base())
        path = self.kb / "extra.md"
        path.write_text("# Daniel Young\n" + "\n" * 65536 + "Mentor: Boris Ivanov\n")
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))
        path.write_bytes(b"# Daniel Young\n\xffMentor: Boris Ivanov\n")
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))
        path.unlink()
        (self.kb / "extra.csv").write_text("person,mentor\nDaniel Young,Boris Ivanov\n")
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))

    def test_base_size_and_file_count_limits(self):
        self.write_cards(self.base())
        # Valid small card at EOF, padded below the per-file limit, beyond total limit.
        card = "# Distractor Person\nBorn: 1970\n" + "\n" * 64000
        for index in range(33):
            (self.kb / ("extra_%02d.md" % index)).write_text(card)
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))
        self.write_cards(self.base() + ["# Distractor Person\nBorn: 1970\n"] * 997)
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))

    def test_skipped_subtrees_and_read_errors_cannot_prove_uniqueness(self):
        self.write_cards(self.base())
        extra = self.root / "extra"
        extra.mkdir()
        (extra / "second.md").write_text("# Daniel Young\nMentor: Boris Ivanov\n")
        link = self.kb / "linked"
        link.symlink_to(extra, target_is_directory=True)
        self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))
        link.unlink()
        with patch("builtins.open", side_effect=PermissionError("unreadable card")):
            self.assertIsNone(self.solve("Who is mentored by Boris Ivanov?"))

    def test_generated_graphs_with_independent_oracle(self):
        rng = random.Random(SEED)
        for case in range(100):
            people = ["Person Name%d" % i for i in range(12)]
            targets = list(range(len(people)))
            rng.shuffle(targets)  # a permutation: exactly one incoming edge per person
            years = rng.sample(range(1900, 2000), len(people))
            cards = ["# %s\nMentor: %s\nBorn: %d\n" % (person, people[targets[i]], years[i])
                     for i, person in enumerate(people)]
            start = rng.randrange(len(people))
            current, subject = start, people[start]
            # Expected result comes from integer graph indices, never the task parser.
            directions = ["inverse"] + [rng.choice(["forward", "inverse"]) for _ in range(rng.randrange(1, 6))]
            for direction in directions:
                if direction == "forward":
                    current = targets[current]
                    subject = "the mentor of " + subject
                else:
                    current = targets.index(current)
                    subject = "the person mentored by " + subject
            question = "In which year was %s born?" % subject
            with self.subTest(seed=SEED, case=case, directions=directions):
                self.write_cards(cards)
                self.assertEqual(self.solve(question), ("solved", str(years[current])))
                rng.shuffle(cards)
                self.write_cards(cards + ["# Other Person\nMentor: Unknown Person\nBorn: 1800\n"])
                self.assertEqual(self.solve(question), ("solved", str(years[current])))
                changed = [card.replace("Born: %d\n" % years[current], "Born: 2099\n") for card in cards]
                self.write_cards(changed)
                self.assertEqual(self.solve(question), ("solved", "2099"))
                # A second incoming edge on the first inverse hop makes the whole chain unclear.
                self.write_cards(cards + ["# Extra Person\nMentor: %s\nBorn: 2099\n" % people[start]])
                self.assertIsNone(self.solve(question))


if __name__ == "__main__":
    unittest.main()

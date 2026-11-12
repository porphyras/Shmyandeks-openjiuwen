"""Offline tests: refusals that should first try the model once more (no network).

Run with: python3 -m unittest discover -s tests -v

After NOT FOUND a different knowledge-base excerpt gets the second look even when it is
shorter; a math program dividing by zero gets one retry before "infeasible"; a card whose
heading line is followed by its facts without a blank line ("# Alice\\nBorn: 1980") is kept.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import context as ctx
from harness.solver import Solver


class ShorterSecondLookTests(unittest.TestCase):
    def test_different_shorter_excerpt_gets_the_second_look(self):
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(msgs[-1]["content"])
            return "ANSWER: Graz" if "headquartered in Graz" in msgs[-1]["content"] else "ANSWER: NOT FOUND"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspace" / "kb").mkdir(parents=True)
            (root / "workspace" / "kb" / "a.md").write_text("Alice Wang works at Cobalt Works.\n")
            task = ctx.Task(str(root), {"task_type": "search",
                                        "instruction": "In which city is the employer of Alice Wang headquartered?"})
            first = "[people] " + "Unrelated person record. " * 600  # long, no answer
            second = "[a] Alice Wang works at Cobalt Works.\n[b] Cobalt Works is headquartered in Graz."
            info = {"kind": "search", "ctx": first, "kb_cards": True, "kb_complete": False, "ndocs": 2,
                    "problems": []}
            with patch.object(ctx, "kb_fallback_text", return_value=second):
                out = Solver(ask).solve(task, info, 0)
        self.assertEqual((out.status, out.answer), ("solved", "Graz"))
        self.assertEqual(len(calls), 2)


class ZeroDivisionRetryTests(unittest.TestCase):
    def test_math_division_by_zero_is_retried_once(self):
        replies = ["```python\nprint(42 / (6 - 6))\n```", "```python\nprint(6 * 7)\n```"]
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(1)
            return replies[len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            task = ctx.Task(tmp, {"task_type": "math", "instruction": "Compute six times seven."})
            solver = Solver(ask)
            out = solver.solve(task, solver.prepare(task), 0)
        self.assertEqual((out.status, out.answer, len(calls)), ("solved", "42", 2))

    def test_division_by_zero_twice_is_infeasible(self):
        def ask(task, msgs, level, max_tokens, temperature=0.0):
            return "```python\nprint(1 / 0)\n```"

        with tempfile.TemporaryDirectory() as tmp:
            task = ctx.Task(tmp, {"task_type": "math", "instruction": "Split 12 apples among 0 children."})
            solver = Solver(ask)
            out = solver.solve(task, solver.prepare(task), 0)
        self.assertEqual(out.status, "infeasible")


class HeadingCardTests(unittest.TestCase):
    def test_card_without_blank_line_after_heading_is_kept(self):
        docs = [("kb/alice.md", "# Alice\nBorn: 1980\n\n# Notes\n\nShe moved to Graz in 2001.")]
        texts = [p for _, p in ctx._passages(docs)]
        self.assertIn("# Alice\nBorn: 1980", texts)
        self.assertNotIn("# Notes", texts)
        part = ctx.entity_passages(docs, "When was Alice born?", ["Alice"])
        self.assertIn("Born: 1980", part)


if __name__ == "__main__":
    unittest.main()

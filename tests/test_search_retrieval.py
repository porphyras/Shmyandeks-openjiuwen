"""A different retrieval can contain the answer even when it is shorter."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from harness.solver import Solver


class SearchRetrievalTests(unittest.TestCase):
    QUESTION = "Where is the employer of Alice Wang headquartered?"
    FACT = "# Alice Wang\nEmployer: Cobalt Works\n# Cobalt Works\nHeadquarters: Graz\n"

    def solve_with(self, first, fallback):
        prompts = []

        def ask(task, messages, *args, **kwargs):
            prompt = messages[-1]["content"]
            prompts.append(prompt)
            return "ANSWER: Graz" if "Headquarters: Graz" in prompt else "NOT FOUND"

        task = SimpleNamespace(short_instruction=lambda: self.QUESTION)
        with patch("harness.context.kb_fallback_text", return_value=fallback) as retrieve:
            result = Solver(ask)._solve_search(task, {"ctx": first, "kb_cards": False, "kb_complete": False}, 0)
        return result, prompts, retrieve

    def test_different_retrieval_is_used_regardless_of_length(self):
        for size in (len(self.FACT) - 1, len(self.FACT), len(self.FACT) + 1000):
            with self.subTest(first_size=size, fallback_size=len(self.FACT)):
                result, prompts, _ = self.solve_with("x" * size, self.FACT)
                self.assertEqual((result.status, result.answer), ("solved", "Graz"))
                self.assertEqual(len(prompts), 2)

    def test_identical_or_empty_retrieval_does_not_repeat_a_refusal(self):
        for fallback in ("", "No relevant facts."):
            with self.subTest(fallback=fallback):
                result, prompts, _ = self.solve_with("No relevant facts.", fallback)
                self.assertEqual(result.status, "infeasible")
                self.assertEqual(len(prompts), 1)

    def test_first_success_does_not_retrieve_or_call_again(self):
        result, prompts, retrieve = self.solve_with(self.FACT, "unused")
        self.assertEqual((result.status, result.answer), ("solved", "Graz"))
        self.assertEqual(len(prompts), 1)
        retrieve.assert_not_called()


if __name__ == "__main__":
    unittest.main()

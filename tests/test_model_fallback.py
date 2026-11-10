"""Offline tests for a model the proxy does not serve (no network).

Run with: python3 -m unittest discover -s tests -v

A 400/404 that names the requested model as missing moves that model's calls to the next live
model of the ladder order; any other 404 (a wrong base URL) is not taken for a missing model.
"""
import threading
import unittest

from harness import llm

ORDER = ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"]
WRAP = "[181001] model call failed, reason: openAI API async invoke error: "


def client():
    c = llm.LLM.__new__(llm.LLM)  # no SDK or loop: only the routing state
    c.dead, c.fallback, c.lock, c.log = set(), list(ORDER), threading.Lock(), lambda rec: None
    return c


class ModelMissingTests(unittest.TestCase):
    def test_named_model_or_explicit_code(self):
        e = Exception(WRAP + "NotFoundError: Error code: 404 - The model `qwen3.8-27b-noreason` does not exist.")
        self.assertTrue(llm.model_missing(e, "qwen3.8-27b-noreason"))
        e = Exception(WRAP + "BadRequestError: Error code: 400 - Invalid model name passed in model=qwen3.8-27b")
        self.assertTrue(llm.model_missing(e, "qwen3.8-27b"))
        self.assertTrue(llm.model_missing(Exception(WRAP + "404 {'code': 'model_not_found'}"), "x"))

    def test_other_errors_are_not_a_missing_model(self):
        for text in ("NotFoundError: Error code: 404 - {'error': {'message': 'not found'}}",
                     "BadRequestError: Error code: 400 - This model's maximum context length is 32768 tokens",
                     "RateLimitError: Error code: 429 - upstream http 429"):
            self.assertFalse(llm.model_missing(Exception(WRAP + text), "qwen3.8-27b-noreason"), text)


class RoutingTests(unittest.TestCase):
    def test_fallback_order(self):
        c = client()
        dead = Exception(WRAP + "404 - The model `qwen3.8-27b-noreason` does not exist.")
        self.assertTrue(c._mark_dead(dead, "qwen3.8-27b-noreason", "qwen3.8-27b-noreason"))
        self.assertEqual(c.serving("qwen3.8-27b-noreason"), "qwen3.6-35b-a3b-noreason")
        # an attempt sent before the switch reports the old model: it is retried, nothing else dies
        self.assertTrue(c._mark_dead(dead, "qwen3.8-27b-noreason", "qwen3.8-27b-noreason"))
        self.assertEqual(c.dead, {"qwen3.8-27b-noreason"})
        dead2 = Exception(WRAP + "404 - The model `qwen3.6-35b-a3b-noreason` does not exist.")
        self.assertTrue(c._mark_dead(dead2, "qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason"))
        self.assertEqual(c.serving("qwen3.8-27b-noreason"), "qwen3.8-27b")

    def test_parallel_attempts_are_matched_to_the_model_they_went_to(self):
        # three attempts get 404 from 27b-noreason, are retried on 35b, all get 404 there too
        c = client()
        miss = lambda m: Exception(WRAP + "404 - The model `%s` does not exist." % m)
        for _ in range(3):
            self.assertTrue(c._mark_dead(miss(ORDER[0]), ORDER[0], ORDER[0]))
        for _ in range(3):
            self.assertTrue(c._mark_dead(miss(ORDER[1]), ORDER[0], ORDER[1]))
        self.assertEqual(c.serving(ORDER[0]), ORDER[2])
        self.assertEqual(c.dead, set(ORDER[:2]))

    def test_stand_in_reasoning_model_gets_room_to_think(self):
        c = client()
        self.assertEqual(c._tokens_for("qwen3.8-27b-noreason", 260), 260)
        for m in ORDER[:2]:
            c._mark_dead(Exception(WRAP + "404 - The model `%s` does not exist." % m), m)
        self.assertEqual(c._tokens_for("qwen3.8-27b-noreason", 260), llm.REASONING_STAND_IN_TOKENS)
        self.assertEqual(c._tokens_for("qwen3.8-27b", 900), 900)  # the reasoning rung itself is unchanged

    def test_all_missing_or_unrelated_error(self):
        c = client()
        self.assertFalse(c._mark_dead(Exception(WRAP + "404 - not found"), "qwen3.8-27b"))
        self.assertEqual(c.dead, set())
        for m in ORDER:
            c._mark_dead(Exception(WRAP + "404 - The model `%s` does not exist." % m), m)
        self.assertEqual(c.serving("qwen3.8-27b"), "qwen3.8-27b")  # nothing left: the call fails as before


if __name__ == "__main__":
    unittest.main()

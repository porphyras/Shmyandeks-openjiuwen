"""Run the real planner/solver/filesystem with a scripted LLM, without SDK/network.

Only LLM transport is replaced. Model programs execute in real subprocesses.
These tests intentionally do not test the openJiuwen rails or HTTP retry loop.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import run_agent
from harness.llm import BudgetExhausted, CallFailed


class ScriptedLLM:
    def __init__(self, handler, budget):
        self.handler, self.budget = handler, budget
        self.spent = 0.0
        self.calls = 0
        self.by_model = {}
        self.exhausted = False
        self.attempts = []
        self.counts = Counter()
        self.lock = threading.Lock()

    def remaining(self):
        return self.budget - self.spent if self.budget else None

    def chat(self, model, messages, max_tokens, *, tag, **kwargs):
        with self.lock:
            self.counts[tag] += 1
            attempt = self.counts[tag]
            self.attempts.append((tag, model))
        reply = self.handler(self, tag, attempt)
        with self.lock:
            self.calls += 1
            self.spent += 1
            entry = self.by_model.setdefault(model, [0, 0.0])
            entry[0] += 1
            entry[1] += 1
        return reply, 1, "stop"


def program(value=42):
    return "```python\nprint(%r)\n```" % value


class RunFaultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.events = []
        patches = [
            patch.dict(os.environ, {"HARNESS_CAREFUL_MAX": "0", "HARNESS_FAST": "0",
                                   "HARNESS_CROSSFIX": "0", "HARNESS_MATH_XC": "off"}, clear=True),
            patch.object(run_agent, "STOP", threading.Event()),
            patch.object(run_agent, "T0", time.time()),
            patch.object(run_agent, "trace", side_effect=lambda rec: self.events.append(dict(rec))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def add_task(self, name, reward=1, instruction="Compute six times seven."):
        task = self.tasks / name
        (task / "workspace").mkdir(parents=True)
        (task / "task.json").write_text(json.dumps({
            "task_id": name, "task_type": "math", "reward": reward,
            "instruction": instruction,
        }))
        return task

    def make_run(self, handler, workers=1, budget=100000):
        fake = ScriptedLLM(handler, budget)
        args = run_agent.parse_args(["--tasks-dir", str(self.tasks), "--out", str(self.root / "solutions.json"),
                                     "--workers", str(workers), "--budget", str(budget)])
        with patch("harness.llm.LLM", return_value=fake):
            run = run_agent.Run(args, "offline-dummy-key")
        return run, fake

    def assert_answers(self, expected):
        self.assertEqual(json.loads((self.root / "solutions.json").read_text()), expected)
        for name, answer in expected.items():
            for rel in ("answer.txt", "workspace/answer.txt"):
                path = self.tasks / name / rel
                self.assertTrue(path.is_file(), str(path))
                self.assertEqual(path.read_text(), answer + "\n")
        self.assertFalse([e for e in self.events if e.get("ev") == "worker_error"], self.events)

    def test_quota_exhaustion_preserves_incremental_answers_and_stops_calls(self):
        good = self.add_task("good", reward=100)
        self.add_task("quota", reward=10)
        self.add_task("pending", reward=1)

        def handler(llm, tag, attempt):
            if tag == "quota":
                # The earlier answer must already be durable, before the run finishes.
                self.assertEqual((good / "answer.txt").read_text(), "42\n")
                self.assertEqual(json.loads((self.root / "solutions.json").read_text()), {"good": "42"})
                (good / "workspace/answer.txt").unlink()
                llm.exhausted = True
                raise BudgetExhausted()
            self.assertEqual(tag, "good")
            return program()

        run, fake = self.make_run(handler)
        run.run()
        self.assertEqual([t for t, _ in fake.attempts], ["good", "quota"])
        self.assertTrue(run_agent.STOP.is_set())
        self.assertAlmostEqual(run.reserved, 0)
        self.assertFalse((self.tasks / "pending/answer.txt").exists())
        self.assert_answers({"good": "42"})

    def test_transport_error_does_not_discard_other_tasks_and_gets_second_pass(self):
        self.add_task("transient", reward=100)
        self.add_task("steady", reward=1)

        def handler(llm, tag, attempt):
            if tag == "transient" and attempt == 1:
                raise CallFailed("injected transport outage after client retries")
            return program()

        run, fake = self.make_run(handler)
        run.run()
        self.assertEqual([t for t, _ in fake.attempts], ["transient", "steady", "transient"])
        self.assertFalse(fake.attempts[-1][1].endswith("noreason"))
        self.assertEqual(fake.calls, 2)
        self.assertAlmostEqual(run.reserved, 0)
        self.assert_answers({"transient": "42", "steady": "42"})

    def test_invalid_program_is_retried_before_any_answer_is_saved(self):
        task = self.add_task("broken")

        def handler(llm, tag, attempt):
            self.assertFalse((task / "answer.txt").exists())
            if attempt == 1:
                return "```python\nraise RuntimeError('injected bad program')\n```"
            return program()

        run, fake = self.make_run(handler)
        run.run()
        self.assertEqual(len(fake.attempts), 2)
        self.assertNotEqual(fake.attempts[0][1], fake.attempts[1][1])
        self.assert_answers({"broken": "42"})

    def test_inspection_exception_is_isolated_to_its_task(self):
        self.add_task("bad_inspection")
        self.add_task("ordinary")
        run, fake = self.make_run(lambda *_: program())
        original_prepare = run.solver.prepare

        def prepare(task):
            if task.id == "bad_inspection":
                raise ValueError("injected corrupt inspection")
            return original_prepare(task)

        with patch.object(run.solver, "prepare", side_effect=prepare):
            run.run()
        self.assertEqual(fake.calls, 2)
        self.assert_answers({"bad_inspection": "42", "ordinary": "42"})

    def test_parallel_tasks_keep_their_own_answers_and_costs(self):
        expected = {"job_%02d" % i: str(i) for i in range(20)}
        for name, answer in expected.items():
            self.add_task(name, instruction="Compute %s plus zero." % answer)
        run, fake = self.make_run(lambda llm, tag, attempt: program(int(tag.rsplit("_", 1)[1])), workers=5)
        run.run()
        self.assertEqual(fake.calls, 20)
        self.assertAlmostEqual(run.reserved, 0)
        self.assertEqual(set(run.results.values()), {"solved"})
        self.assert_answers(expected)
        estimates = json.loads((self.root / "token_estimates.json").read_text())
        self.assertEqual(set(estimates), set(expected))
        self.assertTrue(all(e["actual"] == 1 and e["status"] == "solved" for e in estimates.values()))

    def test_unaffordable_high_reward_task_does_not_block_a_cheaper_one(self):
        self.add_task("expensive", reward=1000)
        self.add_task("cheap", reward=1)
        run, fake = self.make_run(lambda *_: program(), budget=250)
        costs = {"expensive": 1000, "cheap": 100}
        with patch.object(run, "estimate", side_effect=lambda task, info: (1.0, costs[task.name])):
            run.run()
        self.assertEqual([t for t, _ in fake.attempts], ["cheap"])
        self.assertTrue(any(e.get("ev") == "skip_budget" and e.get("task") == "expensive" for e in self.events))
        self.assertFalse((self.tasks / "expensive/answer.txt").exists())
        self.assert_answers({"cheap": "42"})


if __name__ == "__main__":
    unittest.main()

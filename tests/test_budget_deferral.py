"""Deterministic reservation pressure: wait for pressure, never for a timed sleep.

Exercises Run.run's real queue and output writes. Solver and budget ledger are
controlled doubles; this does not model token forecasts or proxy billing quality.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import run_agent
from harness.llm import BudgetExhausted
from harness.solver import Outcome


class BudgetDeferralTests(unittest.TestCase):
    def run_batch(self, *, budget=200, forecast=100, charge=1, quota=False,
                  initial_reserve=0, retry=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for i in range(6):
            d = root / ("task_%d" % i)
            d.mkdir()
            (d / "task.json").write_text(json.dumps({
                "task_id": d.name, "task_type": "math", "instruction": "Compute six times seven.", "reward": 1,
            }))
        gate, lock = threading.Event(), threading.Lock()
        attempts, events = [], []
        client = SimpleNamespace(budget=budget, spent=0, calls=0, by_model={}, exhausted=False)
        client.remaining = lambda: client.budget - client.spent

        def trace(rec):
            events.append(dict(rec))
            if rec.get("ev") in ("skip_budget", "defer_budget"):
                gate.set()

        def solve(task, info, level):
            with lock:
                attempts.append((task.name, level))
                first_wave = len(attempts) <= 2
            if first_wave and budget >= forecast:
                if not gate.wait(timeout=5):
                    raise AssertionError("reservation pressure was never observed")
            if quota:
                client.exhausted = True
                raise BudgetExhausted()
            with lock:
                client.spent += charge
            with run.lock:
                run.cost[task.name] = run.cost.get(task.name, 0) + charge
            if retry and level == 0:
                return Outcome("error", note="injected first-pass failure")
            return Outcome("solved", "42")

        args = run_agent.parse_args(["--tasks-dir", str(root), "--out", str(root / "solutions.json"),
                                     "--budget", str(budget), "--workers", "3"])
        env = {"HARNESS_FAST": "0", "HARNESS_CAREFUL_MAX": "0", "HARNESS_CROSSFIX": "0",
               "HARNESS_MATH_XC": "off"}
        with patch.dict(os.environ, env, clear=True), patch("harness.llm.LLM", return_value=client), \
                patch.object(run_agent, "STOP", threading.Event()), patch.object(run_agent, "T0", time.time()), \
                patch.object(run_agent, "trace", side_effect=trace):
            run = run_agent.Run(args, "offline-dummy")
            run.reserved = initial_reserve
            run.solver = SimpleNamespace(
                prepare=lambda task: {"kind": "math", "ctx_chars": 0, "problems": []}, solve=solve)
            # Bound even a regressed infinite requeue loop without relying on sleeps.
            checks = iter([1000] * 100)
            with patch.object(run, "estimate", return_value=(1.0, forecast)), \
                    patch.object(run, "time_left", side_effect=lambda: next(checks, 0)):
                run.run()
        return root, run, client, attempts, events

    def test_temporary_reservations_do_not_permanently_discard_affordable_tasks(self):
        root, run, client, attempts, events = self.run_batch()
        self.assertTrue(any(e.get("ev") in ("skip_budget", "defer_budget") for e in events))
        self.assertEqual(set(run.results.values()), {"solved"})
        self.assertEqual(len(run.results), 6, (attempts, events))
        self.assertEqual(len(attempts), 6)
        self.assertEqual(client.spent, 6)
        self.assertAlmostEqual(run.reserved, 0)
        answers = json.loads((root / "solutions.json").read_text())
        self.assertEqual(answers, {"task_%d" % i: "42" for i in range(6)})

    def test_truly_unaffordable_tasks_are_not_requeued(self):
        _, run, _, attempts, events = self.run_batch(budget=50)
        self.assertEqual(attempts, [])
        self.assertEqual(run.results, {})
        self.assertEqual(sum(e.get("ev") == "skip_budget" for e in events), 6)

    def test_expensive_completions_can_leave_deferred_tasks_unaffordable(self):
        _, run, client, attempts, _ = self.run_batch(charge=100)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(client.spent, 200)
        self.assertEqual(set(run.results.values()), {"solved"})
        self.assertAlmostEqual(run.reserved, 0)

    def test_quota_exhaustion_does_not_restart_deferred_tasks(self):
        _, run, client, attempts, _ = self.run_batch(quota=True)
        self.assertTrue(client.exhausted)
        self.assertLessEqual(len(attempts), 2)
        self.assertEqual(run.results, {})
        self.assertAlmostEqual(run.reserved, 0)

    def test_tiny_reservation_residue_does_not_cause_an_infinite_requeue(self):
        _, run, _, attempts, _ = self.run_batch(budget=100, charge=0, initial_reserve=1e-12)
        self.assertEqual(len(attempts), 6)
        self.assertEqual(len(run.results), 6)
        self.assertEqual(run.reserved, 0)

    def test_deferred_second_pass_keeps_its_retry_level(self):
        _, run, _, attempts, _ = self.run_batch(retry=True)
        self.assertEqual(len(attempts), 12)
        for name in run.results:
            self.assertEqual([level for task, level in attempts if task == name], [0, 2])
        self.assertEqual(len(run.results), 6)
        self.assertEqual(set(run.results.values()), {"solved"})


if __name__ == "__main__":
    unittest.main()

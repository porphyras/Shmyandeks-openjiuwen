"""Inspection past the deadline: each task is inspected exactly once (no network).

Run with: python3 -m unittest discover -s tests -v

prepare_all stops waiting at the deadline; an inspection still running is awaited by the
worker, not started a second time (two runs of one folder's tests could remove each other's
files), and one cancelled before it started runs then. Run.run with a controlled solver.
"""
import collections
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_agent
from harness.solver import Outcome


class LateInspectionTests(unittest.TestCase):
    def test_each_task_is_inspected_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for i in range(10):  # more than the 8 inspection threads: two are cancelled at the deadline
            d = root / ("task_%d" % i)
            d.mkdir()
            (d / "task.json").write_text(json.dumps({"task_id": d.name, "task_type": "math",
                                                     "instruction": "Compute six times seven.", "reward": 1}))
        calls, lock, events = collections.Counter(), threading.Lock(), []

        def prepare(task):
            with lock:
                calls[task.name] += 1
            time.sleep(0.5)
            return {"kind": "math", "ctx_chars": 0, "problems": []}

        client = SimpleNamespace(budget=None, spent=0, calls=0, by_model={}, exhausted=False)
        client.remaining = lambda: None
        args = run_agent.parse_args(["--tasks-dir", str(root), "--out", str(root / "solutions.json"),
                                     "--workers", "3"])
        env = {"HARNESS_FAST": "0", "HARNESS_CAREFUL_MAX": "0", "HARNESS_CROSSFIX": "0",
               "HARNESS_MATH_XC": "off", "HARNESS_INSPECT_DEADLINE": "0.1"}
        with patch.dict(os.environ, env, clear=True), patch("harness.llm.LLM", return_value=client), \
                patch.object(run_agent, "STOP", threading.Event()), patch.object(run_agent, "T0", time.time()), \
                patch.object(run_agent, "trace", side_effect=lambda rec: events.append(dict(rec))):
            run = run_agent.Run(args, "offline-dummy")
            run.solver = SimpleNamespace(prepare=prepare, solve=lambda task, info, level: Outcome("solved", "42"))
            with patch.object(run, "estimate", return_value=(1.0, 10.0)), \
                    patch.object(run, "time_left", return_value=1000.0):
                run.run()
        self.assertEqual(dict(calls), {"task_%d" % i: 1 for i in range(10)})
        how = collections.Counter(e["how"] for e in events if e.get("ev") == "inspect_late")
        self.assertEqual(how, {"awaited": 8, "started": 2})
        self.assertEqual(set(run.results.values()), {"solved"})
        self.assertEqual(len(run.results), 10)


if __name__ == "__main__":
    unittest.main()

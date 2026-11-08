"""Offline regression tests for the final answer recovery pass.

Run with: python3 -m unittest discover -s tests -v
"""
import tempfile
import threading
import unittest
from pathlib import Path

from harness.io_utils import write_answer
from run_agent import Run


class AnswerRecoveryTests(unittest.TestCase):
    def make_run(self, task_dir, answer="42"):
        # Recovery does not need a model client, SDK, or proxy credentials.
        run = Run.__new__(Run)
        run.infos = {}
        run.lock = threading.Lock()
        run.answers = {str(task_dir): answer}
        return run

    def test_restores_each_answer_copy(self):
        for location in ("answer.txt", "workspace/answer.txt"):
            for damage in ("overwrite", "delete"):
                with self.subTest(location=location, damage=damage):
                    with tempfile.TemporaryDirectory() as tmp:
                        task_dir = Path(tmp)
                        (task_dir / "workspace").mkdir()
                        self.assertTrue(write_answer(str(task_dir), "42"))
                        target = task_dir / location
                        if damage == "overwrite":
                            target.write_text("wrong\n", encoding="utf-8")
                        else:
                            target.unlink()

                        self.make_run(task_dir).rewrite_answers()

                        for copy in ("answer.txt", "workspace/answer.txt"):
                            self.assertEqual((task_dir / copy).read_text(), "42\n")

    def test_recovers_task_without_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            self.make_run(task_dir).rewrite_answers()
            self.assertEqual((task_dir / "answer.txt").read_text(), "42\n")

    def test_does_not_follow_workspace_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "answer.txt").write_text("untouched\n")
            (task_dir / "workspace").symlink_to(outside, target_is_directory=True)

            self.make_run(task_dir).rewrite_answers()

            self.assertEqual((task_dir / "answer.txt").read_text(), "42\n")
            self.assertEqual((outside / "answer.txt").read_text(), "untouched\n")


if __name__ == "__main__":
    unittest.main()

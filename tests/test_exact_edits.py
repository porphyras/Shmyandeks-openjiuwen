import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.context import Task
from harness.edits import EditError, MAX_EDITS, MAX_REPLY, apply_exact_edits
from harness import solver as solver_module


class ExactEditTests(unittest.TestCase):
    def test_exact_change_and_explicit_file(self):
        sources = {"a.py": "value = 1\n", "pkg/b.py": "value = 1\n"}
        changed = apply_exact_edits(sources, json.dumps([["pkg/b.py", "value = 1", "value = 2"]]))
        self.assertEqual(changed, {"pkg/b.py": "value = 2\n"})
        self.assertEqual(sources["pkg/b.py"], "value = 1\n")

    def test_fenced_reply_and_multiple_sequential_edits(self):
        reply = '```json\n[["1", "2"], ["2", "3"]]\n```'
        self.assertEqual(apply_exact_edits({"a.py": "value = 1\n"}, reply), {"a.py": "value = 3\n"})

    def test_ambiguous_within_or_across_files_is_rejected(self):
        for sources, old in (({"a.py": "a=1\nb=1\n"}, "1"),
                             ({"a.py": "a=1\n", "b.py": "b=1\n"}, "1"),
                             ({"a.py": "s='aaaa'\n"}, "aaa")):
            with self.subTest(sources=sources):
                with self.assertRaises(EditError):
                    apply_exact_edits(sources, json.dumps([[old, "2"]]))

    def test_malformed_missing_or_noop_edits_are_rejected(self):
        payloads = ("not json", "{}", "[]", '[["", "x"]]', '[[" ", "x"]]',
                    '[["1", "1"]]', '[[1, "2"]]', '[["missing", "2"]]',
                    '[["x"]]', '[["1", "2"], ["2", "1"]]')
        for reply in payloads:
            with self.subTest(reply=reply), self.assertRaises(EditError):
                apply_exact_edits({"a.py": "value = 1\n"}, reply)

    def test_ineligible_paths_are_rejected(self):
        for name in ("test_mylib.py", "../a.py", "/tmp/a.py", "new.py"):
            with self.subTest(name=name), self.assertRaises(EditError):
                apply_exact_edits({"a.py": "value = 1\n"}, json.dumps([[name, "1", "2"]]))

    def test_invalid_python_and_size_limits_are_rejected(self):
        payloads = (json.dumps([["1", "("]]), json.dumps([["1", "2"]] * (MAX_EDITS + 1)),
                    " " * (MAX_REPLY + 1))
        for reply in payloads:
            with self.subTest(length=len(reply)), self.assertRaises(EditError):
                apply_exact_edits({"a.py": "value = 1\n"}, reply)

    def test_failure_after_a_valid_edit_is_atomic(self):
        sources = {"a.py": "value = 1\n", "b.py": "other = 3\n"}
        original = dict(sources)
        with self.assertRaises(EditError):
            apply_exact_edits(sources, json.dumps([["1", "2"], ["missing", "4"]]))
        self.assertEqual(sources, original)


class CodingEditIntegrationTests(unittest.TestCase):
    BAD = "def total(xs):\n    result = 0\n    for x in xs:\n        result -= x\n    return result\n"
    GOOD = BAD.replace("result -= x", "result += x")
    TESTS = ("import unittest\nimport mylib as m\nclass T(unittest.TestCase):\n"
             "    def test_total(self):\n        self.assertEqual(m.total([1, 2]), 3)\n"
             "        self.assertEqual(m.total([-2, -1]), -3)\n        self.assertEqual(m.total([]), 0)\n"
             "if __name__ == '__main__':\n    unittest.main()\n")

    def solve(self, replies, enabled=True, level=0):
        replies = iter(replies)
        calls = []

        def ask(task, messages, *args, **kwargs):
            calls.append(messages[-1]["content"])
            return next(replies)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "mylib.py").write_text(self.BAD)
            tests = workspace / "test_mylib.py"
            tests.write_text(self.TESTS)
            task = Task(tmp, {"task_type": "coding", "instruction": "Fix mylib.py. Write the word DONE."})
            solver = solver_module.Solver(ask)
            info = solver.prepare(task)
            self.assertFalse(info["initial_pass"])
            with patch.object(solver_module, "CODE_EDITS", enabled):
                result = solver.solve(task, info, level)
            self.assertEqual(tests.read_text(), self.TESTS)
            return result, calls, (workspace / "mylib.py").read_text()

    def test_compact_edit_fixes_the_function_in_one_call(self):
        result, calls, source = self.solve([json.dumps([["result -= x", "result += x"]])])
        self.assertEqual((result.status, result.answer), ("solved", "DONE"))
        self.assertEqual(source, self.GOOD)
        self.assertEqual(len(calls), 1)
        self.assertIn("Reply ONLY with JSON", calls[0])

    def test_invalid_edit_gets_full_function_fallback(self):
        for bad in ("not json", json.dumps([["result", "answer"]]),
                    json.dumps([["test_mylib.py", "3", "0"]])):
            with self.subTest(reply=bad):
                result, calls, source = self.solve([bad, "```python\n%s\n```" % self.GOOD])
                self.assertEqual(result.status, "solved")
                self.assertEqual(source, self.GOOD)
                self.assertEqual(len(calls), 2)
                self.assertIn("Reply in one ```python", calls[1])
                self.assertIn("result -= x", calls[1])

    def test_wrong_but_valid_edit_must_pass_tests(self):
        wrong = json.dumps([["result -= x", "result += 2 * x"]])
        result, calls, source = self.solve([wrong, "```python\n%s\n```" % self.GOOD])
        self.assertEqual(result.status, "solved")
        self.assertEqual(len(calls), 2)
        self.assertIn("result += 2 * x", calls[1])
        self.assertEqual(source, self.GOOD)

    def test_full_function_reply_is_accepted_on_first_attempt(self):
        result, calls, source = self.solve(["```python\n%s\n```" % self.GOOD])
        self.assertEqual(result.status, "solved")
        self.assertEqual(len(calls), 1)
        self.assertEqual(source, self.GOOD)

    def test_disabled_mode_and_reasoning_retry_use_original_protocol(self):
        for enabled, level in ((False, 0), (True, 2)):
            with self.subTest(enabled=enabled, level=level):
                result, calls, _ = self.solve(["```python\n%s\n```" % self.GOOD], enabled, level)
                self.assertEqual(result.status, "solved")
                self.assertIn("Reply in one ```python", calls[0])

    def test_rejected_edits_never_produce_done(self):
        result, _, source = self.solve(["not json", "not python"])
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.answer)
        self.assertEqual(source, self.BAD)


if __name__ == "__main__":
    unittest.main()

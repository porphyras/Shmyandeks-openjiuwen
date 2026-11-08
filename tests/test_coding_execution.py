"""Exercise the real subprocess runner without the SDK or model calls."""
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.solver import Solver


class CodingExecutionTests(unittest.TestCase):
    def run_tests(self, source, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mylib.py").write_text(source, encoding="utf-8")
            tests = []
            for name, body in files.items():
                path = root / name
                path.write_text(textwrap.dedent(body), encoding="utf-8")
                tests.append(str(path))
            return Solver(None)._run_tests(SimpleNamespace(cwd=tmp), tests)

    CLASS_TEST = """\
        import unittest
        import mylib

        class TestTotal(unittest.TestCase):
            def test_total(self):
                self.assertEqual(mylib.total([1, 2]), 3)
        """
    GOOD = "def total(xs):\n    return sum(xs)\n"
    BAD = "def total(xs):\n    return 0\n"

    def test_testcase_without_main_detects_broken_code(self):
        ok, log = self.run_tests(self.BAD, {"test_mylib.py": self.CLASS_TEST})
        self.assertFalse(ok, log)
        self.assertIn("FAIL: test_total", log)
        self.assertIn("Ran 1 test", log)

    def test_testcase_without_main_runs_passing_test(self):
        ok, log = self.run_tests(self.GOOD, {"test_mylib.py": self.CLASS_TEST})
        self.assertTrue(ok, log)
        self.assertIn("Ran 1 test", log)

    def test_guarded_unittest_script_still_runs(self):
        script = textwrap.dedent(self.CLASS_TEST) + "\nif __name__ == '__main__':\n    unittest.main()\n"
        for source, expected in ((self.GOOD, True), (self.BAD, False)):
            with self.subTest(expected=expected):
                ok, log = self.run_tests(source, {"test_mylib.py": script})
                self.assertEqual(ok, expected, log)
                self.assertIn("Ran 1 test", log)

    def test_plain_functions_still_run(self):
        script = "import mylib\ndef test_total():\n    assert mylib.total([1, 2]) == 3\n"
        for source, expected in ((self.GOOD, True), (self.BAD, False)):
            with self.subTest(expected=expected):
                ok, log = self.run_tests(source, {"test_mylib.py": script})
                self.assertEqual(ok, expected, log)
                self.assertIn("Ran 1 test", log)

    def test_mixed_styles_run_both_tests(self):
        script = textwrap.dedent(self.CLASS_TEST) + "\ndef test_extra():\n    assert False, 'plain function ran'\n"
        ok, log = self.run_tests(self.GOOD, {"test_mylib.py": script})
        self.assertFalse(ok, log)
        self.assertIn("plain function ran", log)
        self.assertIn("Ran 2 test", log)

    def test_mixed_styles_do_not_skip_a_failing_class(self):
        script = textwrap.dedent(self.CLASS_TEST) + "\ndef test_extra():\n    assert True\n"
        ok, log = self.run_tests(self.BAD, {"test_mylib.py": script})
        self.assertFalse(ok, log)
        self.assertIn("FAIL: test_total", log)
        self.assertIn("Ran 2 test", log)

    def test_plain_async_function_is_not_silently_passed(self):
        script = "async def test_async():\n    assert False, 'must not be ignored'\n"
        ok, log = self.run_tests(self.GOOD, {"test_mylib.py": script})
        self.assertFalse(ok, log)
        self.assertIn("Async test needs an async test runner", log)

    def test_empty_suite_is_not_a_success(self):
        for script in ("import mylib\n", "import unittest\nunittest.main()\n"):
            with self.subTest(script=script):
                ok, log = self.run_tests(self.BAD, {"test_mylib.py": script})
                self.assertFalse(ok, log)

    def test_empty_file_list_is_not_a_success(self):
        ok, log = self.run_tests(self.BAD, {})
        self.assertFalse(ok, log)

    def test_sixth_test_file_is_not_ignored(self):
        script = textwrap.dedent(self.CLASS_TEST) + "\nif __name__ == '__main__':\n    unittest.main()\n"
        files = {"test_%d.py" % i: script for i in range(5)}
        files["test_5.py"] = "raise AssertionError('sixth test ran')\n"
        ok, log = self.run_tests(self.GOOD, files)
        self.assertFalse(ok, log)
        self.assertIn("sixth test ran", log)


if __name__ == "__main__":
    unittest.main()

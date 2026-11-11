"""Offline tests for pytest-style test files and the missing-fixture verdict (no network).

Run with: python3 -m unittest discover -s tests -v

A file that needs pytest (imports it, fixture or parametrize arguments, conftest.py) runs under
the image's pytest; a run where nothing passed is not a success. Otherwise the fallback runner
also runs plain Test* classes (with setup_method) and the plain tests of a file whose
unittest.main() skips them. A test reads a missing file only when nothing before the read could have
produced it.
"""
import os
import tempfile
import textwrap
import unittest

from harness import context as ctx
from harness import solver

BUGGY = "class Stack:\n    def __init__(self): self.items = []\n    def push(self, x): self.items.append(x)\n" \
        "    def pop(self): return self.items.pop(0)\n    def is_empty(self): return not self.items\n"
FIXED = BUGGY.replace("pop(0)", "pop()")


def run_tests(test_src, code=BUGGY):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "stack.py"), "w") as f:
        f.write(code)
    with open(os.path.join(d, "test_stack.py"), "w") as f:
        f.write(textwrap.dedent(test_src))
    return solver.Solver(None)._run_tests_once(None, [os.path.join(d, "test_stack.py")], 60)


PLAIN_CLASS = """
from stack import Stack

def test_empty():
    assert Stack().is_empty()

class TestStack:
    def setup_method(self, method):
        self.s = Stack()
    def test_pop(self):
        self.s.push(1); self.s.push(2)
        assert self.s.pop() == 2
"""

MIXED = """
import unittest
from stack import Stack

class T(unittest.TestCase):
    def test_empty(self):
        self.assertTrue(Stack().is_empty())

def test_pop():
    s = Stack(); s.push(1); s.push(2)
    assert s.pop() == 2

if __name__ == "__main__":
    unittest.main()
"""


class RunnerTests(unittest.TestCase):
    def test_plain_class_is_run(self):
        ok, out = run_tests(PLAIN_CLASS)
        self.assertFalse(ok)
        self.assertIn("Ran 2 tests", out)
        self.assertTrue(run_tests(PLAIN_CLASS, FIXED)[0])

    def test_plain_tests_next_to_unittest_main_are_run(self):
        ok, out = run_tests(MIXED)
        self.assertFalse(ok)
        self.assertTrue(run_tests(MIXED, FIXED)[0])

    def test_class_with_init_is_not_collected(self):
        ok, out = run_tests("from stack import Stack\nclass TestX:\n    def __init__(self): pass\n"
                            "    def test_a(self):\n        assert True\n")
        self.assertFalse(ok)
        self.assertIn("No runnable tests", out)

    def test_fixtures_and_parametrize_run_under_pytest(self):
        src = """
        import pytest
        from stack import Stack

        @pytest.mark.parametrize("xs,top", [([1, 2], 2), ([5], 5)])
        def test_top(xs, top):
            s = Stack()
            for x in xs:
                s.push(x)
            assert s.pop() == top

        def test_tmp(tmp_path):
            (tmp_path / "a.txt").write_text("x")
            assert Stack().is_empty()
        """
        self.assertTrue(run_tests(src, FIXED)[0])
        ok, out = run_tests(src, BUGGY)
        self.assertFalse(ok)
        self.assertEqual(ctx.failing_test_names(out), ["test_top"])

    def test_all_skipped_is_not_a_pass(self):
        src = "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert False\n"
        self.assertFalse(run_tests(src, FIXED)[0])

    def test_failing_names_from_pytest_summary(self):
        out = "FAILED test_m.py::TestStack::test_pop - assert 1 == 2\nERROR test_m.py::test_load - TypeError\n"
        self.assertEqual(ctx.failing_test_names(out), ["test_pop", "test_load"])


class MissingFixtureTests(unittest.TestCase):
    def check(self, src, sources=()):
        return ctx.missing_fixtures(textwrap.dedent(src), tempfile.mkdtemp(), sources)

    def test_read_before_any_code_is_a_verdict(self):
        src = """
        import unittest, core as m
        class T(unittest.TestCase):
            def test_fixture(self):
                with open('fixtures/sample.txt') as fh:
                    text = fh.read()
                self.assertEqual(m.word_count(text), 3)
        """
        self.assertEqual(self.check(src), ["test reads missing file fixtures/sample.txt"])
        self.assertEqual(self.check("from pathlib import Path\ndef test_a():\n    assert Path('in.json').read_text()\n"),
                         ["test reads missing file in.json"])

    def test_file_the_code_writes_first_is_not_missing(self):
        src = """
        import unittest
        from pathlib import Path
        from report import save_report, export
        class T(unittest.TestCase):
            def test_save(self):
                save_report([1, 2])
                with open("report.txt") as f:
                    self.assertEqual(f.read(), "1,2")
            def test_export(self):
                out = Path("out.csv")
                export([1, 2], out)
                self.assertTrue(out.exists())
        """
        self.assertEqual(self.check(src), [])

    def test_setup_or_source_mention_blocks_the_verdict(self):
        src = """
        import unittest, gen
        class T(unittest.TestCase):
            def setUp(self):
                gen.build()
            def test_a(self):
                self.assertTrue(open("data.txt").read())
        """
        self.assertEqual(self.check(src), [])
        src2 = "def test_a():\n    assert open('data.txt').read()\n"
        self.assertEqual(self.check(src2), ["test reads missing file data.txt"])
        self.assertEqual(self.check(src2, ["def build():\n    open('data.txt', 'w').write('x')\n"]), [])


if __name__ == "__main__":
    unittest.main()

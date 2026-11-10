"""Offline tests for the model path on inputs away from the bench templates (no model calls).

Run with: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import tempfile
import unittest

from harness import context as ctx
from harness import solver


def write(text, name="t.csv"):
    d = tempfile.mkdtemp(prefix="gp_")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def run(code):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(solver._tolerant_ints(code), {})
    return buf.getvalue().strip()


class TableLoadingTests(unittest.TestCase):
    def test_comment_line_before_the_header(self):
        p = write("# export 2024-05-01, amounts in NOK\norder_id,amount,status\n1,20,paid\n2,30,refunded\n")
        self.assertEqual(ctx.table_skip(p), 1)
        self.assertIn("skiprows=1", ctx.pandas_load(p, "t.csv"))
        brief, info = ctx.csv_brief(p)
        self.assertEqual(info["header"], ["order_id", "amount", "status"])
        self.assertEqual(info["rows"], 2)

    def test_plain_table_is_unchanged(self):
        p = write("a,b\n1,2\n")
        self.assertEqual(ctx.table_skip(p), 0)
        self.assertNotIn("skiprows", ctx.pandas_load(p, "t.csv"))

    def test_semicolon_and_decimal_comma(self):
        p = write("sku;qty;unit_cost\nA;1;2,50\nB;3;4,75\n")
        load = ctx.pandas_load(p, "t.csv")
        self.assertIn("sep=';'", load)
        self.assertIn("decimal=','", load)
        brief, _ = ctx.csv_brief(p)
        self.assertIn("semicolon-separated", brief)
        self.assertIn("A;1;2,50", brief)


class TolerantIntTests(unittest.TestCase):
    def test_rounding_noise_becomes_the_integer(self):
        self.assertEqual(run("print(int(72 / (0.8 * 0.9)))"), "100")
        self.assertEqual(run("import math\nprint(math.floor(0.1 * 3 * 10), math.ceil(1.1 * 3 - 0.3))"), "3 3")

    def test_other_values_behave_as_before(self):
        self.assertEqual(run("import math\nprint(int(7.9), int('42'), int('ff', 16), math.floor(250 / 12), math.ceil(2.1))"),
                         "7 42 255 20 3")

    def test_code_without_calls_is_untouched(self):
        self.assertEqual(solver._tolerant_ints("print(1 + 2)"), "print(1 + 2)")



class NewCodeInstructionTests(unittest.TestCase):
    def test_new_code_is_not_done_for_free(self):
        self.assertTrue(solver._NEW_CODE.search(
            "The tests in test_mylib.py pass. Add a function average(xs) to mylib.py; hidden tests will check it."))
        self.assertTrue(solver._NEW_CODE.search("Implement the method area of class Circle."))

    def test_bug_fix_wording_keeps_the_free_done(self):
        for text in ("utils.py has a few bugs that tests.py catches. Fix them. Write DONE (and nothing else) to answer.txt",
                     "Do not add new files anywhere. Create an answer.txt with DONE."):
            self.assertIsNone(solver._NEW_CODE.search(text))


if __name__ == "__main__":
    unittest.main()

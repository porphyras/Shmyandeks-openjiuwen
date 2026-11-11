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


class AnnouncedColumnTests(unittest.TestCase):
    def problems(self, question):
        d = tempfile.mkdtemp(prefix="gp_")
        os.makedirs(os.path.join(d, "workspace"))
        with open(os.path.join(d, "workspace", "sales.csv"), "w") as f:
            f.write("order_id,region,units\n1,west,5\n2,east,7\n")
        instr = "The file sales.csv has columns order_id,region,units,discount (2 rows). " + question
        return ctx.data_context(ctx.Task(d, {"task_id": "t", "instruction": instr, "reward": 1}))[1]

    def test_missing_column_the_question_uses(self):
        self.assertTrue(any("discount" in p for p in self.problems("What is the average discount in region 'west'?")))

    def test_missing_column_the_question_does_not_use(self):
        self.assertFalse(any("missing in file" in p for p in self.problems("How many units were sold in region 'west'?")))


class AnswerFormatTests(unittest.TestCase):
    def test_decimals_the_task_asks_for(self):
        ask = "What is the average price? Round the result to 2 decimal places."
        self.assertEqual(solver.format_for_instruction("51.4", ask), "51.40")
        self.assertEqual(solver.format_for_instruction("15", ask), "15.00")
        self.assertEqual(solver.format_for_instruction("1234.5", ask), "1234.50")
        self.assertEqual(solver.format_for_instruction("0.5", ask), "0.50")
        self.assertEqual(solver.format_for_instruction("3.14159", "Give it to 3 decimals."), "3.142")

    def test_more_digits_round_like_python(self):
        ask = "Round the result to 2 decimal places."
        self.assertEqual(solver.format_for_instruction("2.675", ask), "2.67")  # f"{2.675:.2f}"
        self.assertEqual(solver.format_for_instruction("0.125", ask), "0.12")
        self.assertEqual(solver.format_for_instruction("-1.005", ask), "%.2f" % -1.005)
        self.assertEqual(solver.format_for_instruction("2.675", "Round to two decimals (round half up)."), "2.68")

    def test_other_answers_unchanged(self):
        self.assertEqual(solver.format_for_instruction("7", "How many units were sold?"), "7")
        self.assertEqual(solver.format_for_instruction("west", "Round the result to 2 decimal places."), "west")
        self.assertEqual(solver.format_for_instruction("3", "Round to 2 decimal places, or to 3 decimals."), "3")

    def test_label_questions(self):
        self.assertTrue(solver._ASKS_LABEL.search("Which product had the highest revenue?"))
        self.assertIsNone(solver._ASKS_LABEL.search("Which year had the most sales?"))


class DecimalsOfTheAnswerTests(unittest.TestCase):
    """Only a phrase about the answer sets its decimals; a description of the data never does."""

    def test_descriptions_of_the_data(self):
        for text in ("The file s.csv has columns a,b (3 rows). Prices are stored with 2 decimal places. How many units?",
                     "The price column is given to 2 decimals. How many units were sold?",
                     "All amounts in the file are rounded to 2 decimal places. How many units were sold?",
                     "What is the total number of units, rounded to the nearest integer? Prices have 2 decimal places.",
                     "How many orders have a price above 5.30? Compare the prices using 2 decimal places.",
                     "Each box weighs 2.50 kg, a weight written with two decimals. How many boxes are left?",
                     "Round all prices to 2 decimals before summing. How many units were sold?",
                     "How many orders have a price with 2 decimal places?",
                     "How many orders have a price, rounded to 2 decimal places, above 5.30?",
                     "What is the number of products whose average price rounded to 2 decimals equals 5.25?",
                     "Round the value to 2 decimal places before comparing. How many orders exceed 5.30?",
                     "The prices have 2 decimal places in the result table. How many rows does it have?",
                     "Give the number of orders whose price, written with 2 decimal places, exceeds 5.30.",
                     "Report the count of products priced to 2 decimal places.",
                     "State how many prices in the file have more than 2 decimal places."):
            self.assertEqual(solver.format_for_instruction("42", text), "42", text)

    def test_phrases_about_the_answer(self):
        for text, want in (("How many dollars does it earn in total (two decimals)?", "42.00"),
                           ("What is the mean price? Round half up to 2 decimal places.", "42.00"),
                           ("What is the mean price? Keep 2 decimal places in the answer.", "42.00"),
                           ("What is it? Give the answer with exactly two decimals (round half up).", "42.00"),
                           ("What fraction is left? Give it as a decimal rounded to two decimals.", "42.00"),
                           ("Compute the average price and round it to 3 decimals.", "42.000"),
                           ("What is the mean? Round the result to 0 decimal places.", "42"),
                           ("What is the average price, to two decimal places?", "42.00"),
                           ("Compute the mean, rounded to 2 decimal places.", "42.00"),
                           ("The answer should be rounded to 2 decimal places. What is the mean?", "42.00"),
                           ("What is the mean? The result must be given with 2 decimal places.", "42.00"),
                           ("What is the mean? Use 2 decimal places.", "42.00"),
                           ("What share is it? Give your answer as a percentage with one decimal.", "42.0"),
                           ("What percentage was sold? Give the answer as a percentage rounded to 1 decimal place, "
                            "without the % sign.", "42.0"),
                           ("What is the sum of quantity * unit_price? Give the amount in dollars with 2 decimal "
                            "places.", "42.00")):
            self.assertEqual(solver.format_for_instruction("42", text), want, text)
        self.assertEqual(solver.format_for_instruction("-0.004", "What is it? Round the result to 2 decimal places."),
                         "0.00")
        self.assertEqual(solver.format_for_instruction(
            "8%", "What percentage was sold? Give it as a percentage rounded to 1 decimal place, with the % sign."), "8.0%")
        self.assertEqual(solver.format_for_instruction(
            "8%", "What percentage was sold? Give the answer as a percentage rounded to 1 decimal place, without the % "
                  "sign."), "8.0")


class CrossfixGuardTests(unittest.TestCase):
    def test_a_dropped_call_is_not_accepted(self):
        from harness import crossfix
        cur = "def count_vowels(s):\n    return sum(1 for ch in s.lower() if ch in 'aeio')\n"
        cand = "def count_vowels(s):\n    return sum(1 for ch in s if ch in 'aeiou')\n"
        self.assertFalse(crossfix._keeps_operations(cur, cand))

    def test_lost_guards_and_arguments(self):
        from harness import crossfix
        keep = crossfix._keeps_operations
        self.assertFalse(keep("def f(x):\n    if x is None:\n        raise ValueError\n    return x + 1\n",
                              "def f(x):\n    return x + 1\n"))                       # guard lost
        self.assertFalse(keep("def f(xs):\n    return sorted(xs, key=len)\n",
                              "def f(xs):\n    return sorted(xs, reverse=True)\n"))     # key= lost
        self.assertFalse(keep("def f(x):\n    return round(x, 1)\n", "def f(x):\n    return round(x)\n"))
        self.assertFalse(keep("def f(s):\n    return s.strip().upper()\n", "def f(s):\n    return s.lower()\n"))
        self.assertFalse(keep("def f(xs):\n    return [x for x in xs if x > 0]\n", "def f(xs):\n    return [x for x in xs]\n"))

    def test_a_replaced_call_is_accepted(self):
        from harness import crossfix
        self.assertTrue(crossfix._keeps_operations("def f(s):\n    return s.upper()\n", "def f(s):\n    return s.lower()\n"))
        self.assertTrue(crossfix._keeps_operations("def f(xs):\n    return sorted(xs)\n",
                                                   "def f(xs):\n    return list(reversed(xs))\n"))
        # the usual planted bugs: which end of the list, a copy instead of a sort, count vs len(split)
        self.assertTrue(crossfix._keeps_operations("def pop(self):\n    return self.items.pop(0)\n",
                                                   "def pop(self):\n    return self.items.pop()\n"))
        self.assertTrue(crossfix._keeps_operations("def f(xs):\n    s = list(xs)\n    return s[0]\n",
                                                   "def f(xs):\n    s = sorted(xs)\n    return s[0]\n"))
        self.assertTrue(crossfix._keeps_operations("def f(s):\n    return s.count(' ')\n",
                                                   "def f(s):\n    return len(s.split())\n"))
        self.assertFalse(crossfix._keeps_operations("def f(xs, i):\n    return xs.pop(i)\n",
                                                    "def f(xs, i):\n    return xs.pop()\n"))
        # one way only: an order or a size must not be traded away
        self.assertFalse(crossfix._keeps_operations("def f(xs):\n    return sorted(xs)[0]\n",
                                                    "def f(xs):\n    return list(xs)[0]\n"))
        self.assertFalse(crossfix._keeps_operations("def f(xs):\n    return len(xs) + 1\n",
                                                    "def f(xs):\n    return sum(xs) + 1\n"))


class DecimalCommaTests(unittest.TestCase):
    def clean(self, values):
        import pandas as pd
        ns = {"pd": pd}
        exec(solver._CLEAN, ns)
        return ns["_clean"](pd.DataFrame({"price": values}))["price"].tolist()

    def test_decimal_comma_is_a_decimal(self):
        self.assertEqual(self.clean(["33,50", "28,25", "10,00"]), [33.5, 28.25, 10.0])
        self.assertEqual(self.clean(["1,5", "2,0"]), [1.5, 2.0])

    def test_thousands_stay_thousands(self):
        self.assertEqual(self.clean(["1,250", "12,345,678", "999"]), [1250, 12345678, 999])
        self.assertEqual(self.clean(["$1,250", "$2,000"]), [1250, 2000])

    def test_tab_separated_decimal_comma_loads_as_numbers(self):
        p = write("sku\tprice\nA\t33,50\nB\t28,25\n", "t.tsv")
        self.assertIn("decimal=','", ctx.pandas_load(p, "t.tsv"))
        brief, _ = ctx.csv_brief(p)
        self.assertIn("A\t33,50", brief)

    def test_known_package_missing_is_not_a_verdict(self):
        d = tempfile.mkdtemp(prefix="gp_")
        self.assertEqual(ctx.missing_imports("import scipy\nimport mylocalhelper\n", d), ["mylocalhelper"])


if __name__ == "__main__":
    unittest.main()

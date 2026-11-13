"""Offline tests for the z12 fixes behind flags (no network).

Run with: python3 -m unittest discover -s tests -v

HARNESS_FIX_CUT: a program cut at max_tokens is retried once instead of trusting its IMPOSSIBLE.
HARNESS_FIX_NFKD: retrieval folds accents (Jürgen = Jurgen). HARNESS_FIX_XLSX: pd.read_excel drops a
trailing total row; small note sheets are shown whole. HARNESS_FIX_CARDS: "- **Key:** value" cards and
front-matter titles.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from harness import context as ctx
from harness import sandbox
from harness import solver
from harness.solver import Solver


class CutTests(unittest.TestCase):
    def run_task(self, flag):
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(max_tokens)
            if len(calls) == 1:
                task._last_finish = "length"
                return "```python\n# a very long comment that ran out\nprint('IMPOSSIBLE')\n```"
            task._last_finish = "stop"
            return "```python\nprint(6 * 7)\n```"

        with tempfile.TemporaryDirectory() as tmp, patch.object(solver, "FIX_CUT", flag):
            task = ctx.Task(tmp, {"task_type": "math", "instruction": "Compute six times seven."})
            s = Solver(ask)
            return s.solve(task, s.prepare(task), 0), calls

    def test_cut_program_is_retried(self):
        out, calls = self.run_task(True)
        self.assertEqual((out.status, out.answer), ("solved", "42"))
        self.assertEqual(calls, [400, 800])

    def test_off_by_default(self):
        self.assertFalse(solver.FIX_CUT)
        out, calls = self.run_task(False)
        self.assertEqual(out.status, "infeasible")


class NfkdTests(unittest.TestCase):
    def test_accents_fold(self):
        with patch.object(ctx, "FIX_NFKD", True):
            self.assertEqual(ctx._norm("Jürgen Straße"), "jurgen strasse")
        self.assertEqual(ctx._norm("Jürgen"), "j rgen")  # off: as before


class XlsxTests(unittest.TestCase):
    def test_total_row_dropped_and_shown(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "sales"
            ws.append(["region", "units"])
            for r, u in (("north", 10), ("south", 20), ("east", 5), ("west", 7), ("x", 1), ("y", 2), ("z", 3)):
                ws.append([r, u])
            ws.append(["Total", 48])
            note = wb.create_sheet("notes")
            for line in ("About this file", "Units are pieces", "The last row of sales is a total"):
                note.append([line])
            wb.save(os.path.join(tmp, "d.xlsx"))
            prog = "import pandas as pd\nprint(pd.read_excel('d.xlsx')['units'].sum())\n"
            rc, out, err = sandbox.run_python_code(solver._XLSX_TOTALS + prog, tmp)
            self.assertEqual((rc, out.strip()), (0, "48"), err)
            rc, out, err = sandbox.run_python_code(prog, tmp)
            self.assertEqual(out.strip(), "96")
            with patch.object(ctx, "FIX_XLSX", True):
                brief = ctx.excel_brief(os.path.join(tmp, "d.xlsx"), "d.xlsx")
            self.assertIn("Total, 48", brief)
            self.assertIn("The last row of sales is a total", brief)


class CardTests(unittest.TestCase):
    def test_bulleted_bold_cards_and_front_matter(self):
        docs = [("kb/c%d.md" % i, "# P%d\n- **Born:** 19%02d\n- **Employer:** Acme\n- **City:** Graz\n" % (i, i))
                for i in range(5)]
        self.assertFalse(ctx.kb_structured(docs))
        with patch.object(ctx, "FIX_CARDS", True):
            self.assertTrue(ctx.kb_structured(docs))
            self.assertEqual(ctx._title("kb/C016.md", "---\nid: C016\nname: Jonas Muller\n---\nBorn: 1980\n"),
                             "Jonas Muller")
        self.assertEqual(ctx._title("kb/C016.md", "---\nname: Jonas Muller\n---\nBorn: 1980\n"), "C016")


if __name__ == "__main__":
    unittest.main()

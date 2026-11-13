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
    def run_task(self, flag, closed=False):
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(max_tokens)
            if len(calls) == 1:
                task._last_finish = "length"
                if closed:
                    return "```python\nprint(6 * 7)\n```\nThis program multiplies the two numbers and then"
                return "```python\n# a very long comment that ran out\nprint('IMPOSSIBLE')"
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

    def test_on_by_default_and_off_switch(self):
        self.assertTrue(solver.FIX_CUT)
        out, calls = self.run_task(False)
        self.assertEqual(out.status, "infeasible")

    def test_closed_block_is_not_retried(self):
        out, calls = self.run_task(True, closed=True)
        self.assertEqual((out.status, out.answer, calls), ("solved", "42", [400]))


class NfkdTests(unittest.TestCase):
    def test_accents_fold(self):
        self.assertEqual(ctx._norm("Jürgen Straße"), "jurgen strasse")
        with patch.object(ctx, "FIX_NFKD", False):
            self.assertEqual(ctx._norm("Jürgen"), "j rgen")  # off: as before


class XlsxTests(unittest.TestCase):
    def workbook(self, tmp, chart=False):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "sales"
        ws.append(["region", "units"])
        for r, u in (("north", 10), ("south", 20), ("east", 5), ("west", 7), ("x", 1), ("y", 2), ("z", 3)):
            ws.append([r, u])
        ws.append(["Total", 48])
        note = wb.create_sheet("notes")
        for line in ["About this file", "Units are pieces", "The last row of sales is a total"] + ["x" * 150] * 10:
            note.append([line])
        if chart:
            from openpyxl.chart import BarChart
            cs = wb.create_chartsheet("chart")
            cs.add_chart(BarChart())
        p = os.path.join(tmp, "d.xlsx")
        wb.save(p)
        return p

    def test_total_row_and_notes_shown_data_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = ctx.excel_brief(self.workbook(tmp), "d.xlsx")
            self.assertIn("Total, 48", brief)
            self.assertIn("The last row of sales is a total", brief)
            self.assertIn("header at row 1: region, units", brief)
            self.assertNotIn("x" * 150 + "\n  " + "x" * 150 + "\n  " + "x" * 150, brief)

    def test_read_excel_is_not_patched(self):
        self.assertFalse(hasattr(solver, "_XLSX_TOTALS"))

    def test_chart_sheet_does_not_break_the_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = ctx.excel_brief(self.workbook(tmp, chart=True), "d.xlsx")
            self.assertIn("header at row 1: region, units", brief)


class CardTests(unittest.TestCase):
    def test_bulleted_bold_cards_and_front_matter(self):
        docs = [("kb/c%d.md" % i, "# P%d\n- **Born:** 19%02d\n- **Employer:** Acme\n- **City:** Graz\n" % (i, i))
                for i in range(5)]
        self.assertTrue(ctx.kb_structured(docs))
        self.assertEqual(ctx._title("kb/C016.md", "---\nid: C016\nname: Jonas Muller\n---\nBorn: 1980\n"), "Jonas Muller")
        self.assertEqual(ctx._title("kb/C016.md", "---\ntitle: Senior Engineer\nname: Jonas Muller\n---\n# X\n"),
                         "Jonas Muller")
        self.assertEqual(ctx._title("kb/C016.md", "---\ntitle: Senior Engineer\n---\n# Jonas Muller\n"), "Jonas Muller")
        self.assertEqual(ctx._title("kb/C016.md", "---\nname:\nrole: x\n---\nBorn: 1980\n"), "C016")
        self.assertEqual(ctx._title("kb/C016.md", "---\nname: >\n  Jonas\n---\nBorn: 1980\n"), "C016")
        with patch.object(ctx, "FIX_CARDS", False):
            self.assertFalse(ctx.kb_structured(docs))
            self.assertEqual(ctx._title("kb/C016.md", "---\nname: Jonas Muller\n---\nBorn: 1980\n"), "C016")

    def test_bold_prose_stays_prose(self):
        docs = [("kb/i%d.md" % i, "# Interview %d\n**Q: Where did you grow up?**\nIn a small town by the sea, "
                 "where my father ran a boatyard.\n**Q: And later?**\nI moved to Graz to study.\n" % i) for i in range(5)]
        self.assertFalse(ctx.kb_structured(docs))


if __name__ == "__main__":
    unittest.main()

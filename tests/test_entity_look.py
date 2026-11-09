"""Offline tests for the targeted look after NOT FOUND (no model calls).

Run with: python3 -m unittest discover -s tests -v

The look gathers the paragraphs about the entities a reply reached: every written form of a
name ("J. Muller"), the paragraph after an introduction, and documents named after the entity.
"""
import unittest

from harness import context as ctx

DOCS = [
    ("kb/interview_jonas_muller.md", "# Interview: Jonas Muller\n\nJonas Muller (1961–) has spent a career in the field.\n\nRelated reading: notes."),
    ("kb/newsletter.md", "# Arktika Labs newsletter\n\nStaff spotlight: Dmitri Sato (finance analyst).\n\nBirth year: 1988\nFavourite tool: wrench"),
    ("kb/meeting.md", "# Quarterly roadmap\n\nAttendees: Nikolai Kowalski, Alice Sato\n\nBirthday round: N. Kowalski was born in 1982."),
    ("kb/office.md", "# Tbilisi office\n\nArktika Labs — HQ: Tbilisi"),
    ("kb/other.md", "# Unrelated\n\nNothing about anyone here."),
]


class EntityLookTests(unittest.TestCase):
    def look(self, question, names):
        return ctx.entity_passages(DOCS, question, names)

    def test_document_named_after_the_entity(self):
        self.assertIn("Jonas Muller (1961–)", self.look("In which year was Maya Wang's mentor born?", ["Jonas Muller"]))

    def test_block_after_the_introduction(self):
        self.assertIn("Birth year: 1988", self.look("In which year was Dmitri Sato born?", ["Dmitri Sato"]))

    def test_initial_and_surname(self):
        self.assertIn("N. Kowalski was born in 1982", self.look("In which year was Tanya's mentor born?", ["Nikolai Kowalski"]))

    def test_attribute_synonym_ranks_the_mention(self):
        self.assertIn("HQ: Tbilisi", self.look("In which city is the headquarters of Arktika Labs?", ["Arktika Labs"]))

    def test_names_the_base_never_mentions_are_dropped(self):
        self.assertEqual(self.look("Who is Zed Qux's mentor?", ["Zed Qux", "Identify Zed Qux"]), "")
        self.assertNotIn("Nothing about anyone", self.look("In which year was Dmitri Sato born?", ["Dmitri Sato"]))

    def test_reply_names_puts_the_reached_entity_first(self):
        reply = "Maya Wang's mentor is Jonas Muller. Jonas Muller's birth year is not stated.\nNOT FOUND"
        names = ctx.reply_names("In which year was Maya Wang's mentor born?", reply)
        self.assertEqual(names[0], "Jonas Muller")
        self.assertIn("Maya Wang", names)


if __name__ == "__main__":
    unittest.main()

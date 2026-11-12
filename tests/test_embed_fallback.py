"""Offline tests for the embedding look (no network; the model ships in models/).

Run with: python3 -m unittest discover -s tests -v

The int8 bge-small model loads after its sha256 check and ranks a paraphrase above unrelated
text; when every look at a large prose base ended in NOT FOUND, the unseen document closest to
the question gets one more look; a broken model turns the look off without an error.
"""
import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from harness import context as ctx
from harness import embed
from harness.solver import EMBED_FALLBACK, Solver

LOOM = ("The treadle loom appeared in Flanders in the twelfth century. Weavers worked its heddles "
        "with their feet, which left both hands free for the shuttle.")


def filler(i):
    return ("Document %d describes the annual harvest festival of a river town, with its market stalls, "
            "brass band, rowing race and the long evening of speeches by the town council.\n" % i) * 3


class EmbedModelTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(embed)

    def test_paraphrase_ranks_first(self):
        q = embed.embed([embed.QUERY_PREFIX + "Where was the foot-operated weaving frame from the 1100s first used?"])[0]
        docs = embed.embed([LOOM, filler(1), "Copper prices rose sharply in the second quarter."])
        self.assertEqual(docs.shape, (3, 384))
        self.assertAlmostEqual(float(np.linalg.norm(docs[0])), 1.0, places=4)
        self.assertEqual(int(np.argmax(docs @ q)), 0)

    def test_checksum_mismatch_turns_the_look_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            for f in embed.MODEL_FILES:
                shutil.copy(os.path.join(embed.model_dir(), f), tmp)
            with open(os.path.join(tmp, "tokenizer.json"), "a") as f:
                f.write(" ")
            with patch.dict(os.environ, {"HARNESS_EMBED_DIR": tmp}):
                with self.assertRaises(RuntimeError):
                    embed.embed(["x"])
                self.assertTrue(embed._FAILED)
                with self.assertRaises(RuntimeError):
                    embed.embed(["x"])


class EmbedLookTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(embed)

    def test_on_by_default(self):
        self.assertTrue(EMBED_FALLBACK)

    def look(self, broken=False):
        calls = []

        def ask(task, msgs, level, max_tokens, temperature=0.0):
            calls.append(msgs[-1]["content"])
            return "ANSWER: Flanders" if "Flanders" in msgs[-1]["content"] else "ANSWER: NOT FOUND"

        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "workspace" / "kb"
            kb.mkdir(parents=True)
            for i in range(40):
                (kb / ("festival_%02d.md" % i)).write_text(filler(i))
            (kb / "textiles.md").write_text(LOOM + "\n")
            task = ctx.Task(tmp, {"task_type": "search",
                                  "instruction": "Where was the foot-operated weaving frame from the 1100s first used?"})
            self.assertGreater(ctx.kb_bytes(task), ctx.PROSE_FULL)
            env = {"HARNESS_EMBED_DIR": "/nonexistent"} if broken else {}
            with patch.dict(os.environ, env):
                out = Solver(ask)._embed_look(task, task.instruction, "", 0)
        return out, calls

    def test_unseen_paraphrased_document_is_found(self):
        out, calls = self.look()
        self.assertEqual((out.status, out.answer), ("solved", "Flanders"))
        self.assertEqual(len(calls), 1)

    def test_broken_model_gives_no_look(self):
        out, calls = self.look(broken=True)
        self.assertIsNone(out)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

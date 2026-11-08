"""Behavioral checks for the developer catalog; no SDK, network or real task files."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import knowledge as kb


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / kb.BUNDLE
        self.bundle.mkdir(parents=True)
        (self.root / "runtime.py").write_text("cost = 1\n")
        (self.root / "sandbox.py").write_text("timeout = 30\n")
        self.write_doc("budget", "runtime.py", "Budget accounting", ["budget", "бюджет"],
                       "# Budget rules\n\nCost units are explained here.[^code]\n"
                       "See [isolation](isolation.md).\n\nDo not load this whole body unnecessarily.\n")
        self.write_doc("isolation", "sandbox.py", "Process isolation", ["sandbox"],
                       "# Isolation\n\nResource limits apply to child processes.[^code]\n", kind="Playbook")
        self.index()

    def write_doc(self, name, source, title, tags, body, kind="Reference", extra=None):
        metadata = {"type": kind, "title": title, "description": title + " details", "tags": tags,
                    "sources": [{"id": "code", "resource": "../../" + source,
                                 "sha256": kb.digest(self.root / source)}]}
        metadata.update(extra or {})
        (self.bundle / (name + ".md")).write_text("---\n" + json.dumps(metadata) + "\n---\n" + body)

    def index(self):
        (self.bundle / "index.md").write_text(kb.render_index(kb.documents(self.root)))

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = kb.main(args, repo_root=self.root)
        return status, out.getvalue(), err.getvalue()

    def test_clean_catalog_and_index(self):
        self.assertEqual(kb.check(self.root, kb.documents(self.root)), [])
        status, out, _ = self.command("list")
        self.assertEqual(status, 0)
        self.assertIn("budget [Reference]", out)
        self.assertIn("isolation [Playbook]", out)

    def test_search_ranks_relevant_topic_and_respects_exact_filters(self):
        docs = kb.documents(self.root)
        self.assertEqual(kb.search(docs, "бюджет")[0][1].identifier, "budget")
        self.assertEqual(kb.search(docs, "budget", tag="sandbox"), [])
        self.assertEqual(kb.search(docs, "resource limits", kind="Reference"), [])
        hit = kb.search(docs, "resource limits", kind="Playbook")[0]
        self.assertEqual(hit[1].identifier, "isolation")
        for line, text in hit[2]:
            self.assertIn(text, hit[1].text.splitlines()[line - 1])

    def test_read_is_bounded_and_uses_actual_line_numbers(self):
        doc = kb.documents(self.root)[0]
        status, out, _ = self.command("read", doc.identifier, "--lines", "2")
        self.assertEqual(status, 0)
        self.assertEqual(len(out.splitlines()), 3)  # path/state plus two body lines
        self.assertNotIn("Do not load", out)
        line = doc.body_line + 2
        status, out, _ = self.command("read", doc.identifier, "--start", str(line), "--lines", "1")
        self.assertIn("%d: %s" % (line, doc.text.splitlines()[line - 1]), out)

    def test_source_drift_is_visible_and_check_fails(self):
        (self.root / "runtime.py").write_text("cost = 2\n")
        status, _, err = self.command("check")
        self.assertEqual(status, 1)
        self.assertIn("changed source", err)
        _, out, _ = self.command("search", "budget")
        self.assertIn("CHANGED SOURCES", out)

    def test_stamp_preserves_body_and_extensions_without_asserting_review(self):
        doc = kb.documents(self.root)[0]
        m = dict(doc.metadata, custom={"keep": 1})
        doc.path.write_text("---\n" + json.dumps(m) + "\n---\n" + doc.body)
        (self.root / "runtime.py").write_text("cost = 3\n")
        self.assertEqual(self.command("stamp", "budget")[0], 0)
        updated = kb.documents(self.root)[0]
        self.assertEqual(updated.body, doc.body)
        self.assertEqual(updated.metadata["custom"], {"keep": 1})
        self.assertNotIn("verified", updated.metadata)
        self.assertEqual(kb.check(self.root, kb.documents(self.root)), [])

    def test_metadata_change_requires_index_regeneration(self):
        doc = kb.documents(self.root)[0]
        doc.metadata["title"] = "Changed accounting contract"
        doc.path.write_text("---\n" + json.dumps(doc.metadata) + "\n---\n" + doc.body)
        self.assertIn("index.md is out of date", "\n".join(kb.check(self.root, kb.documents(self.root))))
        self.assertEqual(self.command("index", "--write")[0], 0)
        self.assertEqual(self.command("check")[0], 0)

    def test_broken_links_and_unknown_source_footnotes_fail(self):
        with (self.bundle / "budget.md").open("a") as f:
            f.write("\n[Missing](missing.md) and unknown attribution.[^unknown]\n")
        status, _, err = self.command("check")
        self.assertEqual(status, 1)
        self.assertIn("broken link", err)
        self.assertIn("unknown source footnotes", err)

    def test_invalid_frontmatter_fails_with_actionable_error(self):
        (self.bundle / "budget.md").write_text("---\ntype: Reference\n---\n# Budget\n")
        status, _, err = self.command("check")
        self.assertEqual(status, 1)
        self.assertIn("JSON (valid YAML)", err)

    def test_local_sources_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "private.py"
            secret.write_text("do not read this content")
            doc = kb.documents(self.root)[0]
            doc.metadata["sources"][0]["resource"] = os.path.relpath(secret, doc.path.parent)
            doc.path.write_text("---\n" + json.dumps(doc.metadata) + "\n---\n" + doc.body)
            status, out, err = self.command("check")
            self.assertEqual(status, 1)
            self.assertIn("Path leaves repository", err)
            self.assertNotIn("do not read this content", out + err)

    def test_read_refuses_non_catalog_path(self):
        self.assertEqual(self.command("read", "../../runtime.py")[0], 1)


if __name__ == "__main__":
    unittest.main()

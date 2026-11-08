---
name: repo-knowledge
description: Find repository contracts, source files and verification commands through the local knowledge catalog; maintain its source fingerprints when documented behavior changes. For development in this repository, not for solving competition tasks.
license: Apache-2.0
---

Use the [knowledge index](../../../docs/knowledge/index.md) to choose a topic.
Read only the matching page and the primary sources needed for the task.

Run from the repository root:

- **List contents:** `python3 tools/knowledge.py list`
- **Search content:** `python3 tools/knowledge.py search QUERY --limit 5`
  Optional exact filters: `--type Reference` and `--tag budget`.
- **Read a file:** `python3 tools/knowledge.py read TOPIC --lines 60`
  For another excerpt, use the returned actual file line numbers with `--start`.
- **Check freshness:** `python3 tools/knowledge.py check`

Search returns matching line snippets rather than whole documents. If sources
changed, inspect those sources before relying on the explanation. Original code,
tests and the selected behavior contract resolve questions that summaries cannot.

When maintaining knowledge, read [catalog maintenance](../../../docs/knowledge/catalog-maintenance.md).
Update prose first, then `stamp TOPIC`; regenerate navigation with `index --write`
if metadata changed. Stamping fingerprints does not confer human review or prove
semantic correctness. Do not add such verification claims without evidence.

Adapted from Google's `kb-search`: the FilesKB MCP list/search/read operations
are supplied here by a local standard-library CLI. No MCP server or Cloud setup
is required. See [NOTICE](NOTICE.md) for upstream attribution and pinned sources.

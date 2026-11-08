---
{
  "type": "Playbook",
  "title": "Catalog maintenance and upstream provenance",
  "description": "Maintain the OKF profile, regenerate navigation and detect source drift locally.",
  "group": "Development",
  "tags": [
    "catalog",
    "knowledge",
    "skill",
    "freshness",
    "каталог",
    "навык"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "tool",
      "resource": "../../tools/knowledge.py",
      "sha256": "8b2e0285821404945e7bc5bf8b8d22ad9bcc644994112761d9b0de78d7a1050b"
    },
    {
      "id": "skill",
      "resource": "../../.agents/skills/repo-knowledge/SKILL.md",
      "sha256": "fe64ca9fb66f5b377f282955f85ade6cc184cff2498bcfa423e5dd84dd95866c"
    },
    {
      "id": "notice",
      "resource": "../../.agents/skills/repo-knowledge/NOTICE.md",
      "sha256": "259db161fdc43ad4a49498051b742fe66046d44f0cba44d7c0c6df21d312979d"
    },
    {
      "id": "ci",
      "resource": "../../.github/workflows/knowledge.yml",
      "sha256": "0c5773094f8670b626d7c7b886aa8a88636542f15b40d96030ffe97c4321b1f0"
    }
  ]
}
---
# Catalog maintenance and upstream provenance

The project skill adapts Google's kb-search list/search/read workflow. Upstream
uses FilesKB MCP; this checkout supplies equivalent local operations through
knowledge.py, without installing Google Cloud or an MCP server.[^skill][^notice]

The bundle follows Open Knowledge Format v0.2: concept Markdown with YAML
frontmatter, typed metadata, sources and metadata-derived index navigation.
The canonical specification moved out of knowledge-catalog/okf; its exact revision
and the adapted skill revision are recorded in NOTICE.[^notice]

Our profile uses JSON frontmatter (valid YAML) for standard-library parsing.
Concepts require type, title, description, tags and sources with stable ids.
Local sources have a sha256 extension. Unknown metadata is preserved. This is a
repository profile checker, not a general validator for arbitrary OKF/YAML.[^tool]

After changing a documented contract:

1. Read the affected topic and changed primary source. Update the prose.
2. Run `python3 tools/knowledge.py stamp TOPIC` to update local fingerprints.
3. Run `python3 tools/knowledge.py index --write` if metadata changed.
4. Run `python3 tools/knowledge.py check` and relevant tests.

Whole-file fingerprints are intentionally conservative. A matching hash means
source bytes are unchanged, not that prose is correct or human-reviewed. Stamp
never adds verified metadata. External references are never fetched by the checker.
Index is generated; do not maintain a second manual list of topic descriptions.[^tool]

Search supports exact type/tag filters. Read returns bounded body excerpts with
actual file line numbers; use --start/--lines to continue. `rg` and direct source
reads remain available when a question goes beyond the catalog.[^tool]

[^tool]: [Local catalog CLI and checker](../../tools/knowledge.py).
[^skill]: [Repository skill](../../.agents/skills/repo-knowledge/SKILL.md).
[^notice]: [Pinned upstream sources and license](../../.agents/skills/repo-knowledge/NOTICE.md).

CI runs the catalog check and its targeted unit tests on pushes and pull requests.[^ci]

[^ci]: [Knowledge workflow](../../.github/workflows/knowledge.yml).

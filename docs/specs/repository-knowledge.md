# Repository knowledge and developer workflow

Status: implemented on this branch; team review pending.

## Problem

Developers and agents need a small starting context that identifies the source of
each contract. Runtime behavior, historical measurements and proposed changes must
remain distinguishable. Adding a skill to the development repository must not
change competition prompts or require Cloud credentials.

## Contract

- AGENTS.md and README lead to a topic index; agents can list, search and read
  bounded excerpts offline using only Python's standard library.
- Each topic names primary sources. Changes to local sources are detected by
  SHA-256 fingerprints; a matching hash does not prove the prose is correct.
- Navigation is generated from concept metadata, avoiding a second hand-maintained
  set of titles and descriptions. Links and metadata are checked in CI.
- The local skill credits and adapts Google's kb-search. Concepts use OKF v0.2
  with JSON frontmatter, a valid YAML subset, and a documented local profile.
- Significant behavior changes can use a short change spec; small fixes can use
  the PR's problem/behavior/verification description. No mandatory model-generated
  specifications or additional permission gates are introduced.
- All developer artifacts remain outside the submitted Docker image.

## Acceptance

`python3 tools/knowledge.py check` passes; targeted catalog tests demonstrate
retrieval, bounded reads, source drift, invalid metadata, broken links and
fingerprint refresh without fabricated verification. The existing offline tests
continue to pass. A skill metadata check and example lookups complete validation.

## Trade-offs

The local CLI supports JSON frontmatter rather than arbitrary YAML, to avoid a
dependency. Whole-file fingerprints are conservative: even an unrelated source
edit may require reviewing a topic. External references are pinned and attributed
but never fetched by the checker. Human review and semantic correctness remain
separate from structural checks. No token-savings percentage is asserted.

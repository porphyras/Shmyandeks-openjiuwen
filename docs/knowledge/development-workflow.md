---
{
  "type": "Playbook",
  "title": "SSOT and change-driven development",
  "description": "Separate current behavior, intended changes, historical results and verification.",
  "group": "Development",
  "tags": [
    "ssot",
    "sdd",
    "spec",
    "development",
    "разработка",
    "спецификация"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "agents",
      "resource": "../../AGENTS.md",
      "sha256": "848a77cfb7de45a54764fa8a156df8cd1fb31cc0da1999b7d82ea6b7ed69ed04"
    },
    {
      "id": "template",
      "resource": "../../docs/templates/change-spec.md",
      "sha256": "538c868f60ba002f713be3bf4af80c0f9a3803550402106b494597e8797d3827"
    },
    {
      "id": "spec",
      "resource": "../../docs/specs/repository-knowledge.md",
      "sha256": "e8609b1c970c71c0fa0f7b596ecbc16dddbcf4863ad35276d055bbdfe57dc3ea"
    },
    {
      "id": "decisions",
      "resource": "../../docs/DECISIONS.md",
      "sha256": "c91f0ab10c814bb4070420d92e6304e1e21eb9df4db7fdcc07487a7ec8add48e"
    }
  ]
}
---
# SSOT and change-driven development

Use one authoritative location for each kind of claim. Current behavior is
established from this checkout's source/tests; intended behavior is the selected
change contract; historical scores remain in the experiment record. If code and
an intended contract disagree, investigate the difference rather than silently
rewriting the contract to fit the code.[^agents][^decisions]

Before a behavioral change, write its concrete problem, expected observable
result and verification. For a bounded fix, the PR description can hold this.
For a substantial change, use the [change-spec template](../templates/change-spec.md).
Describe material trade-offs and how to tell success from a superficially passing
implementation. This is a developer workflow, not another LLM stage per task.[^template]

Implement and verify against that contract, then record the actual evidence and
uncertainty. Update the affected knowledge topic when its contract changes;
inspect its linked sources before refreshing fingerprints. Keep configuration
values and large result tables in their existing canonical locations.[^agents]

The [repository knowledge spec](../specs/repository-knowledge.md) is the first
worked example. Its implementation status belongs to this branch; an open PR or
an unmerged experiment must not be represented as current runtime behavior.[^spec]

[^agents]: [Developer and agent entry point](../../AGENTS.md).
[^template]: [Change contract template](../templates/change-spec.md).
[^spec]: [This catalog's behavior contract](../specs/repository-knowledge.md).
[^decisions]: [Historical decisions](../DECISIONS.md).

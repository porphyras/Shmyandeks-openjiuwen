---
{
  "type": "Reference",
  "title": "Solver paths and local checks",
  "description": "Find the math, data, knowledge-base and code-repair paths and their validators.",
  "group": "Runtime",
  "tags": [
    "solver",
    "coding",
    "math",
    "pandas",
    "kb",
    "fastpath",
    "решатель"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "solver",
      "resource": "../../harness/solver.py",
      "sha256": "9d6fc5b8a2b89db3f4ba5cf084b2c427ee42ba7e8314ab5336d0fbe4e1476d42"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "bf5c21379efa6360f33a1a7d4f4c18bedd569a9d282680237d3d632c84042036"
    },
    {
      "id": "fast",
      "resource": "../../harness/fastpath.py",
      "sha256": "3c8979d74b20b7ad5ac2bea9570e05350ea3ae91ae62b2d6006135a25ee1d792"
    },
    {
      "id": "crossfix",
      "resource": "../../harness/crossfix.py",
      "sha256": "266453494ba879ccea6c54f44dc017b17ad98c8e76d40571560f995ad199f54f"
    }
  ]
}
---
# Solver paths and local checks

Solver.prepare builds a compact context and feasibility signals. Inspect the
kind-specific context builder before adjusting prompts or adding another model
call; the required information may already be available locally.[^context]

| Kind | Model artifact | Local check |
|---|---|---|
| math | Python program | execute and inspect scalar output |
| data | pandas expression/program over loaded tables | execute against task files |
| search | answer, optionally a short fact chain | match a span in the selected base; numeric exceptions exist |
| coding | changed function definitions | merge into sources and run discovered tests |

The exact prompts, reply parsing, retries and exceptions live in Solver.
A test-success verdict controls DONE; it is not proof of hidden-test correctness.
Inspect _run_tests_once when extending supported test styles.[^solver]

fastpath answers only recognized task forms. A new form must have tests against
nearby unsupported or ambiguous wording. crossfix reuses definitions that passed
named tests elsewhere, with signature/docstring/edit-size guards and target tests;
it is different from guessing a mutation until visible tests pass.[^fast][^crossfix]

See [task lifecycle](task-lifecycle.md) for output placement and
[testing](testing-and-evidence.md) for positive and negative evidence.

[^solver]: [Prompts, merging and checks](../../harness/solver.py).
[^context]: [Context and feasibility inspection](../../harness/context.py).
[^fast]: [Deterministic task parsers](../../harness/fastpath.py).
[^crossfix]: [Cross-task code reuse](../../harness/crossfix.py).

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
      "sha256": "39b0c671a78fa3b7bd487b2f0fc8185ccef9e3c09a13772fe362d47629458e80"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "68231b1eb908e707362e041ee5107f12574da94246bae2fc60be484f5dea809a"
    },
    {
      "id": "fast",
      "resource": "../../harness/fastpath.py",
      "sha256": "1c348fb0fc7b7cabf9cec81eb62121aaf1f2ec335904ca02b9b8a7e752bea6e1"
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
It discovers unittest classes without a main entry point and plain test functions;
an empty suite cannot establish success. Test contradictions do not infer
permutation invariance from function names alone.[^solver][^context]

fastpath answers only recognized task forms. A new form must have tests against
nearby unsupported or ambiguous wording. crossfix reuses definitions that passed
named tests elsewhere, with signature/docstring/edit-size guards and target tests;
it is different from guessing a mutation until visible tests pass.[^fast][^crossfix]
Over a card base, a free "infeasible" or a head count needs every card value to
be bare (a year, a plain name, one lowercase word): "Note: Dana Kim is mentored
by Carla Diaz" or "Role: engineer at Delta Foods" may state the asked fact under
another label, so such a base goes to the model.[^fast]

Card questions can combine forward Mentor links with a unique inverse link
("the person mentored by ..."). Inverse chains require a complete, bounded
markdown snapshot in the task working directory and a supported card schema.
Ambiguous identities, missing links, unsupported data and read limits fall back
to the model; this path never returns an infeasible verdict. Small-batch model
confirmation still follows the normal run policy.[^fast]

See [task lifecycle](task-lifecycle.md) for output placement and
[testing](testing-and-evidence.md) for positive and negative evidence.

[^solver]: [Prompts, merging and checks](../../harness/solver.py).
[^context]: [Context and feasibility inspection](../../harness/context.py).
[^fast]: [Deterministic task parsers](../../harness/fastpath.py).
[^crossfix]: [Cross-task code reuse](../../harness/crossfix.py).

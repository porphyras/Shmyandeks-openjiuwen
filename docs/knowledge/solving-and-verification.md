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
      "sha256": "134b31d388b066c1c7cebd294d31d028308ea3d30245176472db4c352f492696"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "8881824659d6dcc3f55bbe529de3bb299caba230317cd96b53e61db5a875645a"
    },
    {
      "id": "fast",
      "resource": "../../harness/fastpath.py",
      "sha256": "b7893b8e5fd64e61805431b02df2cf0d596ce050afe75cc0d21dfb167c7c4b20"
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
A search that ends in NOT FOUND after every look gets one targeted look
(HARNESS_ENTITY_LOOK, on by default): entity_passages gathers the paragraphs
about the names the last reply reached, in any written form ("J. Muller"), with
the paragraph after each mention and documents named after the entity. Only an
answer found verbatim in those paragraphs (or a number for a counting question)
is accepted; otherwise the verdict stays infeasible.[^solver][^context]
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
another label, so such a base goes to the model. The same holds for a base with
any card key outside the known schema (Born, Employer, Mentor, Headquarters,
Founded, Role, Hometown, Industry): "DOB", "Firm" or "Supervisor" may carry the
asked fact.[^fast]

Tests that pass before any change give a free DONE, except when the instruction
asks for new code (add or implement a function, method or class; hidden tests).
A math program's int(), math.floor() and math.ceil() take a float within
rounding noise of an integer as that integer (72 / 0.72 prints 100, not 99).
Tables skip comment and blank lines before the header. A column announced in
"The file X has columns ..." but absent from the file makes the task infeasible
only when the question itself uses it; otherwise it is a hint for the model.[^solver][^context]

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

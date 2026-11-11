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
      "sha256": "0017e038780d1305f64b179112f5004b67ccb27876a84f8a59fd18ffa4e14525"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "9842ba74b65e046d606a6e26a132f0bf523011fee60f448cd9062b10944a10b3"
    },
    {
      "id": "fast",
      "resource": "../../harness/fastpath.py",
      "sha256": "2d66eddd426ece64701b00de6e690ff8da50c4938201733c3088768bb452ec7e"
    },
    {
      "id": "crossfix",
      "resource": "../../harness/crossfix.py",
      "sha256": "ec67e8c77f92fe5f806a1936b3d5d503c9992f0f3864d68459217e949692ff81"
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
A test file that needs pytest (imports it, takes fixture or parametrize arguments, or
has a conftest.py beside it or at the task root) runs under the image's pytest from the
task root; success needs a passed test, and failing names come from its FAILED lines.
Other files run as scripts; the fallback runner discovers unittest classes without a
main entry point, plain test functions and pytest-style Test* classes (no TestCase base,
no __init__; setup_class/setup_method/teardown_method), also when unittest.main() in
the file would skip them, and an empty suite cannot establish success. A missing-file verdict needs a literal read (open, or
Path(...).read_text) with no earlier call in its test, setUp hook or module code that
could create the file, a name absent from the sources and not passed to a call. Test contradictions do not infer
permutation invariance from function names alone. They also catch a pure call
required to be both true and false, a builtin over one (len(f(x)) as 3 and 4) and a
result kept in a variable that nothing else touches before the assertions. A function
counts as pure only when proven so (unproven_functions): a plain module-level def without
decorators or mutable defaults that reads only its arguments, pure builtins and modules,
other proven defs and scalar constants never rebound; registries, globals, class
attributes and closures disable the verdict. A test that writes through the module
(mock.patch, setattr, m.A.b = ..., vars(m), reload) gives none. A method call obj.m() required
to equal two values counts only when pure_methods proves m leaves its object unchanged
(reads self and its data - values/keys/items, get with a key - calls only safe builtins,
pure modules, proven helpers of any source file or other such methods; no class with a
base outside the sources). Skipped and xfail tests are
left out of every check. A data program's KeyError on a name from the question is retried
once with the table columns before the task is called infeasible.[^solver][^context]

fastpath answers only recognized task forms. A new form must have tests against
nearby unsupported or ambiguous wording. crossfix reuses definitions that passed
named tests elsewhere, with signature/docstring/edit-size guards and target tests;
it is different from guessing a mutation until visible tests pass. A variant must keep
what the definition does besides the bug: guards (if/raise/try/with/assert/loops,
comprehension conditions), keyword arguments, sort-key tuples and lambdas, positional
arguments of calls that stay, and every call unless one of its family replaces it
(upper -> lower); otherwise the task goes to the model.[^fast][^crossfix]
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

---
{
  "type": "Playbook",
  "title": "Verification and evidence",
  "description": "Choose offline tests, generated-set grading, proxy comparisons or Docker checks.",
  "group": "Development",
  "tags": [
    "tests",
    "bench",
    "score",
    "evidence",
    "проверки",
    "тесты"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "sets",
      "resource": "../../bench/README_sets.md",
      "sha256": "b19d20a4397129e548146eb1f6c1a3428a39fba4e23a121098eea8cded67b291"
    },
    {
      "id": "local",
      "resource": "../../bench/run_local.sh",
      "sha256": "a8224368cdf9de3915eb7409c96c95f284ec49ee3226f1494d172030915eab70"
    },
    {
      "id": "judge",
      "resource": "../../bench/judge_sim.sh",
      "sha256": "9e379ed195c4de440316d4a55d3b73a0e915292aa9e7e039e010eafb8b5cbaa1"
    },
    {
      "id": "decisions",
      "resource": "../../docs/DECISIONS.md",
      "sha256": "c91f0ab10c814bb4070420d92e6304e1e21eb9df4db7fdcc07487a7ec8add48e"
    },
    {
      "id": "tests",
      "resource": "../../tests/test_free_verdicts.py",
      "sha256": "7efe7b43b9f9c92c4039159d44e3ca77ee2883c1e40a82e74644f079af5b0773"
    }
  ]
}
---
# Verification and evidence

From the repository root, with Python 3.12 and the pinned dependencies installed
(`pip install -r requirements.txt`: run tests import openJiuwen's Workflow), the
offline checks need no model credentials; CI runs the same:

```sh
python3 -m unittest discover -s tests -v
python3 tools/knowledge.py check
```

Read the targeted tests before adding a new one. Existing free-verdict tests
illustrate the contract: an unknown wording is not evidence of infeasibility.[^tests]

bench/README_sets.md owns generator commands, set layout, keys, hidden tests and
grading semantics. Keep answer keys outside the task inputs and submission image.
A generated-set score is a regression result, not a hidden-competition forecast.[^sets]

run_local.sh copies a set and code into a named bench/runs directory, starts a
stub and optionally forwards to the real proxy. It replaces the named run directory:
use a new run name when preserving evidence. Forward mode reads credentials from
an external local file; inspect the script rather than copying secrets into docs.[^local]

judge_sim.sh builds/runs Docker with judge-style flags. Use it for SDK imports,
read-only execution and container behavior. It requires Docker, dependencies and
appropriate local setup; offline unit tests require none of those.[^judge]

DECISIONS.md records previous experiments and hold-out limitations. New results
should identify code revision, set/seed, mode, actual spend and solved reward.
Distinguish stub/oracle mechanics, real model quality and container evidence.[^decisions]

[^tests]: [Offline free-verdict checks](../../tests/test_free_verdicts.py).
[^sets]: [Bench sets and grading](../../bench/README_sets.md).
[^local]: [Host-run recipe](../../bench/run_local.sh).
[^judge]: [Container-run recipe](../../bench/judge_sim.sh).
[^decisions]: [Historical evidence and rejected approaches](../DECISIONS.md).

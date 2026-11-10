---
{
  "type": "Reference",
  "title": "Task intake and outputs",
  "description": "How a task batch becomes separate model requests and persisted answers.",
  "group": "Runtime",
  "tags": [
    "tasks",
    "workspace",
    "intake",
    "answer",
    "задачи",
    "вход"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "run",
      "resource": "../../run_agent.py",
      "sha256": "cb8f82ece57b7a67814204e3c9049353b086e65ebf2b75af574ad6d365d3c0a8"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "8881824659d6dcc3f55bbe529de3bb299caba230317cd96b53e61db5a875645a"
    },
    {
      "id": "io",
      "resource": "../../harness/io_utils.py",
      "sha256": "7d0cffeec49b9fdc288282057ff13a16d0a5ed14c6f857097a5184fe637a0258"
    }
  ]
}
---
# Task intake and outputs

The harness receives a directory of task folders. Each has task.json with
instruction, task_type, task_id and reward; workspace/ may hold input files.
Task.cwd selects the workspace when usable. Task.instruction retains the
original text; short_instruction removes known boilerplate and caps long text.[^context]

Run.run loads the batch, inspects tasks, attempts free solutions and queues model
work by expected reward per cost. The Python process sees the batch; each solver
constructs messages for an individual task. There is no shared batch-wide model
conversation. Cross-task code reuse is an explicit crossfix operation.[^run]

A math or data answer is written with exactly the decimals the task asks for
(padding only, or Python-style rounding of extra digits; half up when stated).
The output contract is answer.txt in the task directory and, when present, its
workspace. Coding tasks also leave modified source files. Summary optionally
writes solutions.json and token_estimates.json. Per-task answers are persisted
as work finishes and rewritten from memory at the end.[^io]

When changing intake, inspect Task, load_tasks, Run.run and finish_task together.
Preserve the difference between original task data and a prompt projection.
See [solvers](solving-and-verification.md) and [budget admission](budget-and-routing.md).

[^context]: [Task and file inspection](../../harness/context.py).
[^run]: [Runtime orchestration](../../run_agent.py).
[^io]: [Answer and summary writing](../../harness/io_utils.py).

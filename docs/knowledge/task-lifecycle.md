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
      "sha256": "855bc3de3640d3582942a4ec55defbb034665fa51f0c6ecb83e9bc6c7fd66609"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "68231b1eb908e707362e041ee5107f12574da94246bae2fc60be484f5dea809a"
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

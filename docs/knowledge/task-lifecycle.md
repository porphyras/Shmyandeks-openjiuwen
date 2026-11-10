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
      "sha256": "1b46eec64e65d6d0fc3e031a905ffe767cb8d7cb5dbdba358db1a12a4ea156dc"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "8aae40824677613288d1847936eebf4199d6437d06a0b3ef7ccf2e8529308e05"
    },
    {
      "id": "flow",
      "resource": "../../harness/oj_flow.py",
      "sha256": "9357f81d4dad4356158006cef78853d08c8ea83f439902bac9de0d153a5e6182"
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
Each scheduled task runs as an openJiuwen Workflow of three nodes (inspect, solve,
finish); the workflow state carries only the task name, its rung and the flags
go/stop/defer, while tasks, inspections and outcomes stay in Run's registries.
The SDK's 60-second workflow timeout is disabled; the harness keeps its own.[^flow]

A math or data answer is written with exactly the decimals the task asks for its
answer (padding only, or Python-style rounding of extra digits; half up when
stated); a sentence describing the data ("prices are stored with 2 decimal
places") does not count, and a question asking for a whole number keeps it.
The output contract is answer.txt in the task directory and, when present, its
workspace. Coding tasks also leave modified source files. Summary optionally
writes solutions.json and token_estimates.json. Per-task answers are persisted
as work finishes and rewritten from memory at the end.[^io]

When changing intake, inspect Task, load_tasks, Run.run and finish_task together.
Preserve the difference between original task data and a prompt projection.
See [solvers](solving-and-verification.md) and [budget admission](budget-and-routing.md).

[^context]: [Task and file inspection](../../harness/context.py).
[^flow]: [The per-task openJiuwen Workflow](../../harness/oj_flow.py).
[^run]: [Runtime orchestration](../../run_agent.py).
[^io]: [Answer and summary writing](../../harness/io_utils.py).

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
      "sha256": "6bbd94a7d79b8809799b2ca4017f9b62657bb2ee480f08546886292b33641e87"
    },
    {
      "id": "context",
      "resource": "../../harness/context.py",
      "sha256": "36cb5107cad14efe6b3337951a1ed93de9ed3cbb1c3491a46b9eb933e785f2af"
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

load_tasks also looks one or two levels deeper when the mounted folder has no task.json
(an unpacked archive), and archive/OS litter (.DS_Store, ._x, __MACOSX, .git) is never
part of a task's files; a crossfix index that fails to build is replaced by an empty one.
Run.run loads the batch, inspects tasks, attempts free solutions and queues model
work by expected reward per cost. The Python process sees the batch; each solver
constructs messages for an individual task. There is no shared batch-wide model
conversation. Cross-task code reuse is an explicit crossfix operation.[^run]
Each scheduled task runs as an openJiuwen Workflow of three nodes (inspect, solve,
finish); the workflow state carries only the task name, its rung and the flags
go/stop/defer, while tasks, inspections and outcomes stay in Run's registries.
The SDK's 60-second workflow timeout is disabled; the harness keeps its own.[^flow]
Inspection runs in parallel up to a deadline (30-240 s); a task not inspected by then
is queued last and its inspect node awaits the inspection still running (up to
LATE_INSPECT_WAIT, then the task is skipped) or runs one that was cancelled before it
started, so a task folder is never inspected twice at once.[^run]

A math or data answer is written with exactly the decimals the task asks for its
answer (padding only, or Python-style rounding of extra digits; half up when
stated); a sentence describing the data ("prices are stored with 2 decimal
places") does not count, and a question asking for a whole number keeps it.
An Excel workbook is shown as text (sheets, the header row found, first rows and the
pd.read_excel call that loads it); parquet/feather files are named as not readable here.
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

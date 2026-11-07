# Shmyandeks — budget-aware task harness on openJiuwen

The harness gets a directory of tasks, sees every task and its reward at once, and spends a
fixed token budget on the tasks that promise the most reward per token. Every task is solved
by the model; plain code only gathers context, runs what the model wrote and checks the result.

## Build and run

```bash
docker build -t hack:latest .
mkdir -p "$OUT" && chmod 777 "$OUT"
timeout -k 1000s 1h docker run --rm --name "run-${TEAM}" \
  --network hack-net -e LLM_BASE_URL -e LLM_API_KEY \
  -v "$TASKS:/data/tasks:rw" \
  --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --cpus 8 --memory 16g --pids-limit 512 \
  --workdir /app --entrypoint python \
  hack:latest /app/run_agent.py --tasks-dir /data/tasks
```

- Base image `python:3.12.13-slim-bookworm` pinned by digest; `openjiuwen==0.1.20`; all
  dependencies pinned in `requirements.txt` (compiled for linux x86-64). Nothing is
  downloaded at run time.
- Works under a read-only root: openJiuwen's file logging (which by default creates
  `./logs/` on import) is switched to console-only before any other openJiuwen import;
  byte-code writing is disabled; temporary scripts go to `/tmp`.
- Arguments are parsed leniently: `--tasks-dir` (or env `TASKS`, default `/data/tasks`),
  optional `--out/--output` (or env `OUT`), optional `--budget` (or env `TOKEN_BUDGET`);
  unknown arguments are ignored.

### Output

- `answer.txt` is written right after each task, atomically (temp file in the same directory,
  then `rename`), both into the task directory and into its `workspace/` if it exists
  (the task text says "current directory", which is `workspace/` for tasks that have one).
- If `OUT` is given: a directory gets `solutions.json` (`{task_id: answer}`) inside, any other
  path is used as the file itself; it is rewritten atomically after each solved task. If `OUT`
  is not set or not writable, this step is skipped.
- `SIGTERM`/`SIGINT` and a watchdog end the run promptly; everything solved is already on disk.

## How a task is solved

1. **Free inspection (no LLM).** For every task: the instruction is stripped of boilerplate
   about tools and `answer.txt`; the workspace is summarised compactly:
   - CSV files: header, row count, three rows, distinct values of low-cardinality columns,
     numeric ranges (never the whole file);
   - knowledge bases: only the documents that mention the entities of the question, plus the
     documents they link to by name (up to three hops); the whole base only as a fallback;
   - code: the tests are run first; the model sees the source, the failing tests and a
     compressed test log.
2. **One short call** to a no-reasoning model with a tiny prompt. The model replies with a
   short Python program (computations, data questions), `ANSWER: <value>` (knowledge-base
   questions) or only the functions it changed (code fixes).
3. **Free verification.** Programs are executed in the task directory (clean environment
   without `LLM_*` variables, timeout, output cap, memory limit); code fixes are merged into
   the file by function name and the tests are re-run (test files are hash-checked and never
   edited); knowledge-base answers must occur verbatim in the base.
4. **Retry / escalation** only on a failed check: one retry with the error text on the next
   model of the ladder, and a final pass for still-failed tasks if budget and time remain.

## Token accounting and task selection

- Cost of a call is counted the organisers' way: `prompt_tokens / 10 + completion_tokens`
  (reasoning tokens are part of `completion_tokens` on the proxy; a separately reported larger
  reasoning count would be added), no cache discount.
- Each task gets an expected cost before any call: context size (chars → tokens) / 10 plus the
  typical output length of its kind, times the expected number of attempts; and a success
  probability by kind, lowered sharply when the free inspection finds a problem.
- Tasks are processed greedily by `reward × p(success) / expected cost` with up to 5 parallel
  sessions. With an unknown budget this order is near-optimal because every task costs well
  under 1 % of the budget; when a budget is given, tasks that no longer fit are skipped in
  favour of cheaper ones. When the proxy stops answering (budget exhausted), the run stops
  and keeps everything already written.

## Infeasible tasks

Detected without spending tokens where possible:
- data: a quoted value right after a column name ("product 'gizmo'") does not occur in that
  column, or announced columns are missing from the file;
- knowledge base: an entity of the question is not mentioned anywhere in the base;
- code: the tests contradict each other — a static check over the test file collects every
  `assertEqual(call, literal)`, flags the same call required to equal two different values,
  and evaluates the remaining assertions about those calls (`len(...)`, comparisons,
  `assertTrue/False`, `assertIn`, ...) with the required values substituted; tests importing
  a module that exists neither in the standard library nor in the workspace;
- programs: a model-written program that divides by zero (split into 0 parts, empty selection).

Otherwise the model may answer `IMPOSSIBLE` / `NOT FOUND`, which stops work on the task
(no retries). Code fixes that still fail after two attempts are not escalated further.
Infeasible tasks get no `answer.txt` (configurable with `HARNESS_INFEASIBLE_ANSWER`).

## Robustness

- Broken or hostile task files never stop the run: every task is loaded and inspected in
  isolation; non-regular files (FIFOs, devices) and symlinks leading out of the task are
  ignored; prompt sizes are capped; inspection has a global deadline after which remaining
  tasks are inspected lazily.
- Programs written by the model run with a clean environment (no `LLM_*`), in their own
  process group, with a timeout, memory and file-size limits; their output goes to capped temp
  files. The proxy key is not even in the harness's own process environment (the harness
  re-executes itself and receives the key through a pipe), and it is redacted from all output.
- Test files and fixed sources are snapshotted before any model call and restored if anything
  changes them; all answers are rewritten from memory at the end of the run.
- Proxy errors: rate limiting (429) is waited out; key/quota refusals or a proxy that stops
  answering end the run cleanly; transient errors are retried a bounded number of times.

## Bench and trajectories

`bench/` holds the local judge replica and training data tools:
- `stub_proxy.py` — OpenAI-compatible stand-in for the organisers' proxy: counts tokens the
  same way, stops answering when the budget is spent; relays to the real proxy or answers from
  an oracle file (to test the mechanics for free);
- `judge_sim.sh` — builds the image and runs it with the judge's exact flags against the stub;
- `gen_tasks.py` / `grade.py` — generator of training sets in the format of the examples
  (all four kinds plus infeasible variants, hidden tests for code) and the grader.

## Open questions to the organisers

Handled defensively in the code: location of `answer.txt` (both places), `OUT` as file or
directory, unknown budget, unknown answer for infeasible tasks.

# Shmyandeks — budget-aware task harness on openJiuwen

The harness gets a directory of tasks, sees every task and its reward at once, and spends a
fixed token budget on the tasks that promise the most reward per token. Plain code answers only
what it can settle with certainty (a statement understood sentence by sentence, a code task
repaired with functions verified in other tasks); everything else is solved by the model, with
plain code gathering the context, running what the model wrote and checking the result.

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
  optional `--out/--output` (or env `OUT`), the token budget from `--budget`, env `BUDGET`
  (how the organisers pass it, e.g. `BUDGET=100000`) or env `TOKEN_BUDGET`, in that order;
  unknown arguments are ignored. The proxy does not report the remainder: the harness counts
  every call the organisers' way and stays within the budget.

### openJiuwen components used

- `openjiuwen.core.foundation.llm.model.init_model` / `Model.invoke` (OpenAI-compatible client
  configured with `ModelClientConfig`/`ModelRequestConfig`) for every model call: one client per
  model, a single persistent event loop, `usage_metadata` (input, output, reasoning tokens) read
  from each `AssistantMessage` for the budget ledger;
- `openjiuwen.core.common.logging.log_config.configure_log_config` to keep the framework's
  logging off the read-only filesystem and out of stdout.

The harness does not use openJiuwen's agents or tool schemas on purpose: every task is solved
with one compact prompt, and tool schemas or conversation history would multiply the input
tokens the budget is counted in.

### Output

- `answer.txt` is written right after each task, atomically (temp file in the same directory,
  then `rename`), both into the task directory and into its `workspace/` if it exists
  (the task text says "current directory", which is `workspace/` for tasks that have one).
- If `OUT` is given: a directory gets `solutions.json` (`{task_id: answer}`) inside, any other
  path is used as the file itself; it is rewritten atomically after each solved task. If `OUT`
  is not set or not writable, this step is skipped.
- `SIGTERM`/`SIGINT` and a watchdog end the run promptly; everything solved is already on disk.

## How a task is solved

0. **Deterministic fast path (no model).** The organisers allow solving without the model.
   A task is answered by plain code only when every sentence of its statement matches a known
   form with an unambiguous meaning and the data it refers to checks out: warehouse stock
   computations built from the example operations (ship per day, deliveries, split and keep a
   share rounded down, double/triple, explicit percent rules); questions over the sales table
   (units/revenue/orders/maximum by product, region, month range) after verifying the file,
   its header and its row count, with a non-empty selection; knowledge-base questions over a
   base made only of "# Name" + "Key: Value" cards, following the named fields (born,
   employer, headquarters, founded, mentor) with every card present. Anything else — an
   unknown sentence, a missing card, an absent value, an empty selection, a negative stock —
   goes to the model path below, so a misparse costs tokens, never a wrong answer. Over the
   bench runs the fast path gave 1,224 answers on 213 distinct tasks with zero errors (it
   never fires on the paraphrased and hard sets); on sets in the examples' format it covers
   about half of the tasks. Code tasks are repaired without the model when the same
   functions appear in other code tasks of the run (the two code examples share four
   functions, each with a different one broken): a definition whose own tests passed in another
   task replaces the broken one only if it is a small edit of the local definition with the same
   signature and docstring (a planted bug changes the body, not the specification), every name
   it uses exists in the target module, a test named after a class method swaps that method
   only, and every visible test then passes. On the bench this fixed 11 of 20 code tasks of
   train_b, 20 of 20 of a second generator's set and 9 of 16 of the paraphrased set, with the
   hidden tests passing every time and no `DONE` on an unsolvable task; train_b fell from ~5.1k
   to ~4.3k units. On small sets
   (the qualification run) the model path still runs through openJiuwen as a confirmation.

1. **Free inspection (no LLM).** For every task: the instruction is stripped of boilerplate
   about tools and `answer.txt`; the workspace is summarised compactly:
   - tables (CSV/TSV, any of `, ; tab`, UTF-8 or cp1251, decimal comma detected): header,
     row count, three rows, distinct values of low-cardinality columns, numeric ranges (never
     the whole file);
   - knowledge bases of fact cards ("Key: Value" lines): only the documents that mention the
     entities of the question, plus the documents they link to by name (up to three hops); the
     whole base only as a fallback;
   - knowledge bases of prose: sent whole up to 14k characters; larger ones are cut into
     paragraphs and the best ones picked by BM25 in two rounds (by the question, then by the
     capitalised names the first-round paragraphs mention — the bridge entities of a
     multi-hop question), up to 9k characters; the model answers with a short chain of facts;
     when it ends with "not found", the next look is picked with the names its reply reached,
     and on a third look the own articles of those entities come first (the chain usually
     stops one hop short: "... mentored by X; X's employer is not in the text");
   - code: the tests are run first; the model sees the source, the failing tests and a
     compressed test log.
2. **One short call** to a no-reasoning model with a tiny prompt; the model returns an
   artefact the harness can check, never free text:
   - computations: a Python program, one assignment per quantity of the statement, no
     comments (comment-free programs that collapse steps lose rounding details; one variable
     per quantity keeps them and costs ~25 % fewer tokens than commented code);
   - data questions: the harness loads every table with pandas (separator, encoding and
     decimal mark detected; text columns that are numbers in disguise — "1,250", "$12.50" —
     converted, blanks/NA as NaN; when the statement calls a column `unit_price` and the file
     header says `unit price`, the prompt names the pair) and the model writes one pandas expression over `df` (a short
     pandas program when there are several tables) — about half the tokens of a full program;
   - knowledge-base questions: a single line `ANSWER: <value>` (or `NOT FOUND`); for prose a
     short chain of facts first (also on a card base when the one-line reply ran out of tokens
     explaining instead of answering - such a reply is never taken as "not found");
   - code fixes: only the functions it changed; the prompt shows the source and the
     compressed test log (the failing assertion lines are in the traceback, so the test file
     itself is not sent).
3. **Free verification.** Programs are executed in the task directory (clean environment
   without `LLM_*` variables, timeout, output cap, memory limit); code fixes are merged into
   the file by function name and the tests are re-run (test files are hash-checked and never
   edited; a method sent without its class goes back to the class that defines it, and when a
   base class and its overrides all define it, each placement is tried against the tests;
   files a test run creates are removed after it, so every run - and the checker's run of the
   hidden tests on the final workspace - starts from the original file set); knowledge-base answers must occur verbatim in the base (and take its spelling),
   except numbers computed for counting/difference questions.
4. **Second opinion for risky computations.** A computation answer is re-derived by the
   other no-reasoning model when the evidence says the first program may be a shortcut: it
   needed control flow or imports, the statement is long or algebraic, the program contains
   numbers the statement never mentions (computed mentally) or ignores a number the
   statement gives (a transcription slip). On disagreement one reasoning call decides by
   majority. Plain step-by-step arithmetic is never re-checked. On a hard math set
   (MATH level 5, GSM8K, SVAMP, ASDiv) this lifted 53/60 to 56–58/60; on example-style
   sets it costs ~20–30 % more tokens on computation tasks only.
5. **Retry / escalation** only on a failed check: one retry with the error text (and a
   warmer temperature) on the next model of the ladder; a knowledge-base question gets a
   second, wider look (whole base or more paragraphs, also following the names the first
   reply reached). Code fixes that still fail get a final pass on the reasoning model (two
   attempts); technical errors are retried at the end. A wrong guess costs nothing, so the
   best available answer is always written — except `DONE` for code, which needs passing tests.

Small sets (up to 10 tasks — the qualification run has 2 feasible tasks) are solved "for
sure": no free infeasibility verdicts, refusals are retried and escalated, and every
answer without a free check gets an independent second opinion.

## Token accounting and task selection

- Cost of a call is counted the organisers' way: `prompt_tokens / 10 + completion_tokens`
  (reasoning tokens are part of `completion_tokens` on the proxy; a separately reported larger
  reasoning count would be added), no cache discount.
- Each task gets an expected cost before any call: context size (chars → tokens) / 10 plus the
  typical output length of its kind, times the expected number of attempts; and a success
  probability by kind, lowered sharply when the free inspection finds a problem.
- Tasks are processed greedily by `reward × p(success) / p90 cost forecast` with up to 5 parallel
  sessions. With an unknown budget this order is near-optimal because every task costs well
  under 1 % of the budget; when a budget is given, tasks that no longer fit are skipped in
  favour of cheaper ones, and the remainder is checked before every call. Proxy errors never
  end the run on their own (failed calls cost nothing): each call is retried with backoff for
  up to three minutes, then the task waits for a later pass; only an explicit out-of-quota
  answer stops new calls. Everything solved is already on disk at any moment.
- Economy mode: when a known budget is less than 1.25× the forecast of the whole queue, second
  opinions and reasoning retries are switched off so that first passes get the budget.
- Replaying the real traces of the bench runs offline under hard budgets
  (`bench/tools/budget_sim.py`, policy vs a 0/1-knapsack oracle that knows every outcome and
  cost in advance): on sets in the examples' format the policy reaches 92–100 % of the oracle
  from 1000 units up and the full score from 5000; on the hard sets 54–87 % of the oracle at
  2000–20000 units (adv: 100 % from 5000). Ordering by the p90 forecast beat the median forecast under tight budgets.

## Infeasible tasks

Detected without spending tokens where possible:
Free verdicts are reserved for signals that cannot fire on a solvable task (checked on ten
bench sets, including pairs of a solvable task and its minimally edited unsolvable twin:
zero false verdicts):
- data: columns announced in the exact form of the examples ("The file X has columns a,b,c
  (N rows)") are missing from that file; every table is empty and the question asks for an
  average or an extreme; no table exists at all where a file is named;
- code: the tests contradict each other. A static check over the test file (no test code is
  executed — a tiny interpreter evaluates literals, comparisons and a few pure builtins)
  flags the same pure call required to equal two different values (calls compared by their
  evaluated arguments; also `assertAlmostEqual`, `assertIsInstance` with incompatible types,
  `x > x` on a pure call, or one unchanged object asked for two values), and evaluates the
  other assertions about those calls with the required values substituted. Functions with
  state — module-level data they change, `self` attributes they change, counters, random or
  time — are never treated as pure. Also: tests importing a module or submodule that exists
  nowhere (optional imports inside `try/except ImportError` are ignored), tests reading a
  file nobody creates;
- programs: a model-written program that divides by zero on a computation, or fails on a
  column the task names but no table has.

Weaker signals (a quoted value missing from its column, an entity not found by name) only
lower the planner's success estimate: the model looks once and may answer `IMPOSSIBLE` /
`NOT FOUND`, which stops work on the task. Infeasible tasks get no `answer.txt`
(configurable with `HARNESS_INFEASIBLE_ANSWER`).

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
- Proxy errors: rate limits and outages are waited out with backoff (failed calls are free);
  only an explicit out-of-quota answer stops new calls; a base URL given without `/v1` or with
  a trailing `/chat/completions` is normalised.
- As PID 1 of the container the harness acts only on signals from outside the container;
  signals sent by programs it runs are ignored. Children start through a tiny exec launcher
  (no Python code between fork and exec in a multi-threaded process).

## Security model

- The image contains only the harness (`run_agent.py`, `harness/`) and pinned packages; the
  bench is excluded by `.dockerignore`. Nothing is downloaded at run time; the only network
  peer is `LLM_BASE_URL`, reached through the openJiuwen client.
- Environment read: `LLM_BASE_URL`, `LLM_API_KEY`, `TASKS`, `OUT`, `BUDGET`, optional `TOKEN_BUDGET` and
  `HARNESS_*` tuning switches. Files touched: the task directory tree and `/tmp`.
- The harness re-executes itself once at start so that the proxy key is no longer in its own
  initial environment (`/proc/<pid>/environ` of the processes it starts); the key arrives
  through an inherited pipe. `HARNESS_NO_REEXEC=1` disables this.
- Signals: an external SIGTERM/SIGINT (from outside the container's PID namespace, as sent by
  `docker stop`/`timeout`) always ends the run at once; only signals sent by the harness's own
  child processes are ignored.
- The process runs as the image's default user (root): the judge's task directory must stay
  writable; isolation comes from the judge's flags (read-only root, `--cap-drop ALL`,
  `no-new-privileges`, pids limit). The harness adds no seccomp profile of its own.
- Writes: `/tmp` (trace, temporary scripts) and the task directories (`answer.txt`, the fixed
  source of code tasks, restored test files). `answer.txt` is created with mode 0666 so that a
  grader running under another uid can read it.
- Dependencies: `openjiuwen` pulls in many packages (model SDKs, transformers, vector stores,
  telemetry); they are transitive and unused — the harness imports only
  `openjiuwen.core.foundation.llm.model` and the logging configuration. All bench runs under
  the judge's flags on an `--internal` Docker network showed no outbound attempts other than
  `LLM_BASE_URL`.
- Task files are untrusted data. Programs the model writes run in a separate process session,
  with an environment that has no `LLM_*` variables, a timeout, memory and file-size limits,
  and capped output; the key itself is kept out of the harness's process environment and
  redacted from every output. Test files and fixed sources are snapshotted and restored.

## Bench and trajectories

`bench/` holds the local judge replica and training data tools:
- `stub_proxy.py` — OpenAI-compatible stand-in for the organisers' proxy: counts tokens the
  same way, stops answering when the budget is spent; relays to the real proxy or answers from
  an oracle file (to test the mechanics for free);
- `judge_sim.sh` — builds the image and runs it with the judge's exact flags against the stub;
  `run_local.sh` — the same harness without Docker, from a code snapshot;
- `gen_tasks.py` / `grade.py` — generator of training sets in the format of the examples
  (all four kinds plus 20 % infeasible variants of many sorts, hidden stricter tests for code)
  and the grader; a second, independently written generator was used as a cross-check;
- failure-scenario tests (broken inputs, misbehaving model replies, proxy outages, budget
  exhaustion, SIGTERM) are kept outside the repository: they contain deliberately hostile
  fixtures that have no place in a submitted project.

Collected trajectories: every model call of the bench runs is logged (task, kind, model,
prompt/completion tokens, cost, latency) — `bench/trajectories/calls_*.csv`, aggregated by
`bench/tools/collect_traj.py`. Before any call the harness also writes its forecast for every
task (`estimate`, `p_success`, `ctx_chars`) and, after each task, the actual spend, to
`token_estimates.json` next to the solutions file in `$OUT` (and to the trace). The forecast
is a per-kind linear model `cost = max(floor, a + b × context characters)` fitted by least
absolute deviations (the median forecast, which minimises the mean absolute error) on the
tasks of these trajectories (counts per kind in the table below), retries, second opinions and escalations included; an upper
bound `estimate_p90 = forecast × q90(actual/forecast)` is written next to it. A question on a
large prose base read as paragraphs gets 2.5× the search forecast: the fit sees only the first
excerpt, while such a task takes one call (~280 units) or two-three looks with 12k-character
excerpts (650–1600). In-sample quality per kind (organisers' units):

| kind | tasks | mean actual | MAE | under-forecast | p90 factor |
|---|---|---|---|---|---|
| coding | 116 | 340 | 207 | 51 % | 1.94 |
| data | 138 | 115 | 47 | 51 % | 1.95 |
| math | 210 | 142 | 60 | 50 % | 1.78 |
| search | 160 | 166 | 57 | 49 % | 3.15 |

The error is dominated by the rare expensive tasks (a reasoning retry of a hard code fix, a
second look at a large prose base); the median task is forecast within a few units.

Results on the real proxy (organisers' units; feasible reward solved / available; ranges over
repeated runs of the stable versions of 07.11–08.11):

| set | tasks (infeasible) | what it is | solved reward | tokens |
|---|---|---|---|---|
| train_b | 100 (20) | generator in the examples' format | 233/233 (100 %) | ~4.3k (fast path, code repair) |
| var80 | 80 (16) | independent generator: paraphrases, other file/field names, classes, TSV | 184/184 (100 %) | ~7–8k |
| var2 | 60 (12) | 55–181 KB prose bases, dirty CSVs (cp1251, `;`, "1,250"), packages | 141–146/152 (93–96 %) | ~25–33k |
| adv | 60 (30) | solvable task + minimally edited unsolvable twin | 90–97/99 (91–98 %) | ~5k |
| hard | 116 (16) | GSM-Hard/GSM8K, MBPP with bugs, MuSiQue 2–4 hops, DABench | 269–281/297 (91–95 %) | ~30–40k |
| math2 | 68 (8) | MATH-500 level 5, GSM8K, SVAMP, ASDiv | 93–96 % | ~25k |

A hold-out set of 60 tasks built by a separate reviewer from other sources was run on
intermediate versions: 77 % → 85 % → 90 % → 98 % → 96 % of feasible tasks solved (only aggregate
scores and generic failure categories were fed back, never task texts or answers; see
`docs/DECISIONS.md` for both hold-out sets). On sets in the
format of the examples a run of 100 tasks takes about a minute with 5 sessions and costs
about 100 units per task, infeasible ones included.

Cost per task over all six sets above (one run each), by kind:

| kind | feasible | tasks | mean | median | p90 | calls/task |
|---|---|---|---|---|---|---|
| coding | yes | 79 | 385 | 151 | 798 | 1.34 |
| data | yes | 80 | 120 | 80 | 257 | 1.02 |
| data | no | 15 | 210 | 99 | 415 | 1.27 |
| math | yes | 136 | 283 | 150 | 683 | 1.65 |
| math | no | 27 | 348 | 163 | 880 | 1.74 |
| search | yes | 87 | 195 | 88 | 387 | 1.11 |
| search | no | 30 | 227 | 130 | 520 | 1.47 |

## Open questions to the organisers

Handled defensively in the code: location of `answer.txt` (both places), `OUT` as file or
directory, unknown budget, unknown answer for infeasible tasks.

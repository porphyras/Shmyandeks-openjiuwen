# Decision report

What the harness does, why it is built this way, and the numbers behind each decision.
All costs are in the organisers' units (`prompt_tokens / 10 + completion_tokens`, reasoning
included). "Bench" means the local judge replica in `bench/` running the real proxy.

## 1. Problem

| | |
|---|---|
| Input | a directory of tasks (math, data analysis, knowledge-base search, code fixing), each with a reward; all visible at once |
| Score | sum of rewards of correct answers; ties broken by fewer tokens; infeasible tasks score 0 and must get no `answer.txt` |
| Budget | ~100k units per run (env `BUDGET`); the proxy does not report the remainder |
| Environment | the organisers' `docker run`: read-only root, tmpfs `/tmp`, `--cap-drop ALL`, no network except the proxy, 1 hour |
| Constraint | every model call through openJiuwen; solving without the model is allowed |

The budget is large relative to a well-built pipeline (~5k units for 100 tasks in the format of
the examples), so the design goal is accuracy first, with every token spent where it moves the
score, and a planner that degrades gracefully when the budget is tight.

## 2. Architecture

| Stage | Cost | What happens |
|---|---|---|
| 0. Fast path | 0 | plain code answers a task only if every sentence of the statement matches a known form and the data checks out; anything else goes on |
| 1. Free inspection | 0 | compact context: table summaries, KB documents by entity links or BM25 paragraphs, test run + compressed log; free infeasibility verdicts from signals that cannot fire on a solvable task |
| 2. One short call | ~100 | a no-reasoning model returns a checkable artefact: a Python program, one pandas expression, an `ANSWER:` line, or only the changed functions |
| 3. Free verification | 0 | run the program / the tests / find the answer verbatim in the KB |
| 4. Second opinion | ~150 | only for risky computations (gated); a reasoning judge only when two different answers stand |
| 5. Retry / escalation | 150–900 | only after a failed check: next model of the ladder with the error text; reasoning model last |
| Planner | – | greedy by `reward × p(success) / p90 cost forecast`, 5 sessions, budget checked before every call, economy mode when the budget is short |

## 3. Key decisions

| Decision | Alternative | Evidence | Why |
|---|---|---|---|
| No-reasoning models by default, reasoning only as the last rung | reasoning everywhere | mean cost per call: 144 (27B no-reasoning) vs 887 (27B reasoning) over 8.5k bench calls | the cheap call plus a free check solves most tasks; reasoning is kept for what failed a check |
| Cap on all reasoning calls: 15 % of a known budget (else 15k) | no cap | — | a heavy set must not spend the budget on rescues |
| Program as the artefact for computations: one assignment per quantity, no comments | free-text answer; commented code; "shortest program" | ~25 % fewer tokens than commented code; "shortest program" saved ~30 % but collapsed steps into a wrong loop on an example task | executable, checkable, keeps rounding steps |
| Data: the harness loads and cleans the tables, the model writes one pandas expression | the model writes a full program | −47 % tokens at equal accuracy (A/B on the real proxy) | encoding, separators, "1,250", "$12.50", total rows handled once in code, not by the model |
| Code: prompt shows the source and the compressed test log, not the test file | send the test file too | −16 % tokens at equal accuracy | the failing assertion lines are already in the traceback |
| KB: one line `ANSWER: <value>` | chain-of-facts reply | −30 % tokens on card bases | chain kept for prose bases, where it helps |
| Deterministic fast path with strict guards | model for everything | 226 bench tasks answered with 0 errors; 353 tasks of an independent generator, 0 wrong; ~half of example-format tasks; train_b at ~5k units total | the organisers allow it; a misparse can only cost tokens, never a wrong answer |
| Static contradiction detector for code tests (no test code executed) | let the model try | 0 false verdicts on ten bench sets incl. solvable/unsolvable twins | contradictory tests are infeasible; recognising them for free saves a doomed call and keeps `DONE` honest |
| Free infeasibility verdicts only from signals that cannot fire on a solvable task | aggressive heuristics | latest runs: 0 free verdicts on feasible tasks; free verdicts on 6/20 (train_b), 6/16 (var80), 12/30 (adv) infeasible tasks | a false verdict loses a reward; a missed one costs only tokens |
| Gated second opinion for computations (control flow, long/algebraic statement, numbers not in the statement, unused stated numbers) | always / never | MATH-level-5 + GSM8K + SVAMP + ASDiv set: 53/60 → 56–58/60; +20–30 % tokens on computation tasks only | catches mental shortcuts; plain step-by-step arithmetic is not re-checked |
| Judge only when two different answers stand | judge on every second opinion | judge needed on 32 of ~1,400 bench math tasks | the reasoning call is paid for only when there is something to arbitrate |
| Code reasoning retry limit 1500 → 3000 tokens | keep 1500 | of 97 reasoning retries at 1500: 64 rescued, 9 failed, 24 cut empty; at 3000: hard-set code 24/25, 0/6 cuts | an empty cut is pure waste |
| Order by p90 forecast; economy mode when a known budget < 1.25× queue forecast | median forecast; no economy mode | offline replay of real traces vs a 0/1-knapsack oracle: 92–100 % of the oracle from 1000 units (example format), full score from 5000; hard sets 65–87 % at 2–20k; p90 ≥ median under tight budgets | robust to the heavy tail of costs |
| Proxy errors never end the run; only an explicit quota answer (`insufficient_quota`, HTTP 402, or "budget exhausted / exceeded / out of budget" in any error) stops new calls | stop after N failures | an early version ended a run after 6 failed calls during an outage | failed calls are free; giving up loses every remaining task |
| Code fixes merged by function name; a method sent without its class goes to its class; with a base class and overrides, every placement is tried against the tests | drop ambiguous methods; choose by text similarity | two similarity measures (shared identifiers, token sequence) each put a replayed fix into the wrong class; trying placements: 5/5 such bench tasks solved incl. hidden tests, at lower cost than before | the tests are the free oracle |

## 4. Rejected experiments

| Experiment | Result | Reason |
|---|---|---|
| Code repair without the model (single-token mutations + tests) | 41 fixed / 9 overfits (hidden tests fail); with a uniqueness check 33 / 2, coverage ~22 % | saves ~500 units per 100 tasks against a risk of ~1 reward |
| Code context trimmed to the failing tests' functions | −3–6 % tokens, more expensive on the hard set | not worth it |
| KB reply limits 80/400 instead of 40/260 | var2 139 vs 143, cut-offs remain | lower score |
| "Invented number" in a math program as an infeasibility signal | fires on 28/178 feasible tasks, catches 2/24 infeasible | models fill a missing number with 1 or a "typical" constant |
| KB answer must be reachable by card links from the question's entity | would drop 6 correct answers on adv, no score gain | computed answers, links via file names and markdown |
| Longer retry (600 tokens) for prose replies cut at 260 | 0/2 recovered, +600 units each | on prose, a cut meant a missing fact; the retry stays for card bases (+2 on adv) |
| Choosing a method's class by text similarity | each of two measures misplaced a fix in the replays (an override with the same names; `max(values)` closer to `mean(values)` than the mean itself) | replaced by trying placements against the tests |
| Snake_case duplicate columns for statement/header name mismatch | doubled every whole-frame aggregate | replaced by one line in the prompt naming the pair |

## 5. Environment traps found

| Trap | Symptom | Fix |
|---|---|---|
| openJiuwen creates `./logs/` on import | crash under a read-only root | logging configured console-only before any other openJiuwen import |
| `asyncio.run` per call | "Event loop is closed" on the second call | one persistent event loop in a thread |
| `--cap-drop ALL` removes root's DAC override | root cannot write into a task directory owned by another uid with mode 775 (confirmed on a strict run) | stay root (the judge's directory must be writable), record `write_failed`; asked the organisers |
| Temp files are created 0600 | a grader under another uid cannot read `answer.txt` | answers written with mode 0666 |
| A stale `.pyc` shipped with a task | Python imports old byte-code, tests disagree with the source | `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX` into `/tmp` |
| Budget passed as env `BUDGET` | — | read `--budget`, `BUDGET`, `TOKEN_BUDGET` in that order |
| HTTP 402 / `insufficient_quota` vs "Rate limit exceeded" | a rate limit taken for budget exhaustion stopped the run | explicit classification: quota → stop new calls; 429 → back off |
| The key in `/proc/<pid>/environ` | readable by programs the model writes | re-exec at start, key passed through a pipe, redacted from all output |
| `preexec_fn` in a multi-threaded process | possible deadlock between fork and exec | children start through a tiny exec launcher |
| Running as PID 1 | signals from own children could stop the run | only signals from outside the container are acted on |
| Tests that create a file and assert it did not exist | every second test run fails, also the checker's hidden run | files created by a test run are removed after it |

## 6. Results

Real proxy, feasible reward solved / available (ranges over repeated runs):

| Set | Tasks (infeasible) | What it is | Solved | Units per run |
|---|---|---|---|---|
| train_b | 100 (20) | generator in the format of the examples | 233/233 (×3) | 5.1–6.2k |
| var80 | 80 (16) | independent generator: paraphrases, other names, TSV | 184/184 | 7–8k |
| var2 | 60 (12) | 55–181 KB prose bases, dirty CSVs, packages | 136–146/152 | 25–33k |
| adv | 60 (30) | solvable task + minimally edited unsolvable twin | 90–97/99 | ~5k |
| hard | 116 (16) | GSM-Hard/GSM8K, MBPP with bugs, MuSiQue 2–4 hops, DABench | 269–281/297 | 30–40k |
| math2 | 68 (8) | MATH-500 level 5, GSM8K, SVAMP, ASDiv | 94–96 % | ~25k |
| qualification | 2 | the organisers' qualification format | 2/2 (×3 locally, ×3 in the judge's container) | ~150 |

Stability (three runs of one version, tasks solved in every run): train_b 80/80, var2 45/48,
adv 26/30, hard 90/100. The blinking tasks are MuSiQue questions with distractors and a few
olympiad-style math problems.

Two hold-out sets were built independently from other sources. Only aggregate scores and,
on a few occasions, generic failure categories ("program crashed", "declared infeasible", "cut
at the token limit") were fed back — never task texts or answers. Part of the early progress on
set A came from fixing the categories reported for it.

| Hold-out | Versions | Result (feasible reward solved) |
|---|---|---|
| A — 60 tasks, reward 142 | successive versions on 07.11 | 77 % → 85 % → 90 % → 98 % of feasible tasks |
| | stable 07.11 ×3 / stable 08.11 ×2 / current ×1 | 129–141 / 136–138 / 136 of 142 |
| B — 64 tasks, reward 140 | stable 07.11 / stable 08.11 ×2 / current ×1 | 125 / 133–135 / 130 of 140 |

Latest changes (08.11) on the tasks they target: card-base search cut-offs +2 (adv), third look
at large prose bases +3 (var2; 8 such tasks 16/16), method placement +1 (var2 code; 5/5 with
hidden tests).

## 7. Known weak spots

- Math where two no-reasoning models agree on the same wrong answer (two of four misses on
  the latest hold-out runs); a check of another kind — plugging the answer back into the
  statement — is prepared as an experiment, off by default.
- MuSiQue-style multi-hop questions with distractor paragraphs: run-to-run noise of 2–4 tasks
  on the hard set.
- Code fixes that pass the visible tests but special-case them (hidden tests fail): rare, one
  task each on var2 and hard.

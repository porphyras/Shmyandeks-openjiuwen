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
| 0. Fast path | 0 | plain code answers a task only if every sentence of the statement matches a known form and the data checks out; code tasks are repaired with definitions verified in other tasks of the run; anything else goes on |
| 1. Free inspection | 0 | compact context: table summaries, KB documents by entity links or BM25 paragraphs, test run + compressed log; free infeasibility verdicts from signals that cannot fire on a solvable task |
| 2. One short call | ~100–150 | a no-reasoning model returns a checkable artefact: a Python program, one pandas expression, an `ANSWER:` line, or only the changed functions |
| 3. Free verification | 0 | run the program / the tests / find the answer verbatim in the KB |
| 4. Second opinion | ~150 | only for risky computations (gated); a reasoning judge only when two different answers stand |
| 5. Retry / escalation | 150–900 | only after a failed check: next model of the ladder with the error text; reasoning model last |
| Planner | – | greedy by `reward × p(success) / p90 cost forecast`, 5 sessions, budget checked before every call, economy mode when the budget is short |

## 3. Key decisions

| Decision | Alternative | Evidence | Why |
|---|---|---|---|
| No-reasoning models by default, reasoning only as the last rung | reasoning everywhere | mean cost per call: 143 (27B no-reasoning) vs 795 (27B reasoning) over the 11.7k calls of 195 bench runs on the real proxy | the cheap call plus a free check solves most tasks; reasoning is kept for what failed a check |
| Cap on all reasoning calls: 15 % of a known budget (else 15k) | no cap | — | a heavy set must not spend the budget on rescues |
| Program as the artefact for computations: one assignment per quantity, no comments | free-text answer; commented code; "shortest program" | ~25 % fewer tokens than commented code; "shortest program" saved ~30 % but collapsed steps into a wrong loop on an example task | executable, checkable, keeps rounding steps |
| Data: the harness loads and cleans the tables, the model writes one pandas expression | the model writes a full program | −47 % tokens at equal accuracy (A/B on the real proxy) | encoding, separators, "1,250", "$12.50", total rows handled once in code, not by the model |
| Code: prompt shows the source and the compressed test log, not the test file | send the test file too | −16 % tokens at equal accuracy | the failing assertion lines are already in the traceback |
| KB: one line `ANSWER: <value>` | chain-of-facts reply | −30 % tokens on card bases | chain kept for prose bases, where it helps |
| Deterministic fast path with strict guards | model for everything | 1,224 fast answers on 213 distinct bench tasks, 0 wrong (it never fires on the paraphrased or hard sets); an independent generator's 353 tasks: 32 answered, 0 wrong; ~half of example-format tasks; train_b at ~5k units total | the organisers allow it; a misparse can only cost tokens, never a wrong answer |
| Code repair from other tasks of the run: a definition whose own tests passed elsewhere replaces the broken one if it is a small edit with the same signature and docstring, uses only names the module has, swaps a method (not a class), and every visible test then passes | always ask the model; repair by guessed mutations (rejected, overfits) | bench with hidden tests: train_a 33/40, train_b 11/20, second generator 20/20, var80 9/16, adv 2/6 code tasks repaired, 0 hidden-test failures, 0 `DONE` on unsolvable tasks; three adversarial pairs (same name with another specification, a helper missing in the target, a class whose other methods differ): 0 false `DONE`; real proxy: train_b 233/233 at 4.3k units (was 5.1k), var80 184/184 at 6.9k (8.0k) | the organisers' generator reuses a small pool of functions (both code examples share four); the kept code already passed its tests, nothing is guessed |
| Free infeasible verdicts from the fast path's full parse (stock never given or contradicted; no card that could be the entity and no mention; no such field; no card has the attribute) | let the model look and answer NOT FOUND / IMPOSSIBLE | all bench sets: math 7 → 32 and search 0 → 33 free verdicts, 0 false; train_b 4.2k → 1.9k units at 233/233, alt100 2.3k → 1.4k, adv 99/99; on train_b infeasible tasks had taken 56 % of the tokens | an infeasible task scores 0 whatever is spent on it; rejected: "a quoted value absent from the table" (two generators disagree whether the answer is 0 or infeasible) |
| Relation chains parsed as a whole by a small grammar ("the mentor of X's mentor", "the company that employs the mentor of X") and walked over the cards by code | let the model follow the chain | red-team batches: chains of 2+ hops 16/19 without the model, 19/19 right; the model alone miscounted hops in 3 of ~10 | counting hops is where the model slips; anything that does not parse stays with the model |
| Static contradiction detector for code tests (no test code executed) | let the model try | 0 free verdicts on feasible tasks in all bench runs since 07.11 afternoon (25cfdbc), solvable/unsolvable twins included | contradictory tests are infeasible; recognising them for free saves a doomed call and keeps `DONE` honest |
| Free infeasibility verdicts only from signals that cannot fire on a solvable task | aggressive heuristics | latest runs: 0 free verdicts on feasible tasks; free verdicts on 6/20 (train_b), 6/16 (var80), 12/30 (adv) infeasible tasks | a false verdict loses a reward; a missed one costs only tokens |
| Gated second opinion for computations (control flow, long/algebraic statement, numbers not in the statement, unused stated numbers) | always / never | MATH-level-5 + GSM8K + SVAMP + ASDiv set: 53/60 → 56–58/60; +20–30 % tokens on computation tasks only | catches mental shortcuts; plain step-by-step arithmetic is not re-checked |
| Judge only when two different answers stand | judge on every second opinion | a third call on 32 of ~1,400 math tasks in the versioned bench runs' traces | the reasoning call is paid for only when there is something to arbitrate |
| Code reasoning retry limit 1500 → 3000 tokens | keep 1500 | of 97 reasoning retries at 1500: 64 rescued, 9 failed, 24 cut empty; at 3000: hard-set code 24/25, 0/6 cuts | an empty cut is pure waste |
| Order by p90 forecast; economy mode when a known budget < 1.25× queue forecast | median forecast; no economy mode | offline replay of real traces vs a 0/1-knapsack oracle: 92–100 % of the oracle from 1000 units (example format), full score from 5000; hard sets 54–87 % at 2–20k (adv: 100 % from 5k); p90 ≥ median under tight budgets | robust to the heavy tail of costs |
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
| A second opinion for every model-path computation | math2 159–162 vs 165–170 of 173; hard math 61–64 = 61–64; hold-out math no gain; tokens ×1.6–2 | the gated second opinion stays |
| Checking a math answer by plugging it back into the statement (model-written asserts) | hold-out math +0.3–0.7 tasks of 12 over three runs, tokens ×4–5; the check fired about once per run | gain within noise |
| A review call after the visible tests pass, for bugs the tests miss | hidden-test score 63 → 60 on MBPP-style tasks (the review "fixed" correct functions), tokens +30–52 % | breaks correct code |
| "A quoted value absent from the table" as a free infeasible verdict | two generators disagree whether the answer is 0 or infeasible | left to the model |
| Text-similarity choice of a fix's class; free-text tails in fast-path patterns | a wrong class in replays; three wrong fast answers on adversarial tails ("sells 5 boxes to a customer who returns them") | closed word lists only, placements tried against the tests |

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

Real proxy, feasible reward solved / available (ranges over repeated runs of the stable versions of 07.11–08.11, the last being stable-2026-11-08h):

| Set | Tasks (infeasible) | What it is | Solved | Units per run |
|---|---|---|---|---|
| train_b | 100 (20) | generator in the format of the examples | 233/233 (×3) | 5.1–6.2k on 07.11; ~1.56k now (code repair, free infeasible verdicts, wider fast path) |
| train_a | 200 (40) | the same generator, a larger draw | 479/479 | ~1.9–2.3k |
| alt100 | 100 (20) | a second generator in the format of the examples | 227/227 | 5.3k on 07.11; ~0.44k now |
| var80 | 80 (16) | independent generator: paraphrases, other names, TSV | 184/184 | 8.0k on 07.11; 6.9–7.2k now |
| var2 | 60 (12) | 55–181 KB prose bases, dirty CSVs, packages | 141–152/152 | 25–33k |
| adv | 60 (30) | solvable task + minimally edited unsolvable twin | 90–99/99 | 4.5–5k |
| hard | 116 (16) | GSM-Hard/GSM8K, MBPP with bugs, MuSiQue 2–4 hops, DABench | 269–281/297 | 30–40k |
| math2 | 68 (8) | MATH-500 level 5, GSM8K, SVAMP, ASDiv | 93–98 % | ~23k |
| qualification | 2 | the organisers' qualification format | 2/2 (×3 locally, ×3 in the judge's container) | ~150 |

Stability (three runs of one version, tasks solved in every run): train_b 80/80, var2 45/48,
adv 26/30, hard 90/100. The blinking tasks are MuSiQue questions with distractors and a few
olympiad-style math problems.

Two hold-out sets were built independently from other sources. Only aggregate scores and,
on a few occasions, generic failure categories ("program crashed", "declared infeasible", "cut
at the token limit") were fed back — never task texts or answers. Part of the early progress on
set A came from fixing the categories reported for it. On 08.11 the half of set B written in the
examples' format (32 tasks, reward 69) was handed over as training data for the fast path; from
then on only set A and the other half of B count as blind.

| Hold-out | Versions | Result (feasible reward solved) |
|---|---|---|
| A — 60 tasks, reward 142 | successive versions on 07.11 | 77 % → 85 % → 90 % → 98 % → 96 % of feasible tasks |
| | stable 07.11 ×3 / stable-08 ×2 / stable-08d, 08e (08.11 afternoon) | 129–141 / 136–138 / 138, 141 of 142 |
| B — 64 tasks, reward 140 | stable 07.11 ×3 / stable-08 ×2 / stable-08d, 08e (08.11 afternoon) | 125 (all three) / 133–135 / 133, 135 of 140 |
| B, blind half — 32 tasks, reward 71 | 08.11 afternoon (three versions) | 64–68 of 71 |

The fast path rarely fires on these hold-out sets (1 of 60 and 1 of 32 blind tasks): they are
written in other styles (competition math, table QA, multi-hop QA, small buggy programs); on the
training half of B it answers 22 of 32 tasks without the model, with no wrong answer.

Latest changes (08.11), measured on the affected tasks only (targeted real-proxy runs, not full-set runs): card-base search cut-offs +2 (adv), third look
at large prose bases +3 (var2; 8 such tasks 16/16), method placement +1 (var2 code; 5/5 with
hidden tests).

## 7. Known weak spots

- Math where two no-reasoning models agree on the same wrong answer: about one task of twelve
  on the hold-out math, run to run; the experiments with a wider second opinion and with an
  answer check were within noise (section 4).
- MuSiQue-style multi-hop questions with distractor paragraphs: run-to-run noise of 2–4 tasks
  on the hard set.
- Code bugs that no visible test exposes (the hidden tests do): the harness fixes what the
  visible tests show; a blind review call broke correct functions more often than it helped.
- Relation chains that mix inverse relations ("the person mentored by X's mentor") are left to
  the model, which sometimes miscounts the hops.

## 8. Blind red-team batches

A separate session generated batches "as the organisers might" without seeing the harness
code or its sets, and reported only aggregates and failure categories. Seven batches (b01–b07,
~330 tasks, solvable and unsolvable, aimed at the fast path's edges: paraphrases, near-miss
wordings, changed scopes, long chains and tables of thousands of rows) were run on successive
versions; categories, never task texts, came back. Together with the reviewer's offline audit
(721 tasks by the end: 155 targeted fast-path fixtures and 566 from independent generators) they exposed two real fast-path defects, both fixed within the
hour with a regression set: free-text tails accepted in warehouse wordings (three wrong fast
answers on the reviewer's fixtures, in an untagged version) and a false free "infeasible" on a
paraphrased card question ("What is the birth year of X?", found by batch b02, present in
stable-2026-11-08e). Since then: no wrong fast answer and no false verdict on any batch. On stable-2026-11-08h: b06 107/107, b05 125/135 (only code bugs invisible to
the visible tests remain), b07 0 wrong answers without the model; costs per task 30–60 units
on example-like batches.

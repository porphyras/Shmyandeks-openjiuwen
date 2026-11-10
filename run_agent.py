#!/usr/bin/env python3
"""Budget-aware task harness on openJiuwen.

    python /app/run_agent.py --tasks-dir /data/tasks [--out PATH] [--budget N]

Reads every <tasks-dir>/<task>/task.json, inspects tasks locally (free), orders them by
expected reward per token and solves them with short model calls, writing answer.txt right
after each task.
"""
import os
import sys

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import heapq  # noqa: E402
import math  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, wait  # noqa: E402

T0 = time.time()
STOP = threading.Event()
TRACE_LOCK = threading.Lock()
TRACE_PATH = os.environ.get("HARNESS_TRACE") or "/tmp/harness_trace.jsonl"
_REDACT_KEY = [""]  # the proxy key, kept out of the environment and redacted from all output

# Model ladder per task kind: level 0 is the first attempt, higher levels are retries.
LADDERS = {
    "math": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "data": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "search": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "coding": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
}
# Prior success probability by kind, and the cost model (organisers' units):
#   median forecast = max(floor, a + b * context_chars), upper bound = forecast * p90
# fitted by least absolute deviations over 664 bench tasks on the real proxy, all six sets
# (bench/tools/fit_cost.py; retries, second opinions and escalations included).
PRIOR_P = {"math": 0.95, "data": 0.97, "search": 0.97, "coding": 0.97}
COST_FIT = {"math": (67.5, 0.1191, 40.1, 1.78), "data": (13.8, 0.1086, 30.5, 1.95),
            "search": (18.1, 0.0285, 19.2, 3.15), "coding": (-34.4, 0.1178, 49.5, 1.94)}
# search: log(cost) = a + b*log(1+chars) + c*prose for the median, then the same for the p90
SEARCH_FIT = (-1.234, 0.75, 0.1, 2.499, 0.375, 0.6)
REASONING_MAX_TOKENS = 1500


def redact(text):
    text = str(text)
    if _REDACT_KEY[0] and _REDACT_KEY[0] in text:
        text = text.replace(_REDACT_KEY[0], "[redacted]")
    return text


def trace(rec):
    try:
        rec = dict(rec)
        rec["t"] = round(time.time() - T0, 2)
        line = redact(json.dumps(rec, ensure_ascii=True, default=str))
        with TRACE_LOCK:
            try:
                with open(TRACE_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        if rec.get("ev") in ("task", "start", "end", "plan", "write_failed", "worker_error"):
            print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--tasks-dir", "--tasks_dir", "--tasks", dest="tasks_dir")
    p.add_argument("--out", "--output", "--out-file", dest="out")
    p.add_argument("--budget", type=float)
    p.add_argument("--workers", type=int)
    p.add_argument("--time-limit", type=float)
    args, _unknown = p.parse_known_args(argv)
    return args


def env_float(name, default=None):
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


class SkipTask(Exception):
    pass


class Run:
    def __init__(self, args, api_key):
        from harness.llm import LLM
        from harness.solver import Solver
        from harness.io_utils import Summary

        self.tasks_dir = args.tasks_dir or os.environ.get("TASKS") or "/data/tasks"
        self.summary = Summary(args.out or os.environ.get("OUT"))
        # The organisers pass the budget in env BUDGET (e.g. 100000); the proxy does not report
        # what is left, so the harness counts it itself.
        # Not passed at all: the organisers' announced figure, so that the per-call check, the
        # reasoning cap and economy mode work as on the judge (unlimited only via HARNESS_BUDGET=0).
        budget = args.budget or env_float("BUDGET") or env_float("TOKEN_BUDGET") or \
            env_float("HARNESS_BUDGET", 100000.0)
        self.time_limit = args.time_limit or env_float("HARNESS_TIME_LIMIT", 3300.0)
        self.workers = max(1, min(5, args.workers or int(env_float("HARNESS_WORKERS", 5))))
        self.infeasible_answer = os.environ.get("HARNESS_INFEASIBLE_ANSWER", "")
        self.second_pass = os.environ.get("HARNESS_SECOND_PASS", "1") == "1"
        # Kinds whose failed checks get one more try on the reasoning model: code fixes (rescued
        # 3 of 4 on the hard set) and data programs that crashed twice (rare, but then a sure loss).
        self.retry_kinds = set(k for k in os.environ.get("HARNESS_RETRY_FAILED", "coding,data").split(",") if k)
        # Kinds whose answers get an independent second opinion even in a full run.
        self.crosscheck_kinds = set(k for k in os.environ.get("HARNESS_CROSSCHECK", "").split(",") if k)
        # Computations whose program needed control flow get a second opinion (gated) - on
        # a hard math set this lifted 53/60 to ~58/60; plain arithmetic is never re-checked.
        self.math_xc = os.environ.get("HARNESS_MATH_XC", "gated")
        # Statements understood completely are answered without the model (allowed by the
        # organisers); everything else takes the model path.
        self.fast = os.environ.get("HARNESS_FAST", "1") == "1"
        self.fast_answers = {}
        self.economy = False
        self.llm = LLM(os.environ.get("LLM_BASE_URL", ""), api_key, budget=budget, log=trace,
                       fallback=list(dict.fromkeys(m for lad in LADDERS.values() for m in lad)))
        self.solver = Solver(self.ask)
        self.cost = {}
        self.lock = threading.Lock()
        self.results = {}
        self.answers = {}   # task dir -> final answer (rewritten at the end of the run)
        self.infos = {}
        self.reserved = 0.0  # expected cost of tasks in flight (only matters with a known budget)
        self.estimates = {}  # task dir name -> cost forecast made before any call, and the actual cost
        self.careful = False

    # ----------------------------------------------------------------- model calls
    def ask(self, task, messages, level, max_tokens, temperature=0.0):
        from harness.llm import BudgetExhausted
        if STOP.is_set() or self.time_left() < 20:
            raise BudgetExhausted()
        ladder = LADDERS.get(task.kind, LADDERS["math"])
        model = ladder[min(level, len(ladder) - 1)]
        reasoning = not model.endswith("noreason")
        if reasoning:
            # Code fixes need room: across the bench runs 24 of 97 reasoning retries of code
            # fixes thought until a 1500 cut and returned nothing (64 of the others fixed the
            # task). Only failed fixes ever reach this rung.
            max_tokens = max(max_tokens, REASONING_MAX_TOKENS * (2 if task.kind == "coding" else 1))
        if reasoning:
            # A cap on all reasoning calls together (15 % of a known budget, else 15k units): they
            # rescue about 2 of 3 failed tasks, but must not eat the budget on a heavy set.
            spent_r = sum(v[1] for m, v in self.llm.by_model.items() if not m.endswith("noreason"))
            cap = 0.15 * self.llm.budget if self.llm.budget else env_float("HARNESS_REASONING_CAP", 15000.0)
            if spent_r + max_tokens * 0.5 > cap:
                raise SkipTask()
        rem = self.llm.remaining()
        if rem is not None:
            est = sum(len(str(m.get("content", ""))) for m in messages) / 33.0 + min(max_tokens, 300) * 0.4
            if est > rem:
                raise SkipTask()  # a known budget would be overrun by this call: leave this task
        text, cost, finish = self.llm.chat(model, messages, max_tokens, temperature=temperature,
                                           timeout=150.0 if reasoning else 60.0, tag=task.name)
        with self.lock:
            self.cost[task.name] = self.cost.get(task.name, 0.0) + cost
        trace({"ev": "reply", "task": task.name, "level": level, "model": model, "finish": finish,
               "text": text[:1500]})
        return text

    def time_left(self):
        return self.time_limit - (time.time() - T0)

    # ----------------------------------------------------------------- planning
    def estimate(self, task, info):
        """(p_success, expected cost) before any call; logged next to the actual spend."""
        kind = info.get("kind", "math")
        a, b, floor, p90x = COST_FIT.get(kind, (60.0, 0.1, 40, 2.0))
        cost = max(floor, a + b * info.get("ctx_chars", 0))
        info["cost_p90"] = cost * p90x
        if kind == "search":
            # Knowledge-base questions: a log-log model (median and 90th percentile, pinball loss)
            # with a term for prose bases, fitted on 1.1k model-path tasks of the bench, the blind
            # red-team batches and hold-out B; out of sample it cut the error on search from 225 to
            # 19 units on hold-out B and from 54 to 52 on later bench runs, coverage kept
            # (bench/tools/refit_cost.py). The linear fit with a x2.5 for prose overshot.
            x = math.log1p(info.get("ctx_chars", 0))
            prose = 0.0 if info.get("kb_cards", True) else 1.0
            cost = math.exp(SEARCH_FIT[0] + SEARCH_FIT[1] * x + SEARCH_FIT[2] * prose)
            info["cost_p90"] = max(cost, math.exp(SEARCH_FIT[3] + SEARCH_FIT[4] * x + SEARCH_FIT[5] * prose))
        p = PRIOR_P.get(kind, 0.6)
        if info.get("problems"):
            p *= 0.05
        if getattr(task, "soft_hints", None):
            p *= 0.5
        return p, cost

    # ----------------------------------------------------------------- run
    def prepare_all(self, tasks, deadline_s):
        """Free inspection in parallel; tasks not inspected by the deadline are inspected
        lazily by the workers (a pathological task must not block the others)."""
        infos = {}

        def prep(t):
            try:
                return self.solver.prepare(t)
            except Exception as e:
                return {"problems": [], "warning": "prepare crash %s" % type(e).__name__, "kind": "math",
                        "ctx_chars": 0}

        ex = ThreadPoolExecutor(max_workers=8)
        futs = {ex.submit(prep, t): t for t in tasks}
        done, _pending = wait(futs, timeout=deadline_s)
        for f in done:
            infos[futs[f].name] = f.result()
        ex.shutdown(wait=False, cancel_futures=True)
        return infos

    def finish_task(self, task, outcome):
        from harness.io_utils import write_answer
        ans = outcome.answer
        if outcome.status == "infeasible":
            ans = self.infeasible_answer or None
        elif task.kind in ("math", "data") and ans:
            from harness.solver import format_for_instruction
            ans = format_for_instruction(ans, task.instruction)
        if ans not in (None, ""):
            ans = redact(ans)
            with self.lock:
                self.answers[task.dir] = ans
            if not write_answer(task.dir, ans):
                trace({"ev": "write_failed", "task": task.name, "dir": task.dir})
                self.say_write_failed(task.dir)
            self.summary.record(task.id, ans)
        self.results[task.name] = outcome.status
        est = self.estimates.get(task.name)
        if est is not None:
            est["actual"] = round(self.cost.get(task.name, 0.0), 1)
            est["status"] = outcome.status
            self.summary.record_estimates(self.estimates)
        trace({"ev": "task", "task": task.name, "kind": task.kind, "status": outcome.status, "answer": ans,
               "note": outcome.note, "cost": round(self.cost.get(task.name, 0.0), 1),
               "spent": round(self.llm.spent, 1)})

    def check_writable(self, tasks):
        """A probe file in the first task folder: answers must land there. If the folder is not
        writable for this process (root without CAP_DAC_OVERRIDE on a host directory), say so
        loudly at once rather than finishing silently with no answers on disk."""
        for t in tasks[:1]:
            probe = os.path.join(t.dir, ".write_probe_%d" % os.getpid())
            try:
                fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                os.unlink(probe)
            except OSError as e:
                trace({"ev": "tasks_not_writable", "dir": t.dir, "err": type(e).__name__})
                self.say_write_failed(t.dir)

    def say_write_failed(self, d):
        if getattr(self, "_said_write_failed", False):
            return
        self._said_write_failed = True
        print("WARNING: cannot write answer.txt into %s: the task folders must be writable for the "
              "container's user (e.g. chmod -R a+rwX on the tasks directory). Answers are still kept "
              "in $OUT/solutions.json when OUT is set." % d, file=sys.stderr, flush=True)

    @staticmethod
    def precheck_outcome(t, info):
        from harness.solver import Outcome, looks_complex
        note = "precheck: " + "; ".join(info["problems"])[:300]
        if t.kind == "data" and all("not found in data" in p for p in info["problems"]):
            # "How many units of an absent product" may well be meant as 0: free to answer,
            # and harmless if the task is graded as infeasible.
            return Outcome("solved", "0", note)
        return Outcome("infeasible", note=note)

    def rewrite_answers(self):
        """Model-written programs run in the same filesystem; restore every answer we gave."""
        from harness.io_utils import write_answer
        try:
            snapshot = list(self.infos.values())
        except RuntimeError:  # dict changed size while a worker added a lazy inspection
            snapshot = []
        for info in snapshot:
            if info and info.get("kind") == "coding":
                try:
                    self.solver.protect(info)
                except Exception:
                    pass
        with self.lock:
            items = list(self.answers.items())
        for d, a in items:
            try:
                # Either copy can have been changed independently. Reuse the writer's
                # path checks and normalization to restore both, even if the root copy
                # is intact. Reading an untrusted answer could also block on a FIFO.
                if not write_answer(d, a):
                    trace({"ev": "write_failed", "dir": d})
            except Exception:
                pass

    def run(self):
        from harness.context import load_tasks
        from harness.llm import BudgetExhausted
        from harness import crossfix, fastpath
        from harness.solver import Outcome

        tasks = load_tasks(self.tasks_dir)
        # Small sets (the qualification run has 2 tasks) are solved "for sure": every answer
        # without a free check gets an independent second opinion.
        self.careful = len(tasks) <= int(env_float("HARNESS_CAREFUL_MAX", 10))
        self.solver.careful = self.careful
        trace({"ev": "start", "tasks": len(tasks), "careful": self.careful, "dir": self.tasks_dir,
               "workers": self.workers, "budget": self.llm.budget})
        self.check_writable(tasks)
        infos = self.prepare_all(tasks, deadline_s=max(30.0, min(240.0, self.time_left() / 6)))
        self.infos = infos
        # Functions that passed their tests in one code task, to repair the same functions
        # broken in another task without the model (harness/crossfix.py).
        xfix = crossfix.build_index([(t, infos.get(t.name)) for t in tasks]) \
            if os.environ.get("HARNESS_CROSSFIX", "1") == "1" else {}
        heap, seq = [], itertools.count()
        for t in tasks:
            info = infos.get(t.name)
            if info is None:  # inspection still running: queue it last, inspect lazily
                t.kind = t.type if t.type in ("math", "search", "coding") else "data"
                heapq.heappush(heap, (0.0, next(seq), 0, t))
                continue
            t.kind = info.get("kind", "math")
            if t.reward <= 0:
                trace({"ev": "task", "task": t.name, "status": "skipped", "note": "reward <= 0"})
                continue
            p, c = self.estimate(t, info)
            info["p"], info["cost"] = p, c
            self.estimates[t.name] = {"task_id": t.id, "kind": t.kind, "estimate": round(c, 1),
                                      "estimate_p90": round(info.get("cost_p90", c * 2.0), 1),
                                      "p_success": round(p, 3), "ctx_chars": info.get("ctx_chars", 0),
                                      "prose": t.kind == "search" and not info.get("kb_cards", True)}
            if info.get("problems") and t.kind != "coding" and not self.careful:
                self.finish_task(t, self.precheck_outcome(t, info))
                continue
            if t.kind == "coding" and info.get("initial_pass") and not info.get("problems"):
                self.finish_task(t, Outcome("solved", "DONE", "tests already pass"))
                continue
            if t.kind == "coding" and xfix:
                try:
                    fixed = crossfix.repair(self.solver, t, info, xfix)
                except Exception as e:
                    fixed = None
                    trace({"ev": "crossfix_error", "task": t.name, "err": type(e).__name__})
                if fixed is not None:
                    self.finish_task(t, fixed)
                    continue
            fast = fastpath.solve(t, t.kind) if self.fast else None
            if fast is not None:
                # The whole statement was understood: answered without the model.
                self.finish_task(t, Outcome(fast[0], fast[1] if fast[0] == "solved" else None, "fast path: " +
                                            ("deterministic" if fast[0] == "solved" else fast[1])))
                if not self.careful:
                    continue
                # Small (qualification) sets: the model path still runs through openJiuwen as a
                # confirmation; the deterministic answer stays on disk.
                self.fast_answers[t.name] = fast
            if t.name in self.fast_answers:
                heapq.heappush(heap, (-1.0, next(seq), 0, t))
                continue
            # Ordered by reward per risk-aware cost (the p90 forecast): under a tight budget this
            # beat the median forecast in the offline replay of real traces (bench/tools/budget_sim.py).
            p90 = info.get("cost_p90", c * 2.0)
            heapq.heappush(heap, (-(t.reward * p / max(p90, 1.0)), next(seq), 0, t))
        self.summary.record_estimates(self.estimates)
        trace({"ev": "estimates", "items": self.estimates})
        # Economy mode: a known budget that does not cover the forecast of the whole queue -
        # second opinions and reasoning retries are switched off, first passes get the budget.
        queued_est = sum(infos[x[3].name]["cost"] for x in heap if x[3].name in infos)
        rem = self.llm.remaining()
        self.economy = rem is not None and not self.careful and queued_est > 0.8 * rem
        trace({"ev": "plan", "queued": len(heap), "economy": self.economy, "inspected": len(infos),
               "est_total": round(sum(infos[x[3].name]["cost"] for x in heap if x[3].name in infos), 1)})

        hlock = threading.Lock()
        retry = []
        deferred = []  # original queue entries blocked only by in-flight reservations

        # The per-task pipeline is an openJiuwen Workflow (harness/oj_flow.py) of three nodes -
        # the blocking steps below, run on the node executor; a worker thread only schedules a
        # task and reads the final state. Objects stay in these registries (the workflow state
        # is deep-copied by the SDK); the state carries the task name, its rung and three flags.
        from harness.oj_flow import run_task_flow
        by_name = {t.name: t for t in tasks}
        outcomes = {}
        executor = ThreadPoolExecutor(max_workers=self.workers + 2, thread_name_prefix="node")

        def inspect_step(d):
            t = by_name[d["task"]]
            info = infos.get(t.name)
            if info is None:
                info = self.solver.prepare(t)
                t.kind = info.get("kind", "math")
                info["p"], info["cost"] = self.estimate(t, info)
                infos[t.name] = info
                if info.get("problems") and t.kind != "coding" and not self.careful:
                    self.finish_task(t, self.precheck_outcome(t, info))
                    return dict(d, go=False)
            return d

        def solve_step(d):
            if not d["go"]:
                return d
            t = by_name[d["task"]]
            info = infos[t.name]
            need = info["cost"]
            with self.lock:
                rem = self.llm.remaining()
                if rem is not None and rem - self.reserved < need:
                    temporary = self.reserved > 0 and rem >= need
                    trace({"ev": "defer_budget" if temporary else "skip_budget", "task": t.name,
                           "est": round(need, 1), "remaining": round(rem, 1),
                           "reserved": round(self.reserved, 1)})
                    # A reservation is a forecast, not a charge. Reconsider after the
                    # active workers finish; a task beyond the actual remainder stays skipped.
                    return dict(d, go=False, defer=temporary)
                self.reserved += need
            try:
                outcomes[t.name] = self.solver.solve(t, info, d["level"])
            except BudgetExhausted:
                return dict(d, go=False, stop=True)
            except SkipTask:
                trace({"ev": "skip_budget_call", "task": t.name})
                return dict(d, go=False)
            except Exception as e:
                outcomes[t.name] = Outcome("error", note="%s: %s" % (type(e).__name__, str(e)[:200]))
            finally:
                with self.lock:
                    self.reserved -= need
            return d

        def finish_step(d):
            if not d["go"]:
                return d
            t = by_name[d["task"]]
            info = infos[t.name]
            level = d["level"]
            out = outcomes.pop(t.name)
            # Only technical errors are retried later: on the bench no task that failed its
            # checks twice was ever rescued by the reasoning rung, while infeasible ones burned
            # thousands of tokens there.
            if t.name in self.fast_answers:
                fast_ans = self.fast_answers[t.name][1]
                agree = out.answer == fast_ans
                trace({"ev": "fast_confirm", "task": t.name, "fast": fast_ans, "model": out.answer, "agree": agree})
                if agree or out.status != "solved":
                    return d  # the deterministic answer is already written
                # Qualification and they disagree: a third opinion decides by majority.
                try:
                    o3 = self.solver.solve(t, info, 2)
                except Exception:
                    return d
                third = o3.answer if o3.status == "solved" else None
                if third == out.answer:
                    self.finish_task(t, out)
                trace({"ev": "fast_third", "task": t.name, "third": third})
                return d
            if level == 0 and (out.status == "error" or (out.status == "failed" and t.kind in self.retry_kinds
                                                             and not self.economy)):
                with hlock:
                    retry.append(t)
            self.finish_task(t, out)
            from harness.solver import looks_complex
            gated = (t.kind == "math" and self.math_xc == "gated" and not self.economy and looks_complex(t, out))
            if out.status == "solved" and t.kind != "coding" and (self.careful or t.kind in self.crosscheck_kinds or gated):
                try:
                    out2 = self.solver.crosscheck(t, info, out)
                except BudgetExhausted:
                    return dict(d, stop=True)
                except Exception as e:
                    out2 = out
                    trace({"ev": "crosscheck_error", "task": t.name, "err": type(e).__name__})
                if out2.answer != out.answer:
                    self.finish_task(t, out2)
            return d

        steps = [("inspect", inspect_step), ("solve", solve_step), ("finish", finish_step)]

        def worker():
            while not STOP.is_set():
                with hlock:
                    if not heap:
                        return
                    entry = heapq.heappop(heap)
                    _, _, level, t = entry
                if self.time_left() < 30 or self.llm.exhausted:
                    return
                try:
                    state = run_task_flow(getattr(self.llm, "loop", None), steps, executor, t.name, level,
                                          max(60.0, self.time_left() + 90))
                except Exception as e:
                    trace({"ev": "worker_error", "task": getattr(t, "name", "?"), "err": "%s: %s" % (type(e).__name__, e)})
                    continue
                if state.get("defer"):
                    with hlock:
                        deferred.append(entry)
                if state.get("stop"):
                    if self.llm.exhausted:
                        STOP.set()
                    return

        def pool():
            while True:
                with ThreadPoolExecutor(max_workers=self.workers) as ex:
                    for _ in range(self.workers):
                        ex.submit(worker)
                with self.lock:
                    # No worker owns a reservation now. Float additions/subtractions
                    # can leave a tiny residual that must not defer another whole wave.
                    self.reserved = 0.0
                # All workers have joined: reservations are released and the actual
                # spend is known. Restore priorities and retry levels, without spinning
                # on the same entries while another worker still owns their budget.
                if not deferred or STOP.is_set() or self.llm.exhausted or self.time_left() < 30:
                    return
                for entry in deferred:
                    heapq.heappush(heap, entry)
                deferred.clear()

        pool()
        self.rewrite_answers()
        # Second pass: tasks whose checks failed get one more try on the strongest rung
        # (coding failures after two fixes are mostly contradictory tests: not retried).
        if self.second_pass and retry and not self.llm.exhausted and not STOP.is_set() and self.time_left() > 120:
            for t in retry:
                info = infos.get(t.name) or {}
                heapq.heappush(heap, (-(t.reward * 0.3 / max(info.get("cost", 100) * 4, 1.0)), next(seq), 2, t))
            pool()
            self.rewrite_answers()
        executor.shutdown(wait=False)
        written = sum(1 for d in self.answers if os.path.isfile(os.path.join(d, "answer.txt")) or
                      os.path.isfile(os.path.join(d, "workspace", "answer.txt")))
        print("Done: %d tasks, %d answers written to the task folders, %.0f budget units spent in %d model calls."
              % (len(tasks), written, self.llm.spent, self.llm.calls), file=sys.stderr, flush=True)
        trace({"ev": "end", "spent": round(self.llm.spent, 1), "calls": self.llm.calls,
               "by_model": self.llm.by_model, "results": _count(self.results.values()),
               "elapsed": round(time.time() - T0, 1)})


def _count(vals):
    d = {}
    for v in vals:
        d[v] = d.get(v, 0) + 1
    return d


def take_key():
    """Keep the proxy key out of /proc/<pid>/environ of this process, so programs written by
    the model cannot read it from their parent: re-exec once with the key passed via a pipe.

    Why: task files are untrusted and may carry prompt injections; the programs the model
    writes run as the same user, and on Linux any same-user process can read its parent's
    initial environment. The key is only ever sent to LLM_BASE_URL by the openJiuwen client."""
    fd = os.environ.pop("HARNESS_KEY_FD", None)
    if fd is not None:
        try:
            with os.fdopen(int(fd), "rb") as f:
                return f.read(65536).decode().strip()
        except (OSError, ValueError):
            return ""
    key = os.environ.get("LLM_API_KEY", "")
    if key and os.name == "posix" and os.environ.get("HARNESS_NO_REEXEC") != "1":
        try:
            r, w = os.pipe()
            os.write(w, key.encode())
            os.close(w)
            os.set_inheritable(r, True)
            env = {k: v for k, v in os.environ.items() if k != "LLM_API_KEY"}
            env["HARNESS_KEY_FD"] = str(r)
            os.execve(sys.executable, [sys.executable] + sys.argv, env)
        except OSError:
            pass  # fall back to the plain environment
    os.environ.pop("LLM_API_KEY", None)
    return key


def install_signals():
    """SIGTERM/SIGINT end the run at once (answers are on disk already). Signals sent by the
    programs we run on the model's behalf are ignored."""
    from harness.sandbox import is_sandbox_sender

    def die(signum):
        STOP.set()
        trace({"ev": "end", "signal": signum})
        os._exit(0)

    sigs = {signal.SIGTERM, signal.SIGINT}
    if hasattr(signal, "sigwaitinfo") and hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, sigs)  # inherited by all threads started later

        def waiter():
            while True:
                try:
                    info = signal.sigwaitinfo(sigs)
                except InterruptedError:
                    continue
                sender = info.si_pid
                # As PID 1 of the container, a real stop comes from outside the PID namespace
                # (si_pid == 0); anything sent from inside is one of the programs we ran.
                if (os.getpid() == 1 and sender != 0) or is_sandbox_sender(sender):
                    trace({"ev": "signal_ignored", "from": sender})
                    continue
                die(info.si_signo)

        threading.Thread(target=waiter, name="signals", daemon=True).start()
    else:
        for s in sigs:
            signal.signal(s, lambda signum, frame: die(signum))


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    key = take_key()
    _REDACT_KEY[0] = key
    install_signals()

    from harness.llm import configure_openjiuwen_logging
    configure_openjiuwen_logging()

    run = Run(args, key)

    def watchdog():  # never overrun the hour, whatever a worker is blocked on
        while True:
            time.sleep(5)
            if time.time() - T0 > run.time_limit + 60:
                trace({"ev": "end", "watchdog": True})
                try:
                    run.rewrite_answers()
                finally:
                    os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        run.run()
    except Exception as e:
        trace({"ev": "end", "crash": "%s: %s" % (type(e).__name__, e)})
        try:
            run.rewrite_answers()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()

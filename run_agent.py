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
import json  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

T0 = time.time()
STOP = threading.Event()
TRACE_LOCK = threading.Lock()
TRACE_PATH = os.environ.get("HARNESS_TRACE") or "/tmp/harness_trace.jsonl"

# Model ladder per task kind: level 0 is the first attempt, higher levels are retries.
LADDERS = {
    "math": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "data": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "search": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
    "coding": ["qwen3.8-27b-noreason", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"],
}
# Prior success probability and expected output tokens of one attempt, by kind.
PRIOR_P = {"math": 0.9, "data": 0.9, "search": 0.92, "coding": 0.85}
EXP_OUT = {"math": 80, "data": 100, "search": 8, "coding": 220}


def trace(rec):
    rec = dict(rec)
    rec["t"] = round(time.time() - T0, 2)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with TRACE_LOCK:
        try:
            with open(TRACE_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if rec.get("ev") in ("task", "start", "end", "plan"):
        print(line, file=sys.stderr, flush=True)


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


class Run:
    def __init__(self, args):
        from harness.llm import LLM
        from harness.solver import Solver
        from harness.io_utils import Summary

        self.tasks_dir = args.tasks_dir or os.environ.get("TASKS") or "/data/tasks"
        self.summary = Summary(args.out or os.environ.get("OUT"))
        budget = args.budget or env_float("TOKEN_BUDGET")
        self.time_limit = args.time_limit or env_float("HARNESS_TIME_LIMIT", 3300.0)
        self.workers = max(1, min(5, args.workers or int(env_float("HARNESS_WORKERS", 5))))
        self.infeasible_answer = os.environ.get("HARNESS_INFEASIBLE_ANSWER", "")
        self.llm = LLM(os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_API_KEY", ""),
                       budget=budget, log=trace)
        self.solver = Solver(self.ask)
        self.cost = {}
        self.lock = threading.Lock()
        self.results = {}

    # ----------------------------------------------------------------- model calls
    def ask(self, task, messages, level, max_tokens):
        from harness.llm import BudgetExhausted
        if STOP.is_set() or self.time_left() < 20:
            raise BudgetExhausted()
        ladder = LADDERS.get(task.kind, LADDERS["math"])
        model = ladder[min(level, len(ladder) - 1)]
        reasoning = not model.endswith("noreason")
        if reasoning:
            max_tokens = max(max_tokens, 2500)
        text, cost, finish = self.llm.chat(model, messages, max_tokens,
                                           timeout=150.0 if reasoning else 60.0, tag=task.id)
        with self.lock:
            self.cost[task.id] = self.cost.get(task.id, 0.0) + cost
        trace({"ev": "reply", "task": task.id, "level": level, "model": model, "finish": finish,
               "text": text[:1500]})
        return text

    def time_left(self):
        return self.time_limit - (time.time() - T0)

    # ----------------------------------------------------------------- planning
    def estimate(self, task, info):
        kind = info.get("kind", "math")
        ctx_tokens = (info.get("ctx_chars", 0) + 120) / 3.3 + 25
        one = ctx_tokens / 10.0 + EXP_OUT.get(kind, 100)
        p = PRIOR_P.get(kind, 0.6)
        if info.get("problems"):
            p *= 0.05
        exp_cost = one * (1 + (1 - p))
        return p, exp_cost

    # ----------------------------------------------------------------- run
    def prepare_all(self, tasks):
        infos = {}

        def prep(t):
            try:
                return t.id, self.solver.prepare(t)
            except Exception as e:
                return t.id, {"problems": ["prepare crash %s" % type(e).__name__], "kind": "math", "ctx_chars": 0}

        with ThreadPoolExecutor(max_workers=8) as ex:
            for tid, info in ex.map(prep, tasks):
                infos[tid] = info
        return infos

    def finish_task(self, task, outcome):
        from harness.io_utils import write_answer
        ans = outcome.answer
        if outcome.status == "infeasible":
            ans = self.infeasible_answer or None
        if ans not in (None, ""):
            write_answer(task.dir, ans)
            self.summary.record(task.id, ans)
        self.results[task.id] = outcome.status
        trace({"ev": "task", "task": task.id, "kind": task.kind, "status": outcome.status, "answer": ans,
               "note": outcome.note, "cost": round(self.cost.get(task.id, 0.0), 1),
               "spent": round(self.llm.spent, 1)})

    def run(self):
        from harness.context import load_tasks
        from harness.llm import BudgetExhausted
        from harness.solver import Outcome

        tasks = load_tasks(self.tasks_dir)
        # Small sets (the qualification run has 2 tasks) are solved "for sure": every answer
        # without a free check gets an independent second opinion.
        self.careful = len(tasks) <= int(env_float("HARNESS_CAREFUL_MAX", 10))
        trace({"ev": "start", "tasks": len(tasks), "careful": self.careful, "dir": self.tasks_dir, "workers": self.workers,
               "budget": self.llm.budget})
        infos = self.prepare_all(tasks)
        heap = []
        for t in tasks:
            info = infos[t.id]
            t.kind = info.get("kind", "math")
            p, c = self.estimate(t, info)
            info["p"], info["cost"] = p, c
            if info.get("problems") and t.kind != "coding":
                self.finish_task(t, Outcome("infeasible", note="precheck: " + "; ".join(info["problems"])))
                continue
            if t.kind == "coding" and info.get("initial_pass"):
                self.finish_task(t, Outcome("solved", "DONE", "tests already pass"))
                continue
            prio = t.reward * p / max(c, 1.0)
            heapq.heappush(heap, (-prio, t.id, 0, t))
        trace({"ev": "plan", "queued": len(heap),
               "est_total": round(sum(infos[x[3].id]["cost"] for x in heap), 1)})

        hlock = threading.Lock()
        retry = []

        def worker():
            while not STOP.is_set():
                with hlock:
                    if not heap:
                        return
                    _, _, level, t = heapq.heappop(heap)
                if self.time_left() < 30 or self.llm.exhausted:
                    return
                info = infos[t.id]
                rem = self.llm.remaining()
                if rem is not None and rem < info["cost"] * 0.6:
                    continue  # cannot afford it; cheaper tasks may still fit
                try:
                    out = self.solver.solve(t, info, level)
                except BudgetExhausted:
                    STOP.set() if self.llm.exhausted else None
                    return
                except Exception as e:
                    out = Outcome("error", note="%s: %s" % (type(e).__name__, str(e)[:200]))
                if out.status in ("failed", "error") and level == 0:
                    with hlock:
                        retry.append(t)
                self.finish_task(t, out)
                if self.careful and out.status == "solved" and t.kind != "coding":
                    try:
                        out2 = self.solver.crosscheck(t, info, out)
                    except Exception as e:
                        out2 = out
                        trace({"ev": "crosscheck_error", "task": t.id, "err": "%s" % type(e).__name__})
                    if out2.answer != out.answer:
                        self.finish_task(t, out2)

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for _ in range(self.workers):
                ex.submit(worker)
        # Second pass: failed tasks get one more try on the stronger rung, best value first.
        if retry and not self.llm.exhausted and self.time_left() > 120:
            for t in retry:
                info = infos[t.id]
                heapq.heappush(heap, (-(t.reward * 0.5 / max(info["cost"] * 3, 1.0)), t.id, 2, t))
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                for _ in range(self.workers):
                    ex.submit(worker)
        trace({"ev": "end", "spent": round(self.llm.spent, 1), "calls": self.llm.calls,
               "by_model": self.llm.by_model, "results": _count(self.results.values()),
               "elapsed": round(time.time() - T0, 1)})


def _count(vals):
    d = {}
    for v in vals:
        d[v] = d.get(v, 0) + 1
    return d


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    def on_term(signum, frame):
        STOP.set()
        trace({"ev": "end", "signal": signum})
        os._exit(0)  # answers are already on disk, written atomically after each task

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    from harness.llm import configure_openjiuwen_logging
    configure_openjiuwen_logging()

    run = Run(args)
    # Global watchdog: never overrun the hour, whatever a worker is blocked on.
    def watchdog():
        while True:
            time.sleep(5)
            if time.time() - T0 > run.time_limit + 60:
                trace({"ev": "end", "watchdog": True})
                os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        run.run()
    except Exception as e:
        trace({"ev": "end", "crash": "%s: %s" % (type(e).__name__, e)})
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()

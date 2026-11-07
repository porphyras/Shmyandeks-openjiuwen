"""Evaluate small candidate programs (MBPP reference solutions and their mutants) in this process.

Usage: python3 -I sandbox_eval.py jobs.json  ->  JSON list on stdout.
Each job: {"src": module source, "tests": [assert statements], "calls": [expressions]}.
Every statement runs under a 1 s timer; the whole run should be wrapped in a subprocess timeout
by the caller. Only the builder's own job files are passed here.
"""
import json
import signal
import sys

try:
    import resource
    for lim in ("RLIMIT_AS", "RLIMIT_DATA"):
        if hasattr(resource, lim):
            try:
                resource.setrlimit(getattr(resource, lim), (2 << 30, 2 << 30))
            except (ValueError, OSError):
                pass
except ImportError:
    pass


class TimeLimit(Exception):
    pass


def _alarm(signum, frame):
    raise TimeLimit()


signal.signal(signal.SIGALRM, _alarm)


def run(stmt, ns, mode):
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        if mode == "exec":
            exec(stmt, ns)
            return True, None, None
        v = eval(stmt, ns)
        return True, None, repr(v)
    except BaseException as e:  # noqa: B902 - anything the candidate raises counts as failure
        return False, type(e).__name__, None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        jobs = json.load(f)
    out = []
    for job in jobs:
        ns = {"__name__": "mylib"}
        ok, err, _ = run(job["src"], ns, "exec")
        res = {"ok": ok, "err": err, "tests": [], "calls": []}
        if ok:
            for t in job.get("tests", []):
                p, e, _ = run(t, dict(ns), "exec")
                res["tests"].append([p, e])
            for c in job.get("calls", []):
                p, e, v = run(c, dict(ns), "eval")
                res["calls"].append([p, e if not p else v])
        out.append(res)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()

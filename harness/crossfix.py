"""Code repair from other tasks of the same run, without the model.

Code tasks of one generator share functions: the two code examples carry the same functions
(reverse_words, total, maximum, count_even), each with a different one broken. A variant of
function f is *verified* when, in the task it comes from, the test named test_f passed on the
untouched source. A failing task gets, for every failing test_f, the verified variant of f most
often seen in the other tasks (never its own current version); the replacement is kept only if
then every visible test passes, else the sources are restored and the task goes to the model.
Nothing is guessed: a kept variant is source text that already passed its tests elsewhere.
"""
import ast
import collections
import os

from . import context as ctx


def _test_names(paths):
    """All test function/method names defined in the test files."""
    names = set()
    for p in paths:
        try:
            tree = ast.parse(ctx.read_text(p, 1 << 20))
        except (SyntaxError, ValueError):
            return None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                names.add(n.name)
    return names


def _status(info):
    """(passed, failed) test names of the first run on the untouched source, or None when
    the run gave no per-test result (no "Ran N tests", a crash at import, a timeout)."""
    out = info.get("test_out") or ""
    if "Ran " not in out or "(timeout)" in out or "time budget exhausted" in out:
        return None
    names = _test_names(info.get("tests") or [])
    if not names:
        return None
    failed = set(ctx.failing_test_names(out))
    if failed - names:
        return None  # a failure we cannot attribute
    return names - failed, failed


def _defs(text):
    """{name: (key, source text, (first line, last line))} for top-level functions/classes."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            out[n.name] = (ast.dump(n), "\n".join(lines[first - 1:n.end_lineno]), (first, n.end_lineno))
    return out


def _owner(test, names):
    """The definition a test is named after: test_total -> total, test_stack_pop -> Stack,
    test_safe_div_zero -> safe_div (the longest matching name), or None."""
    t = test.lower()
    hits = [n for n in names if t == "test_" + n.lower() or t.startswith("test_" + n.lower() + "_")]
    return max(hits, key=len) if hits else None


def _tests_by_def(names_tests, defs):
    by = collections.defaultdict(set)
    for test in names_tests:
        o = _owner(test, defs)
        if o:
            by[o].add(test)
    return by


def build_index(pairs):
    """pairs: [(task, info)] of inspected code tasks -> {name: {key: [count, source, tasks]}}.
    A definition is verified in a task when every test named after it passed there (tasks with
    contradictory tests still verify the definitions whose own tests pass)."""
    index = collections.defaultdict(dict)
    for task, info in pairs:
        if not info or info.get("kind") != "coding":
            continue
        st = _status(info)
        if st is None:
            continue
        passed, failed = st
        alldefs = {}
        for path, text in (info.get("src_orig") or {}).items():
            alldefs.update(_defs(text) or {})
        for name, tests in _tests_by_def(passed | failed, alldefs).items():
            if tests and not tests & failed:
                key, src, _ = alldefs[name]
                e = index[name].setdefault(key, [0, src, set()])
                e[0] += 1
                e[2].add(task.name)
    return index


def _replace(text, name, new_src):
    defs = _defs(text)
    if not defs or name not in defs:
        return None
    first, last = defs[name][2]
    lines = text.splitlines()
    tail = "\n" if text.endswith("\n") else ""
    return "\n".join(lines[:first - 1] + new_src.splitlines() + lines[last:]) + tail


def repair(solver, task, info, index):
    """Outcome("solved", "DONE") if variants verified elsewhere make every visible test pass,
    else None (sources left exactly as they were)."""
    if not index or info.get("problems") or info.get("initial_pass") or not info.get("srcs"):
        return None
    st = _status(info)
    if st is None:
        return None
    _, failed = st
    if not failed:
        return None
    src = dict(info.get("src_orig") or {})
    new = dict(src)
    names = set()
    for t in src.values():
        names |= set(_defs(t) or {})
    owners = set()
    for test in failed:
        o = _owner(test, names)
        if o is None:
            return None  # a failing test we cannot tie to a definition: the model's job
        owners.add(o)
    for name in sorted(owners):
        where = [p for p, t in new.items() if name in (_defs(t) or {})]
        if len(where) != 1:
            return None
        current_key = _defs(new[where[0]])[name][0]
        cands = sorted(((c, k) for k, (c, _, tasks) in index.get(name, {}).items()
                        if k != current_key and tasks - {task.name}), reverse=True)
        if not cands:
            return None
        if len(cands) > 1 and cands[0][0] == cands[1][0]:
            return None  # two verified variants equally common: ambiguous
        replaced = _replace(new[where[0]], name, index[name][cands[0][1]][1])
        if replaced is None:
            return None
        new[where[0]] = replaced
    changed = {p: t for p, t in new.items() if t != src[p]}
    if not changed:
        return None
    from .solver import _write
    if not all(_write(p, t) for p, t in changed.items()):
        for p in changed:
            _write(p, src[p])
        return None
    ok, out = solver._run_tests(task, info["tests"])
    if solver._tests_tampered(info) or not ok:
        for p in changed:
            _write(p, src[p])
        return None
    info["final_src"] = list(changed.items())
    from .solver import Outcome
    return Outcome("solved", "DONE", "repaired with definitions verified in other tasks: %s" %
                   ", ".join(sorted(owners)))

"""Code repair from other tasks of the same run, without the model.

Code tasks of one generator share functions: the two code examples carry the same functions
(reverse_words, total, maximum, count_even), each with a different one broken. A variant of
a function (or of a method, for tests named test_<class>_<method>) is *verified* when, in the
task it comes from, every test named after it passed on the untouched source. A failing task
gets, for every failing test, the verified variant most often seen in the other tasks, but only
when that variant is a small edit of the local broken one (a planted bug is a token or two),
every name it uses exists in the target module, and then every visible test passes; otherwise
the sources are restored and the task goes to the model. Nothing is guessed: a kept variant is
source text that already passed its tests elsewhere. Tasks inspected lazily (after the
inspection deadline) take no part.
"""
import ast
import os
import builtins
import collections
import difflib
import io
import re
import textwrap
import tokenize

from . import context as ctx

# Size of a fix, in tokens: the examples' planted bugs are 1-2 tokens (-= for +=, sorted for
# reversed) and carry no docstring; bench generators with docstrings plant bugs of up to ~14.
MAX_EDIT_TOKENS = 6           # no docstring to vouch for the definition being the same function
MAX_EDIT_TOKENS_SAME_DOC = 20  # same signature and the same non-empty docstring


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
    the run gave no trustworthy per-test result (no "Ran N tests", a crash at import, a
    timeout, or a failure count that disagrees with unittest's own summary line)."""
    out = info.get("test_out") or ""
    if "Ran " not in out or "(timeout)" in out or "time budget exhausted" in out:
        return None
    names = _test_names(info.get("tests") or [])
    if not names:
        return None
    listed = ctx.failing_test_names(out)
    summary = sum(int(x) for m in re.finditer(r"^FAILED \(([^)]*)\)", out, re.M)
                  for x in re.findall(r"(?:failures|errors)=(\d+)", m.group(1)))
    if summary != len(listed):
        return None  # truncated or unusual output: per-test results cannot be trusted
    failed = set(listed)
    if failed - names:
        return None  # a failure we cannot attribute
    return names - failed, failed


def _defs(text):
    """{name: (key, source, (first line, last line), indent)} for top-level functions and
    classes and, as "Class.method", for the methods of top-level classes."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    out = {}

    def add(name, n):
        first = min([n.lineno] + [d.lineno for d in n.decorator_list])
        src = "\n".join(lines[first - 1:n.end_lineno])
        out[name] = (ast.dump(n), src, (first, n.end_lineno), n.col_offset)

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(n.name, n)
            if isinstance(n, ast.ClassDef):
                for b in n.body:
                    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add("%s.%s" % (n.name, b.name), b)
    return out


def _owner(test, names):
    """The definition a test is named after: test_total -> total, test_safe_div_zero ->
    safe_div (the longest matching name), test_stack_pop -> Stack.pop. A test named after a
    class but no method of it gives None (a whole class is never swapped)."""
    t = test.lower()

    def match(prefix, cands):
        hits = [n for n in cands if t == prefix + n.lower() or t.startswith(prefix + n.lower() + "_")]
        return max(hits, key=len) if hits else None

    top = match("test_", [n for n in names if "." not in n])
    if top is None:
        return None
    methods = [n.split(".", 1)[1] for n in names if n.startswith(top + ".")]
    if not methods:
        return top  # a function
    m = match("test_%s_" % top.lower(), methods)
    return "%s.%s" % (top, m) if m else None


def _tokens(src):
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(textwrap.dedent(src)).readline):
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                tokenize.COMMENT, tokenize.ENDMARKER):
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def _edit_size(a, b):
    """Tokens changed between two definitions (sum over unequal blocks of the longer side)."""
    ta, tb = _tokens(a), _tokens(b)
    if ta is None or tb is None:
        return 10 ** 6
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


def _same_function(a, b):
    """Max tokens allowed between two variants of one definition, or None if they are not
    the same function: a planted bug changes the body, never the signature or the docstring."""
    try:
        na = ast.parse(textwrap.dedent(a)).body[0]
        nb = ast.parse(textwrap.dedent(b)).body[0]
    except (SyntaxError, ValueError, IndexError):
        return None
    if type(na) is not type(nb) or ast.dump(na.args) != ast.dump(nb.args):
        return None
    if [ast.dump(d) for d in na.decorator_list] != [ast.dump(d) for d in nb.decorator_list]:
        return None  # @staticmethod / @property against a plain method: not the same thing
    ra = ast.dump(na.returns) if na.returns else None
    rb = ast.dump(nb.returns) if nb.returns else None
    if ra != rb:
        return None
    da, db = ast.get_docstring(na), ast.get_docstring(nb)
    if da != db:
        return None
    return MAX_EDIT_TOKENS_SAME_DOC if da else MAX_EDIT_TOKENS


KEEP_OPS = os.environ.get("HARNESS_CROSSFIX_KEEP_OPS", "1") != "0"


_GUARD_STMTS = (ast.If, ast.Raise, ast.Try, ast.With, ast.Assert, ast.For, ast.While)
# Calls that replace one another in a planted bug (upper -> lower, sorted -> reversed, min -> max).
_CALL_FAMILIES = [{"lower", "upper", "title", "capitalize", "casefold", "swapcase"},
                  {"sorted", "reversed", "sort", "reverse"}, {"min", "max"}, {"strip", "lstrip", "rstrip"},
                  {"startswith", "endswith"}, {"ljust", "rjust", "center"}, {"ceil", "floor", "round", "trunc"},
                  {"any", "all"}, {"isdigit", "isalpha", "isalnum", "isupper", "islower"},
                  {"append", "insert", "extend", "add"}, {"sum", "len", "prod"}]


def _features(src):
    """Guards and call details of a definition: statements (if/raise/try/with/assert/loops),
    comprehension conditions, keyword arguments (but reverse=), tuples, lambdas, and per call
    name the largest number of positional arguments."""
    try:
        tree = ast.parse(textwrap.dedent(src))
    except (SyntaxError, ValueError):
        return None
    c, pos = collections.Counter(), {}
    for n in ast.walk(tree):
        if isinstance(n, _GUARD_STMTS):
            c["stmt:" + type(n).__name__] += 1
        elif isinstance(n, ast.comprehension):
            c["compif"] += len(n.ifs)
        elif isinstance(n, ast.keyword) and n.arg and n.arg != "reverse":
            c["kw:" + n.arg] += 1
        elif isinstance(n, ast.Tuple) and isinstance(n.ctx, ast.Load) and len(n.elts) > 1:
            c["tuple"] += 1
        elif isinstance(n, ast.Lambda):
            c["lambda"] += 1
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name:
                pos[name] = max(pos.get(name, 0), len(n.args))
    return c, pos


def _keeps_operations(cur_src, cand_src):
    """The variant verified elsewhere must keep what the current definition does besides the
    bug: a guard (if/raise/try/with/assert/loop, a comprehension condition), a keyword argument,
    a sort key's tuple or lambda, a positional argument of a call that stays (round(x, 1)), and
    any call unless one of its family takes its place (upper -> lower is the usual planted bug; a
    lost .strip() or abs() is not). Such a loss can pass the visible tests here and fail wider
    hidden ones, so the task goes to the model instead."""
    fa, fb = _features(cur_src), _features(cand_src)
    if fa is None or fb is None:
        return False
    (ca, pa), (cb, pb) = fa, fb
    if any(ca[k] > cb.get(k, 0) for k in ca):
        return False
    if any(n in pb and pb[n] < pa[n] for n in pa):
        return False
    added = set(pb) - set(pa)
    for gone in set(pa) - set(pb):
        family = next((f for f in _CALL_FAMILIES if gone in f), {gone})
        if not family & added:
            return False
    return True


def _free_names(src):
    """Names a definition reads that it does not bind itself (parameters, assignments,
    loop and comprehension targets, inner definitions, local imports)."""
    try:
        tree = ast.parse(textwrap.dedent(src))
    except (SyntaxError, ValueError):
        return None
    bound, used = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            (used if isinstance(n.ctx, ast.Load) else bound).add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return used - bound - set(dir(builtins))


def _module_names(text):
    """Names bound at the top level of a module."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
    return names


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
        for text in (info.get("src_orig") or {}).values():
            alldefs.update(_defs(text) or {})
        by = collections.defaultdict(set)
        for test in passed | failed:
            o = _owner(test, alldefs)
            if o:
                by[o].add(test)
        for name, tests in by.items():
            if tests and not tests & failed:
                key, src = alldefs[name][0], alldefs[name][1]
                e = index[name].setdefault(key, [0, src, set()])
                e[0] += 1
                e[2].add(task.name)
    return index


def _replace(text, name, new_src):
    d = (_defs(text) or {}).get(name)
    if d is None:
        return None
    (first, last), indent = d[2], d[3]
    body = textwrap.indent(textwrap.dedent(new_src), " " * indent)
    lines = text.splitlines()
    tail = "\n" if text.endswith("\n") else ""
    return "\n".join(lines[:first - 1] + body.splitlines() + lines[last:]) + tail


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
            return None  # a failing test we cannot tie to a function or method: the model's job
        owners.add(o)
    for name in sorted(owners):
        where = [p for p, t in new.items() if name in (_defs(t) or {})]
        if len(where) != 1:
            return None
        cur_key, cur_src = _defs(new[where[0]])[name][:2]
        cands = sorted(((c, k) for k, (c, _, tasks) in index.get(name, {}).items()
                        if k != cur_key and tasks - {task.name}), reverse=True)
        if not cands or (len(cands) > 1 and cands[0][0] == cands[1][0]):
            return None  # nothing verified elsewhere, or two variants equally common
        cand_src = index[name][cands[0][1]][1]
        limit = _same_function(cur_src, cand_src)
        if limit is None or _edit_size(cur_src, cand_src) > limit:
            return None  # not a small fix of this definition: a different function of the same name
        free = _free_names(cand_src)
        if free is None or free - _module_names(new[where[0]]):
            return None  # it relies on a helper this module does not have
        if KEEP_OPS and not _keeps_operations(cur_src, cand_src):
            return None  # it drops a call this definition makes: the model's job
        replaced = _replace(new[where[0]], name, cand_src)
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

"""Task loading and cheap, LLM-free inspection: compact context and feasibility signals."""
import ast
import csv
import hashlib
import io
import json
import os
import re
import stat

MAX_TASK_JSON = 1 << 20
MAX_FILE_READ = 256 * 1024
MAX_INSTRUCTION = 6000
MAX_DATA_CTX = 4000
MAX_KB_CTX = 30000

csv.field_size_limit(1 << 24)

# Boilerplate sentences that only describe the tool usage / answer.txt protocol, which the
# harness handles itself. Unknown sentences are always kept.
_BOILERPLATE = [
    r"Work only inside the current directory\.",
    r"When you are done, write ONLY the (?:final answer|word DONE) to the file answer\.txt in the current directory(?: \(for example with bash: echo 42 > answer\.txt\))?\.",
    r"Compute it with the bash tool \(python3 -c\)\.",
    r"Use the file tools or bash \(ls, grep, cat\) to find it\.",
    r"Use bash with python3 to compute it\.",
]


class Task:
    def __init__(self, task_dir, data):
        self.dir = task_dir
        self.name = os.path.basename(task_dir.rstrip("/"))
        self.id = str(data.get("task_id") or self.name)
        t = str(data.get("task_type") or "").strip().lower()
        if not t:
            m = re.match(r"([a-z_]+?)_\d+$", self.name)
            t = m.group(1) if m else "unknown"
        self.type = t
        self.instruction = str(data.get("instruction") or "")
        try:
            self.reward = float(data.get("reward", 1))
        except (TypeError, ValueError):
            self.reward = 1.0
        if self.reward != self.reward or self.reward in (float("inf"), float("-inf")):
            self.reward = 1.0  # unknown value: treat like an ordinary task
        self.reward = min(self.reward, 1e6)
        ws = os.path.join(task_dir, "workspace")
        # A workspace that is a symlink leading out of the task directory is not trusted.
        inside = os.path.realpath(ws).startswith(os.path.realpath(task_dir) + os.sep)
        self.workspace = ws if os.path.isdir(ws) and inside else None
        self.cwd = self.workspace or task_dir
        self.wants_done = bool(re.search(r"word DONE", self.instruction))

    def short_instruction(self):
        s = self.instruction
        if len(s) > MAX_INSTRUCTION:
            s = s[:MAX_INSTRUCTION // 2] + " ... " + s[-MAX_INSTRUCTION // 2:]
        for pat in _BOILERPLATE:
            s = re.sub(pat, "", s)
        return re.sub(r"\s+", " ", s).strip()


def load_tasks(root):
    tasks = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return tasks
    for name in names:
        d = os.path.join(root, name)
        tj = os.path.join(d, "task.json")
        if not os.path.isfile(tj):
            continue
        try:
            if os.path.getsize(tj) > MAX_TASK_JSON:
                continue
            with open(tj, encoding="utf-8-sig", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            t = Task(d, data)
        except Exception:  # broken task.json of any kind: skip this task only
            continue
        if t.instruction:
            tasks.append(t)
    return tasks


def read_text(path, limit=MAX_FILE_READ):
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return ""  # FIFOs, devices, sockets would block or be endless
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def list_files(root, limit=10000):
    """Regular files under root (symlinks only if they stay inside root), sorted."""
    out = []
    try:
        real_root = os.path.realpath(root)
    except OSError:
        return out
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(x for x in dn if x not in ("__pycache__", ".git") and not os.path.islink(os.path.join(dp, x)))
        for f in sorted(fn):
            if f == "answer.txt" or f.startswith(".answer.txt"):
                continue
            p = os.path.join(dp, f)
            try:
                if os.path.islink(p) and not os.path.realpath(p).startswith(real_root + os.sep):
                    continue
                if not stat.S_ISREG(os.stat(p).st_mode):
                    continue
            except OSError:
                continue
            out.append(p)
            if len(out) >= limit:
                return out
    return out


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(16 << 20)).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- tabular data

def csv_brief(path, head_rows=3, max_distinct=15, max_bytes=16 << 20):
    """Header, row count, a few rows, distinct values of low-cardinality columns, numeric ranges.
    Streams the file (bounded memory); values are compared stripped."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "%s: not a regular file" % os.path.basename(path), {}
        f = open(path, encoding="utf-8-sig", errors="replace", newline="")
    except OSError:
        return "%s: unreadable" % os.path.basename(path), {}
    header, head, nrows, nbytes = None, [], 0, 0
    distinct, lo, hi, numeric = [], [], [], []
    try:
        with f:
            reader = csv.reader(f)
            for row in reader:
                if header is None:
                    header = [c.strip() for c in row]
                    n = len(header)
                    distinct = [set() for _ in range(n)]
                    lo, hi, numeric = [None] * n, [None] * n, [True] * n
                    continue
                if not row:
                    continue
                nrows += 1
                if len(head) < head_rows:
                    head.append(row)
                for i in range(min(len(row), len(header))):
                    v = row[i].strip()
                    if numeric[i]:
                        try:
                            x = float(v)
                            lo[i] = x if lo[i] is None else min(lo[i], x)
                            hi[i] = x if hi[i] is None else max(hi[i], x)
                        except ValueError:
                            numeric[i] = False
                    if len(distinct[i]) <= 200:
                        distinct[i].add(v)
                nbytes += sum(len(c) for c in row) + len(row)
                if nbytes > max_bytes:
                    break
    except (csv.Error, OSError, ValueError):
        pass
    if header is None:
        return "%s: empty file" % os.path.basename(path), {}
    lines = ["%s: %d rows; columns: %s" % (os.path.basename(path), nrows, ",".join(header))]
    lines += [",".join(r) for r in head]
    info = {"header": header, "values": {}}
    fmt = lambda x: str(int(x)) if x == int(x) else str(x)
    for i, col in enumerate(header):
        if numeric[i] and lo[i] is not None:
            lines.append("%s: %s..%s" % (col, fmt(lo[i]), fmt(hi[i])))
            continue
        vals = sorted(distinct[i])
        if len(vals) <= 200:
            info["values"][col] = set(vals)
        if len(vals) <= max_distinct:
            lines.append("%s values: %s" % (col, ",".join(vals)))
        else:
            lines.append("%s: %s distinct values" % (col, len(vals) if len(vals) <= 200 else "200+"))
    return "\n".join(lines), info


def quoted_terms(instruction):
    return [m for m in re.findall(r"'([^'\n]{1,40})'|\"([^\"\n]{1,40})\"", instruction) for m in m if m]


def data_context(task):
    """Returns (context_text, problems) for a workspace with data files."""
    parts, problems, csv_infos = [], [], []
    files = list_files(task.cwd)
    for p in files[:30]:
        rel = os.path.relpath(p, task.cwd)
        if p.lower().endswith(".csv"):
            brief, info = csv_brief(p)
            parts.append(brief)
            csv_infos.append(info)
        else:
            t = read_text(p, 600)
            parts.append("%s:\n%s" % (rel, t[:600]))
    instr = task.short_instruction()
    # Cheap feasibility probe: a quoted value right after a column name ("product 'widget'")
    # must occur in that column (case-insensitive). Anything less specific is left to the model.
    for col, term in re.findall(r"\b(\w+)\s+['\"]([^'\"\n]{1,40})['\"]", instr):
        for info in csv_infos:
            vals = info.get("values", {}).get(col)
            if vals is not None and term.lower() not in {v.lower() for v in vals}:
                problems.append("%s '%s' not found in data" % (col, term))
    # Columns announced in the instruction must exist.
    m = re.search(r"has columns ([\w,]+)", instr)
    if m and csv_infos:
        announced = [c for c in m.group(1).split(",") if c]
        hdr = set(csv_infos[0].get("header", []))
        missing = [c for c in announced if c not in hdr]
        if missing:
            problems.append("columns missing in file: %s" % ",".join(missing))
    if not files:
        problems.append("no data files")
    problems += missing_named_files(task, files)
    text = "\n\n".join(parts)
    if len(text) > MAX_DATA_CTX:
        text = text[:MAX_DATA_CTX] + "\n... (%d files in total)" % len(files)
    return text, problems


# ---------------------------------------------------------------- knowledge base

_STOP = set("the a an in of which what who whom whose is was were did does do for to and or by on at from with "
            "year city born employer headquarters company person directory contains small knowledge base "
            "markdown files use file tools bash ls grep cat find it work only inside current when you are done "
            "write final answer how many".split())


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def kb_docs(task):
    docs = []
    for p in list_files(task.cwd, limit=10000):
        if os.path.splitext(p)[1].lower() in (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ""):
            docs.append((os.path.relpath(p, task.cwd), read_text(p, 8192).strip()))
    return docs


def _title(rel, text):
    m = re.search(r"^#\s*(.+)$", text, re.M)
    if m:
        return m.group(1).strip()
    base = os.path.splitext(os.path.basename(rel))[0]
    base = re.sub(r"^(person|company|org|city)_", "", base)
    return base.replace("_", " ")


def _entities(instruction):
    s = re.sub(r"'s\b", "", instruction)
    caps = re.findall(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+)+)\b", s)
    out = []
    for c in caps:
        words = c.split()
        if all(w.isupper() and len(w) > 1 for w in words) or all(w.lower() in _STOP for w in words):
            continue  # "ONLY DONE", "In Which" ...
        out.append(c)
    return out


def kb_context(task, full_limit=600, max_docs=12):
    """Pick the documents mentioning the entities of the question and follow name links
    (a document that mentions another document's title pulls it in). Falls back to the
    whole KB when it is small or nothing matched."""
    docs = kb_docs(task)
    problems = []
    if not docs:
        return "", ["no knowledge base files"], 0
    total = sum(len(t) for _, t in docs)
    instr = task.short_instruction()
    ents = [_norm(e) for e in _entities(instr)]
    normd = [(_norm(t), _norm(_title(r, t))) for r, t in docs]
    picked = []
    for i, (nt, ntitle) in enumerate(normd):
        if any(e and (e == ntitle or re.search(r"\b%s\b" % re.escape(e), nt)) for e in ents):
            picked.append(i)
    if ents and not picked:
        problems.append("entity not found in knowledge base: %s" % ", ".join(ents))
    # Exact title matches first, they are the subject documents.
    picked.sort(key=lambda i: 0 if normd[i][1] in ents else 1)
    picked = picked[:max_docs]
    frontier = list(picked)
    for _hop in range(3):
        new = []
        for i in frontier:
            for j, (nt, ntitle) in enumerate(normd):
                if j in picked or j in new or len(ntitle) < 3:
                    continue
                if re.search(r"\b%s\b" % re.escape(ntitle), normd[i][0]):
                    new.append(j)
        new = new[:max(0, max_docs - len(picked))]
        picked += new
        frontier = new
        if not new:
            break
    complete = False
    if picked and total > full_limit:
        sel = picked
        # The subject document was found by its exact title and link expansion was not cut
        # by the cap: whatever is not in the excerpt is not in the base either.
        complete = bool(ents) and normd[picked[0]][1] in ents and len(picked) < max_docs
    elif total <= 12000:
        sel = list(range(len(docs)))
    else:
        # Nothing matched and the KB is big: rank by plain word overlap.
        words = set(w for w in _norm(instr).split() if w not in _STOP and len(w) > 2)
        sel = sorted(range(len(docs)), key=lambda i: -len(words & set(normd[i][0].split())))[:max_docs]
    if sel is not picked:
        complete = len(sel) == len(docs)
    text = "\n".join(_fmt_doc(*docs[i]) for i in sel)
    if len(text) > MAX_KB_CTX:
        text, complete = text[:MAX_KB_CTX], False
    return text, problems, (len(sel), complete)


def _fmt_doc(rel, text):
    body = re.sub(r"\n\s*\n", "\n", text)
    return body if body.startswith("#") else "[%s]\n%s" % (rel, body)


def kb_fallback_text(task, small=8000, limit=8000):
    """Second look at the base: the whole base if small, else a wider excerpt."""
    full = kb_full_text(task, limit=small + 1)
    if len(full) <= small:
        return full
    text, _p, _n = kb_context(task, full_limit=0, max_docs=30)
    return text[:limit]


def kb_full_text(task, limit=MAX_KB_CTX):
    docs = kb_docs(task)
    text = "\n".join(_fmt_doc(r, t) for r, t in docs)
    return text[:limit]


# ---------------------------------------------------------------- code

def find_tests(cwd):
    out = []
    for p in list_files(cwd, limit=500):
        b = os.path.basename(p)
        if b.endswith(".py") and (b.startswith("test_") or b.endswith("_test.py")):
            out.append(p)
    return out


def source_files(cwd):
    tests = set(find_tests(cwd))
    return [p for p in list_files(cwd, limit=500) if p.endswith(".py") and p not in tests]


def compress_test_output(out, limit=1200):
    keep = []
    for line in out.splitlines():
        s = line.strip()
        if not s or set(s) <= set("=-") or s.startswith("Traceback (most recent call last)"):
            continue
        if s.startswith(("Ran ", "OK")):
            continue
        if s.startswith("File ") and ("/unittest/" in s or "/lib/python" in s):
            continue
        keep.append(line.rstrip())
    text = "\n".join(keep)
    if len(text) > limit:
        text = text[:limit // 2] + "\n...\n" + text[-limit // 2:]
    return text


def failing_test_names(out):
    return re.findall(r"^(?:FAIL|ERROR): (\w+)", out, re.M)


def extract_functions(src, names):
    """Source of the given test methods/functions (falls back to whole source)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = src.splitlines()
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            chunks.append("\n".join(lines[start:node.end_lineno]))
    return "\n".join(chunks) if chunks else src


# ---------------------------------------------------------------- test contradictions

_SAFE_FUNCS = {"len": len, "sorted": sorted, "abs": abs, "min": min, "max": max, "sum": sum, "set": set,
               "list": list, "tuple": tuple, "str": str, "int": int, "float": float, "bool": bool, "round": round}


def _lit(node):
    try:
        return True, ast.literal_eval(node)
    except Exception:
        return False, None


def _has_call(node):
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def _is_target_call(node):
    """A call of the code under test (m.f(...) or f(...)), not of a pure builtin like len()."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return isinstance(f, ast.Attribute) or (isinstance(f, ast.Name) and f.id not in _SAFE_FUNCS)


class _NoEval(Exception):
    pass


def _small(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > 10 ** 12:
        raise _NoEval()
    if isinstance(v, (str, bytes, list, tuple, set, dict)) and len(v) > 100000:
        raise _NoEval()
    return v


_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
        ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b}


def _ev(n, known):
    """Tiny interpreter over a whitelist of pure, bounded operations (test files are untrusted:
    nothing is ever passed to eval/exec)."""
    if isinstance(n, ast.Call):
        key = ast.dump(n)
        if key in known:
            return known[key]
        if not (isinstance(n.func, ast.Name) and n.func.id in _SAFE_FUNCS) or n.keywords:
            raise _NoEval()
        args = [_small(_ev(a, known)) for a in n.args]
        return _small(_SAFE_FUNCS[n.func.id](*args))
    if isinstance(n, ast.Constant):
        return _small(n.value)
    if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
        vals = [_ev(e, known) for e in n.elts]
        return _small({ast.List: list, ast.Tuple: tuple, ast.Set: set}[type(n)](vals))
    if isinstance(n, ast.Dict):
        return _small({_ev(k, known): _ev(v, known) for k, v in zip(n.keys, n.values) if k is not None})
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.Not, ast.USub, ast.UAdd)):
        v = _ev(n.operand, known)
        return (not v) if isinstance(n.op, ast.Not) else _small(-v if isinstance(n.op, ast.USub) else +v)
    if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)):
        a, b = _small(_ev(n.left, known)), _small(_ev(n.right, known))
        return _small(a + b if isinstance(n.op, ast.Add) else a - b)
    if isinstance(n, ast.BoolOp):
        vals = [_ev(v, known) for v in n.values]
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.Compare) and len(n.ops) == len(n.comparators):
        left = _ev(n.left, known)
        for op, c in zip(n.ops, n.comparators):
            right = _ev(c, known)
            if type(op) not in _CMP or not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
        return _small(_ev(n.value, known)[n.slice.value])
    raise _NoEval()


def _eval_subst(node, known):
    """Evaluate `node` with the known call results substituted. Returns (ok, value)."""
    try:
        return True, _ev(node, known)
    except Exception:
        return False, None


def _target_names(tree):
    """Names through which a test file reaches the code under test: imported modules
    (import mylib as m) and imported functions (from mylib import f), non-stdlib only."""
    import sys
    mods, funcs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in sys.stdlib_module_names:
                    mods.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] not in sys.stdlib_module_names:
            for a in n.names:
                funcs.add(a.asname or a.name)
    return mods, funcs


def _pure_call(node, mods, funcs):
    """m.f(<literals>) or f(<literals>) on the code under test: the only calls whose result
    can be assumed identical wherever they appear (no objects, no state, no variables)."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        if not (isinstance(f.value, ast.Name) and f.value.id in mods):
            return False
    elif not (isinstance(f, ast.Name) and f.id in funcs):
        return False
    for a in list(node.args) + [k.value for k in node.keywords]:
        ok, _ = _lit(a)
        if not ok:
            return False
    return True


def test_contradictions(src):
    """Find assertions that no implementation can satisfy together: the same call required to
    equal two different literals, or a property of a call contradicting its required value."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    mods, funcs = _target_names(tree)
    asserts = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert"):
            asserts.append(n)
    known, problems = {}, []
    for n in asserts:
        if n.func.attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            a, b = n.args[0], n.args[1]
            for call, lit in ((a, b), (b, a)):
                ok, val = _lit(lit)
                if ok and _pure_call(call, mods, funcs):
                    key = ast.dump(call)
                    if key in known and known[key] != val:
                        problems.append("%s expected both %r and %r" % (ast.unparse(call), known[key], val))
                    known.setdefault(key, val)
                    break
    if not known:
        return problems
    for n in asserts:
        attr = n.func.attr
        if attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            for expr, lit in ((n.args[0], n.args[1]), (n.args[1], n.args[0])):
                okl, val = _lit(lit)
                if okl and not _pure_call(expr, mods, funcs) and _has_call(expr):
                    ok, v = _eval_subst(expr, known)
                    if ok and v != val:
                        problems.append("%s contradicts required values" % ast.unparse(n)[:120])
                    break
        elif attr in ("assertTrue", "assertFalse") and n.args:
            ok, val = _eval_subst(n.args[0], known)
            if ok and bool(val) != (attr == "assertTrue"):
                problems.append("%s contradicts required values" % ast.unparse(n)[:120])
        elif attr in ("assertNotEqual", "assertGreater", "assertLess", "assertGreaterEqual", "assertLessEqual",
                      "assertIn", "assertNotIn") and len(n.args) >= 2:
            if not (_has_call(n.args[0]) or _has_call(n.args[1])):
                continue
            ok1, v1 = _eval_subst(n.args[0], known)
            ok2, v2 = _eval_subst(n.args[1], known)
            if not (ok1 and ok2):
                continue
            try:
                res = {"assertNotEqual": v1 != v2, "assertGreater": v1 > v2, "assertLess": v1 < v2,
                       "assertGreaterEqual": v1 >= v2, "assertLessEqual": v1 <= v2,
                       "assertIn": v1 in v2, "assertNotIn": v1 not in v2}[attr]
            except Exception:
                continue
            if not res:
                problems.append("%s contradicts required values" % ast.unparse(n)[:120])
    return problems


def missing_imports(test_src, cwd):
    """Modules imported by a test that exist neither in the stdlib nor in the workspace."""
    import sys
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return []
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods.add(n.module.split(".")[0])
    out = []
    for m in sorted(mods):
        if m in sys.stdlib_module_names or m in sys.builtin_module_names:
            continue
        if os.path.exists(os.path.join(cwd, m + ".py")) or os.path.isdir(os.path.join(cwd, m)):
            continue
        try:
            import importlib.util
            if importlib.util.find_spec(m) is not None:
                continue  # installed in the image
        except (ImportError, ValueError):
            pass
        out.append(m)
    return out


def missing_named_files(task, files=None):
    """Files the instruction names explicitly (e.g. "The file sales.csv ...") must exist."""
    files = files if files is not None else list_files(task.cwd, 2000)
    have = set(os.path.basename(p) for p in files) | set(os.path.relpath(p, task.cwd) for p in files)
    instr = task.short_instruction()
    named = set(re.findall(r"\b([\w./-]+\.(?:csv|tsv|json|md|txt|xlsx|py))\b", instr))
    # Files the task asks to produce are not expected to exist yet.
    named -= set(re.findall(r"\b(?:to|into|as|save|write|create|output)\s+(?:a\s+|the\s+|file\s+)*"
                            r"([\w./-]+\.(?:csv|tsv|json|md|txt|xlsx|py))\b", instr, re.I))
    return ["file %s not found" % n for n in sorted(named)
            if n not in have and os.path.basename(n) not in have and n != "answer.txt"]

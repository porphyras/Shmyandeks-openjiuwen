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
PROSE_FULL = 14000  # a prose base up to this size is sent whole (~400 units)
PROSE_PASSAGES = 9000  # larger prose bases: best paragraphs (BM25, two rounds) up to this size

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

TABLE_EXT = (".csv", ".tsv")


def is_table(path):
    return path.lower().endswith(TABLE_EXT)


def table_sep(path):
    if path.lower().endswith(".tsv"):
        return "\t"
    head = read_text(path, 4096).split("\n", 1)[0]
    return max(("\t", ",", ";"), key=lambda c: (head.count(c), c == ","))


def table_encoding(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(256 * 1024)
    except OSError:
        return "utf-8"
    try:
        raw.decode("utf-8")
        return "utf-8-sig"
    except UnicodeDecodeError as e:
        if e.start > len(raw) - 4:  # cut in the middle of a character at the end of the sample
            return "utf-8-sig"
        return "cp1251"


def pandas_load(path, rel):
    """pd.read_csv call matching the file's separator, encoding and decimal mark."""
    sep = table_sep(path)
    enc = table_encoding(path)
    args = [repr(rel)]
    if sep != ",":
        args.append("sep=%r" % sep)
    if enc != "utf-8-sig":
        args.append("encoding=%r, encoding_errors='replace'" % enc)
    else:
        args.append("encoding='utf-8-sig'")
    head = read_text(path, 8192)
    if sep == ";" and re.search(r"\d,\d", head):
        args.append("decimal=','")
    args.append("skipinitialspace=True, on_bad_lines='skip'")
    return "pd.read_csv(%s)" % ", ".join(args)


def csv_brief(path, head_rows=3, max_distinct=15, max_bytes=16 << 20, name=None):
    """Header, row count, a few rows, distinct values of low-cardinality columns, numeric ranges.
    Streams the file (bounded memory); values are compared stripped."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "%s: not a regular file" % (name or os.path.basename(path)), {}
        sep = table_sep(path)
        f = open(path, encoding=table_encoding(path), errors="replace", newline="")
    except OSError:
        return "%s: unreadable" % (name or os.path.basename(path)), {}
    header, head, nrows, nbytes, truncated = None, [], 0, 0, False
    distinct, lo, hi, numeric = [], [], [], []
    try:
        with f:
            reader = csv.reader(f, delimiter=sep)
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
                    truncated = True
                    break
    except (csv.Error, OSError, ValueError):
        pass
    name = name or os.path.basename(path)
    if header is None:
        return "%s: empty file" % name, {}
    lines = ["%s (%s-separated): %d rows; columns: %s" % (name, "tab" if sep == "\t" else "comma", nrows,
                                                         ",".join(header))]
    lines += [",".join(r) for r in head]
    info = {"header": header, "values": {}, "rows": nrows, "truncated": truncated}
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
    parts, problems, csv_infos, table_paths, soft = [], [], [], [], []
    files = list_files(task.cwd)
    for p in files[:30]:
        rel = os.path.relpath(p, task.cwd)
        if is_table(p):
            brief, info = csv_brief(p, name=rel)
            parts.append(brief)
            csv_infos.append(info)
            table_paths.append(p)
        else:
            t = read_text(p, 600)
            parts.append("%s:\n%s" % (rel, t[:600]))
    instr = task.short_instruction()
    # Cheap feasibility probe: a quoted value right after a column name ("product 'widget'")
    # must occur in that column (case-insensitive). Anything less specific is left to the model.
    for col, term in re.findall(r"\b(\w+)\s+['\"]([^'\"\n]{1,40})['\"]", instr):
        having = [info["values"][col] for info in csv_infos if col in info.get("values", {})]
        # Absent from every table that has this column (and all of them were fully listed).
        if having and all(term.lower() not in {v.lower() for v in vals} for vals in having) and \
                not any(info.get("truncated") for info in csv_infos):
            # Only a hint for the planner (plurals, synonyms, typos): the model decides.
            soft.append("%s '%s' not found in data" % (col, term))
    # Columns announced in the instruction must exist.
    # Columns announced as "The file X has columns a,b,c" must exist in that file. Only the
    # simple single-file comma list is checked; anything less clear is left to the model.
    m = re.search(r"[Tt]he file ([\w./-]+) has columns ([\w]+(?:,[\w]+)+)(?=\s*\(\d+ rows\))", instr)
    if m and csv_infos:
        fname, announced = m.group(1), m.group(2).split(",")
        hdrs = [info for p, info in zip(table_paths, csv_infos)
                if os.path.basename(p) == os.path.basename(fname) or os.path.relpath(p, task.cwd) == fname]
        if len(hdrs) == 1:
            norm = lambda x: re.sub(r"[\s_]+", "_", x.strip().lower())
            hdr = {norm(h) for h in hdrs[0].get("header", [])}
            missing = [c for c in announced if norm(c) not in hdr]
            if missing:
                problems.append("columns missing in file: %s" % ",".join(missing))
    if not files:
        problems.append("no data files")
    # Every table is empty (a header only) and the question asks for a statistic that does not
    # exist for zero rows.
    if csv_infos and all(info.get("rows", 1) == 0 for info in csv_infos) and \
            re.search(r"\b(average|mean|median|maximum|minimum|highest|lowest|largest|smallest|most|least)\b", instr, re.I):
        problems.append("no data rows for an average/extreme")
    if not csv_infos:
        problems += missing_named_files(task, files)  # a named file is only decisive with no tables at all
    text = "\n\n".join(parts)
    if len(text) > MAX_DATA_CTX:
        text = text[:MAX_DATA_CTX] + "\n... (%d files in total)" % len(files)
    task.soft_hints = soft
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


def kb_structured(docs):
    """True for fact-card bases ("Key: Value" lines); False for prose paragraphs."""
    kv = total = 0
    for _r, t in docs[:200]:
        for line in t.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            total += 1
            kv += bool(re.match(r"^[A-Za-z][\w ]{0,30}:\s+\S", line))
    return total > 0 and kv >= 0.5 * total


def _passages(docs, max_len=900):
    """Split documents into paragraph-sized passages, each tagged with its document title."""
    out = []
    for rel, text in docs:
        title = _title(rel, text)
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para or para.startswith("#") and len(para) < 120:
                continue
            for k in range(0, len(para), max_len):
                out.append((title, para[k:k + max_len]))
    return out


def _bm25_rank(passages, query_words, k1=1.2, b=0.75):
    import math
    toks = [_norm(t + " " + p).split() for t, p in passages]
    n = len(toks) or 1
    avg = sum(len(t) for t in toks) / n
    df = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    scores = []
    for i, t in enumerate(toks):
        tf = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        sc = 0.0
        for w in query_words:
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                sc += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        scores.append(sc)
    return sorted(range(len(toks)), key=lambda i: -scores[i]), scores


def prose_passages(docs, instr, limit, extra=""):
    """Two rounds of BM25 over paragraphs: by the question, then by the capitalised names the
    first-round passages mention (the bridge entities of a multi-hop question)."""
    passages = _passages(docs)
    if not passages:
        return ""
    q = [w for w in _norm(instr).split() if w not in _STOP and len(w) > 2]
    if extra:
        # Names the model reached in a first attempt: they lead to the next hop's paragraphs.
        names = re.findall(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+)*)\b", extra)
        q += [w for n in names for w in _norm(n).split() if w not in _STOP and len(w) > 2]
    order, scores = _bm25_rank(passages, q)
    chosen, size = [], 0
    for i in order[:6]:
        if scores[i] <= 0:
            break
        chosen.append(i)
        size += len(passages[i][1])
    bridge = set()
    for i in chosen:
        for name in re.findall(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+)*)\b", passages[i][1]):
            bridge.update(w for w in _norm(name).split() if w not in _STOP and len(w) > 2)
    if bridge:
        order2, scores2 = _bm25_rank(passages, list(bridge) + q)
        for i in order2:
            if size >= limit:
                break
            if i not in chosen and scores2[i] > 0:
                chosen.append(i)
                size += len(passages[i][1])
    return "\n".join("[%s] %s" % passages[i] for i in chosen)[:limit]


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
        pass  # not a free verdict: names come in many forms; the model sees the base instead
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
    if not kb_structured(docs):
        # Prose: link expansion by titles is unreliable, multi-hop facts hide in text.
        if total <= PROSE_FULL:
            sel = list(range(len(docs)))
            text = "\n".join(_fmt_doc(*docs[i]) for i in sel)
            return text, [], (len(sel), True, False)  # whole base sent: NOT FOUND is reliable
        text = prose_passages(docs, instr, PROSE_PASSAGES)
        return text, [], (0, False, False)
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
    return text, problems, (len(sel), complete, True)


def _fmt_doc(rel, text):
    body = re.sub(r"\n\s*\n", "\n", text)
    return body if body.startswith("#") else "[%s]\n%s" % (rel, body)


def kb_fallback_text(task, small=20000, limit=12000, hint=""):
    """Second look at the base: the whole base if it fits `small`, else the best paragraphs
    (BM25, two rounds; `hint` = the first reply, whose names point at the next hop)."""
    full = kb_full_text(task, limit=small + 1)
    if len(full) <= small:
        return full
    return prose_passages(kb_docs(task), task.short_instruction(), limit, extra=hint)


def kb_full_text(task, limit=MAX_KB_CTX):
    docs = kb_docs(task)
    text = "\n".join(_fmt_doc(r, t) for r, t in docs)
    return text[:limit]


# ---------------------------------------------------------------- code

def find_tests(cwd, instruction=""):
    """Test files: test_*.py, *_test.py, tests.py, test.py, and any script the instruction
    runs ("python3 X.py") that exists in the workspace."""
    named = set(re.findall(r"python3?\s+(?:-m\s+\w+\s+)?([\w./-]+\.py)", instruction or ""))
    out = []
    for p in list_files(cwd, limit=500):
        b = os.path.basename(p)
        rel = os.path.relpath(p, cwd)
        if not b.endswith(".py"):
            continue
        if b.startswith("test_") or b.endswith("_test.py") or b in ("tests.py", "test.py"):
            out.append(p)
        elif (rel in named or b in named) and re.search(r"\bunittest\b|\bpytest\b|^\s*assert\b|def test",
                                                        read_text(p, 65536), re.M):
            out.append(p)
    return out


def source_files(cwd, instruction=""):
    tests = set(find_tests(cwd, instruction))
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
        key = _call_key(n)
        if key is not None and key in known:
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


def _call_key(node):
    """Identity of a call by callee text and evaluated arguments, so that m.f(2), m.f(1 + 1)
    and m.f(x=2)-style spellings of the same call compare equal. None if not evaluable."""
    try:
        args = tuple(repr(_ev(a, {})) for a in node.args)
        kws = tuple(sorted((k.arg or "", repr(_ev(k.value, {}))) for k in node.keywords))
    except Exception:
        return None
    return (ast.unparse(node.func), args, kws)


def _pure_call(node, mods, funcs, impure=frozenset()):
    """m.f(<literals>) or f(<literals>) on the code under test, with at least one argument and
    f free of module state: the only calls whose result can be assumed identical wherever they
    appear (no objects, no counters, no variables)."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        if not (isinstance(f.value, ast.Name) and f.value.id in mods):
            return False
        name = f.attr
    elif isinstance(f, ast.Name) and f.id in funcs:
        name = f.id
    else:
        return False
    if name in impure or not (node.args or node.keywords):
        return False
    return _call_key(node) is not None


_MUTATORS = {"append", "extend", "insert", "pop", "remove", "clear", "update", "add", "discard",
             "setdefault", "popitem", "sort", "reverse", "write", "seek", "send", "__setitem__"}


def impure_functions(sources):
    """Functions that read or change module-level state (global/nonlocal, mutating a module
    name, random/time), or any function in a source that defines classes' methods with self
    state - those are never treated as pure by the contradiction check."""
    out = set()
    for src in sources:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        module_names = set()
        for n in tree.body:
            if isinstance(n, ast.Assign):
                module_names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name):
                module_names.add(n.target.id)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            bad = False
            for x in ast.walk(fn):
                if isinstance(x, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom)):
                    bad = True
                elif isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute):
                    base = x.func.value
                    if x.func.attr in _MUTATORS and isinstance(base, ast.Name) and base.id in module_names \
                            and base.id not in local:
                        bad = True
                    if isinstance(base, ast.Name) and base.id in ("random", "time", "datetime", "uuid", "itertools"):
                        bad = True
                elif isinstance(x, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                    targets = x.targets if isinstance(x, (ast.Assign, ast.Delete)) else [x.target]
                    for tg in targets:
                        root = tg
                        while isinstance(root, (ast.Subscript, ast.Attribute)):
                            root = root.value
                        if isinstance(root, ast.Name) and root.id in module_names and root.id not in local \
                                and root is not tg:
                            bad = True
            if bad:
                out.add(fn.name)
    return out


def test_contradictions(src, impure=frozenset()):
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
    known, problems, almost = {}, [], {}
    for n in asserts:
        if n.func.attr == "assertAlmostEqual" and len(n.args) >= 2 and _pure_call(n.args[0], mods, funcs, impure):
            ok, val = _lit(n.args[1])
            places = 7
            for kw in n.keywords:
                if kw.arg == "places" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                    places = kw.value.value
            if len(n.args) >= 3 and isinstance(n.args[2], ast.Constant) and isinstance(n.args[2].value, int):
                places = n.args[2].value
            if ok and isinstance(val, (int, float)) and not any(k.arg == "delta" for k in n.keywords):
                key = _call_key(n.args[0])
                for v0, p0 in almost.get(key, []):
                    if abs(v0 - val) > 2 * 10 ** (-min(places, p0)):
                        problems.append("%s expected both %r and %r" % (ast.unparse(n.args[0])[:80], v0, val))
                almost.setdefault(key, []).append((val, places))
        if n.func.attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            a, b = n.args[0], n.args[1]
            for call, lit in ((a, b), (b, a)):
                ok, val = _lit(lit)
                if ok and _pure_call(call, mods, funcs, impure):
                    key = _call_key(call)
                    if key is None:
                        break
                    if key in known and known[key] != val:
                        problems.append("%s expected both %r and %r" % (ast.unparse(call), known[key], val))
                    known.setdefault(key, val)
                    break
    problems += _self_contradictions(asserts, mods, funcs, impure)
    if not known:
        return problems
    for n in asserts:
        attr = n.func.attr
        if attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            for expr, lit in ((n.args[0], n.args[1]), (n.args[1], n.args[0])):
                okl, val = _lit(lit)
                if okl and not _pure_call(expr, mods, funcs, impure) and _has_call(expr):
                    ok, v = _eval_subst(expr, known)
                    if ok and v != val:
                        problems.append("%s contradicts required values" % ast.unparse(n)[:120])
                    break
        elif attr in ("assertIsInstance", "assertNotIsInstance") and len(n.args) >= 2:
            ok, v = _eval_subst(n.args[0], known)
            types = _type_tuple(n.args[1])
            if ok and types and (isinstance(v, types) != (attr == "assertIsInstance")):
                problems.append("%s contradicts required values" % ast.unparse(n)[:120])
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


_TYPES = {"int": int, "str": str, "float": float, "list": list, "dict": dict, "tuple": tuple, "bool": bool,
          "set": set, "bytes": bytes, "type(None)": type(None)}


def _type_tuple(node):
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    out = []
    for x in names:
        t = _TYPES.get(ast.unparse(x))
        if t is None:
            return None
        out.append(t)
    return tuple(out)


def _deterministic_expr(node, mods, funcs):
    """Built only from literals and calls into the code under test (m.f(1), m.C(7).value())."""
    for x in ast.walk(node):
        if isinstance(x, ast.Name) and x.id not in mods and x.id not in funcs and x.id not in _SAFE_FUNCS:
            return False
        if isinstance(x, (ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield)):
            return False
    return _has_call(node)


def _uses_impure(node, impure):
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            f = x.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in impure:
                return True
            if not (x.args or x.keywords) and not (isinstance(f, ast.Name) and f.id in _SAFE_FUNCS) \
                    and not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call)):
                return True  # zero-argument module calls (counters, ids, new objects) may differ
    return False


def _self_contradictions(asserts, mods, funcs, impure=frozenset()):
    """x > x, x < x, x != x can never hold when x is a pure call m.f(<literals>) of a function
    the tests elsewhere treat as pure (compared with a literal). Constructors, counters and
    id generators are excluded: two calls may legitimately differ."""
    out = []
    def shape(x):
        class Blank(ast.NodeTransformer):
            def visit_Constant(self, c):
                return ast.copy_location(ast.Name(id="_", ctx=ast.Load()), c)
        return ast.unparse(Blank().visit(ast.parse(ast.unparse(x), mode="eval")))

    pure_shapes = set()
    for n in asserts:
        if n.func.attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            for call, lit in ((n.args[0], n.args[1]), (n.args[1], n.args[0])):
                if isinstance(call, ast.Call) and _lit(lit)[0] and _deterministic_expr(call, mods, funcs) and not _uses_impure(call, impure):
                    pure_shapes.add(shape(call))

    def pure_expr(x):
        # same call shape compared with a literal elsewhere; not a bare constructor call
        return (isinstance(x, ast.Call) and _deterministic_expr(x, mods, funcs) and not _uses_impure(x, impure) and shape(x) in pure_shapes
                and not ast.unparse(x.func).split(".")[-1][:1].isupper())
    strict = (ast.Lt, ast.Gt, ast.NotEq)
    for n in asserts:
        attr = n.func.attr
        pair, bad = None, False
        if attr in ("assertTrue", "assertFalse") and n.args and isinstance(n.args[0], ast.Compare) \
                and len(n.args[0].ops) == 1:
            c = n.args[0]
            pair = (c.left, c.comparators[0])
            op = c.ops[0]
            bad = isinstance(op, strict) if attr == "assertTrue" else isinstance(op, (ast.Eq, ast.LtE, ast.GtE))
        elif attr in ("assertGreater", "assertLess", "assertNotEqual") and len(n.args) >= 2:
            pair, bad = (n.args[0], n.args[1]), True
        if pair and bad and ast.dump(pair[0]) == ast.dump(pair[1]) and pure_expr(pair[0]):
            out.append("%s can never hold" % ast.unparse(n)[:120])
    # The same expression required to be instances of unrelated builtin types (int and str).
    req = {}
    for n in asserts:
        if n.func.attr == "assertIsInstance" and len(n.args) >= 2 and _deterministic_expr(n.args[0], mods, funcs):
            types = _type_tuple(n.args[1])
            if types:
                req.setdefault(ast.dump(n.args[0]), []).append((types, n))
    for items in req.values():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a_t, b_t = items[i][0], items[j][0]
                if not any(issubclass(x, y) or issubclass(y, x) for x in a_t for y in b_t):
                    out.append("%s requires both %s and %s" % (ast.unparse(items[i][1].args[0])[:80],
                                                               ast.unparse(items[i][1].args[1]),
                                                               ast.unparse(items[j][1].args[1])))
    return out


def missing_fixtures(test_src, cwd):
    """Files a test reads by a literal path that do not exist (only the source may be edited)."""
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return []
    reads, writes = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and ast.unparse(n.func) in ("open", "io.open", "Path", "pathlib.Path") and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            path = n.args[0].value
            mode = ""
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = str(n.args[1].value)
            for k in n.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = str(k.value.value)
            (writes if any(ch in mode for ch in "wax+") else reads).add(path)
    if any(isinstance(n, ast.Attribute) and n.attr in ("write_text", "write_bytes", "mkdir", "touch", "makedirs",
                                                       "copy", "copyfile", "copytree", "mkstemp", "mkdtemp",
                                                       "TemporaryDirectory", "NamedTemporaryFile") for n in ast.walk(tree)):
        return []
    # A path also passed to the code under test (m.export("report.csv")) is produced by it.
    produced = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and not (ast.unparse(n.func) in ("open", "io.open", "Path", "pathlib.Path")):
            for a in list(n.args) + [k.value for k in n.keywords]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    produced.add(a.value)
    reads -= produced
    return ["test reads missing file %s" % p for p in sorted(reads - writes)
            if re.search(r"\.\w{1,5}$", p) and not os.path.exists(os.path.join(cwd, p))]


def _module_in_tree(cwd, m):
    for p in list_files(cwd, limit=2000):
        b = os.path.basename(p)
        if b == m + ".py" or (b == "__init__.py" and os.path.basename(os.path.dirname(p)) == m):
            return True
    return False


def missing_imports(test_src, cwd):
    """Modules imported by a test that exist neither in the stdlib nor in the workspace."""
    import sys
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return []
    guarded = set()
    for n in ast.walk(tree):  # imports inside try/except ImportError are optional
        if isinstance(n, ast.Try) and any(h.type is None or "ImportError" in ast.unparse(h.type)
                                          or "ModuleNotFoundError" in ast.unparse(h.type) for h in n.handlers):
            for x in n.body:
                for y in ast.walk(x):
                    if isinstance(y, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(y))
    mods, dotted = set(), set()
    for n in ast.walk(tree):
        if id(n) in guarded:
            continue
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
            dotted.update(a.name for a in n.names if "." in a.name)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods.add(n.module.split(".")[0])
            if "." in n.module:
                dotted.add(n.module)
    out = []
    # A submodule of a local package that is not there ("from pkg.extra import ...").
    for d in sorted(dotted):
        parts = d.split(".")
        for root in [cwd] + sorted({os.path.dirname(os.path.dirname(p)) for p in list_files(cwd, 2000)
                                   if os.path.basename(os.path.dirname(p)) == parts[0]}):
            base = os.path.join(root, parts[0])
            if os.path.isdir(base):
                sub = os.path.join(root, *parts)
                if not (os.path.isdir(sub) or os.path.isfile(sub + ".py")):
                    out.append(d)
                break
    for m in sorted(mods):
        if m in sys.stdlib_module_names or m in sys.builtin_module_names:
            continue
        if os.path.exists(os.path.join(cwd, m + ".py")) or os.path.isdir(os.path.join(cwd, m)) \
                or _module_in_tree(cwd, m):
            continue  # local module (also src/ layouts)
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

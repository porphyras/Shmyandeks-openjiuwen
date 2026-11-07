"""Task loading and cheap, LLM-free inspection: compact context and feasibility signals."""
import ast
import csv
import hashlib
import io
import json
import os
import re

MAX_TASK_JSON = 1 << 20
MAX_FILE_READ = 256 * 1024

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
        if not (self.reward == self.reward) or self.reward < 0:  # NaN / negative
            self.reward = 0.0
        ws = os.path.join(task_dir, "workspace")
        self.workspace = ws if os.path.isdir(ws) else None
        self.cwd = self.workspace or task_dir
        self.wants_done = bool(re.search(r"word DONE", self.instruction))

    def short_instruction(self):
        s = self.instruction
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
            with open(tj, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
        except (OSError, ValueError):
            continue
        t = Task(d, data)
        if t.instruction:
            tasks.append(t)
    return tasks


def read_text(path, limit=MAX_FILE_READ):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def list_files(root, limit=5000):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(x for x in dn if x not in ("__pycache__", ".git"))
        for f in sorted(fn):
            if f == "answer.txt" or f.startswith(".answer.txt"):
                continue
            out.append(os.path.join(dp, f))
            if len(out) >= limit:
                return out
    return out


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- tabular data

def csv_brief(path, head_rows=3, max_distinct=15):
    """Header, row count, a few rows, distinct values of low-cardinality columns, numeric ranges."""
    text = read_text(path, 64 << 20)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return "%s: empty file" % os.path.basename(path), {}
    header, body = rows[0], [r for r in rows[1:] if r]
    lines = ["%s: %d rows; columns: %s" % (os.path.basename(path), len(body), ",".join(header))]
    for r in body[:head_rows]:
        lines.append(",".join(r))
    info = {"header": header, "values": {}}
    for i, col in enumerate(header):
        vals = [r[i] for r in body if i < len(r)]
        nums = []
        for v in vals:
            try:
                nums.append(float(v))
            except ValueError:
                nums = None
                break
        if nums:
            lo, hi = min(nums), max(nums)
            fmt = lambda x: str(int(x)) if x == int(x) else str(x)
            lines.append("%s: %s..%s" % (col, fmt(lo), fmt(hi)))
        else:
            distinct = sorted(set(vals))
            info["values"][col] = set(distinct)
            if len(distinct) <= max_distinct:
                lines.append("%s values: %s" % (col, ",".join(distinct)))
            else:
                lines.append("%s: %d distinct values" % (col, len(distinct)))
    return "\n".join(lines), info


def quoted_terms(instruction):
    return [m for m in re.findall(r"'([^'\n]{1,40})'|\"([^\"\n]{1,40})\"", instruction) for m in m if m]


def data_context(task):
    """Returns (context_text, problems) for a workspace with data files."""
    parts, problems, csv_infos = [], [], []
    files = list_files(task.cwd)
    for p in files:
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
    return "\n\n".join(parts), problems


# ---------------------------------------------------------------- knowledge base

_STOP = set("the a an in of which what who whom whose is was were did does do for to and or by on at from with "
            "year city born employer headquarters company person directory contains small knowledge base "
            "markdown files use file tools bash ls grep cat find it work only inside current when you are done "
            "write final answer how many".split())


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def kb_docs(task):
    docs = []
    for p in list_files(task.cwd, limit=3000):
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
    return [c for c in caps if _norm(c) not in ("the directory",)]


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
    if picked and total > full_limit:
        sel = picked
    elif total <= 12000:
        sel = list(range(len(docs)))
    else:
        # Nothing matched and the KB is big: rank by plain word overlap.
        words = set(w for w in _norm(instr).split() if w not in _STOP and len(w) > 2)
        sel = sorted(range(len(docs)), key=lambda i: -len(words & set(normd[i][0].split())))[:max_docs]
    text = "\n".join(_fmt_doc(*docs[i]) for i in sel)
    return text, problems, len(sel)


def _fmt_doc(rel, text):
    body = re.sub(r"\n\s*\n", "\n", text)
    return body if body.startswith("#") else "[%s]\n%s" % (rel, body)


def kb_full_text(task, limit=60000):
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

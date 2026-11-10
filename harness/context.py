"""Task loading and cheap, LLM-free inspection: compact context and feasibility signals."""
import ast
import collections
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


def table_skip(path):
    """Leading lines before the header: comments ("# export 2024-05-01") and blank lines."""
    n = 0
    for line in read_text(path, 8192).lstrip("\ufeff").split("\n")[:20]:
        if line.strip() and not line.lstrip().startswith("#"):
            return n
        n += 1
    return 0


def table_sep(path):
    if path.lower().endswith(".tsv"):
        return "\t"
    lines = read_text(path, 8192).split("\n")
    head = lines[table_skip(path)] if table_skip(path) < len(lines) else lines[0]
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
    # Decimal comma only when every "d,d" looks like a decimal (1-2 digits after the comma)
    # and no decimal point is used; "1,250" is a thousands separator (handled later).
    commas = re.findall(r"\d,(\d+)", head)
    if sep == ";" and commas and all(len(x) <= 2 for x in commas) and not re.search(r"\d\.\d", head):
        args.append("decimal=','")
    skip = table_skip(path)
    if skip:
        args.append("skiprows=%d" % skip)
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
            for _ in range(table_skip(path)):
                f.readline()
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
    lines = ["%s (%s-separated): %d rows; columns: %s" % (name, {"\t": "tab", ";": "semicolon"}.get(sep, "comma"),
                                                         nrows, ",".join(header))]
    lines += [(";" if sep == ";" else ",").join(r) for r in head]  # "2,50" stays one decimal-comma value
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
            # Only a column the question itself uses makes it unanswerable; an announced column
            # the question never needs ("..., discount") is only a hint for the model.
            question = instr[m.end():].lower()
            used = [c for c in missing if re.search(r"\b%s\b" % re.escape(c.lower().replace("_", " ")),
                                                     question.replace("_", " "))]
            if used:
                problems.append("columns missing in file: %s" % ",".join(used))
            elif missing:
                soft.append("announced columns missing in file: %s" % ",".join(missing))
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
        if os.path.splitext(p)[1].lower() in (".md", ".markdown", ".rst", ".txt", ".adoc", ".org", ".json", ".yaml",
                                              ".yml", ".csv", ".tsv", ".html", ".htm", ".xml", ".ini", ".cfg", ""):
            docs.append((os.path.relpath(p, task.cwd), read_text(p, 65536).strip()))
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


def prose_passages(docs, instr, limit, extra="", own_articles=0):
    """Two rounds of BM25 over paragraphs: by the question, then by the capitalised names the
    first-round passages mention (the bridge entities of a multi-hop question)."""
    passages = _passages(docs)
    if not passages:
        return ""
    q = [w for w in _norm(instr).split() if w not in _STOP and len(w) > 2]
    if extra:
        # Names the model reached in a first attempt: they lead to the next hop's paragraphs.
        names = re.findall(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+)*)\b", extra)
        q += [w for n in names if not n.isupper() for w in _norm(n).split()
              if w not in _STOP and w not in ("answer", "not", "found", "reply", "knowledge") and len(w) > 2]
    order, scores = _bm25_rank(passages, q)
    chosen, size = [], 0
    if extra and own_articles:
        # The entities the model already reached (named in its previous reply) are described in
        # their own articles: those come first, the latest-named entity first (the next hop).
        own = {}
        for rel, text in docs:
            for key in (_norm(_title(rel, text)), _norm(os.path.splitext(os.path.basename(rel))[0])):
                if len(key) >= 5:
                    own.setdefault(key, _title(rel, text))
        seen = []
        for n in reversed(re.findall(r"\b([A-Z][\w\-']+(?:\s+[A-Z][\w\-']+)+)\b", extra)):
            t = own.get(_norm(n))
            if t and t not in seen:
                seen.append(t)
        for t in seen[:own_articles]:
            for i, (pt, para) in enumerate(passages):
                if pt == t and i not in chosen and size < limit // 2:
                    chosen.append(i)
                    size += len(para)
    for i in order[:6]:
        if i in chosen:
            continue
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


def _name_forms(name):
    """Patterns for a person or company name as a base may write it: in full, "J. Muller",
    "J Muller" (two-word names only)."""
    words = name.split()
    pats = [r"\b%s\b" % r"\s+".join(map(re.escape, words))]
    if len(words) == 2 and words[0][0].isupper():
        pats.append(r"\b%s\.?\s+%s\b" % (re.escape(words[0][0]), re.escape(words[1])))
    return re.compile("|".join(pats))


# How bases word the attributes questions ask about ("founded" -> "started in 2000", "HQ: Tbilisi").
_ATTR_WORDS = {
    "founded": ("started", "established", "dates", "since", "launched", "created", "formed"),
    "headquarters": ("hq", "headquartered", "based", "head", "office", "offices"),
    "headquartered": ("hq", "headquarters", "based", "head", "office"),
    "born": ("birth", "birthday", "year"),
    "employs": ("works", "employer", "joined", "behalf", "staff", "engineer", "analyst"),
    "employer": ("works", "employs", "joined", "behalf", "staff", "engineer", "analyst"),
}


def entity_passages(docs, instr, names, limit=8000):
    """Targeted look for names the model reached but whose facts it did not see: paragraphs
    that mention a name in any of its written forms, each with the paragraph after it (a
    profile block "Birth year: 1988" follows its introduction), and the opening paragraphs of
    documents named after the entity (kb/interview_jonas_muller.md). Each name gets an equal
    share of `limit`; within it the entity's own documents come first, then mentions ranked by
    the question's words. Names the base never mentions are dropped."""
    by_doc = []
    for rel, text in docs:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        paras = [p for p in paras if not (p.startswith("#") and len(p) < 120)]
        by_doc.append((rel, _title(rel, text), paras))
    q = [w for w in _norm(instr).split() if w not in _STOP and len(w) > 2]
    for w in list(q):
        q += _ATTR_WORDS.get(w, ())
    groups = []
    for name in names:
        pat = _name_forms(name)
        slug = _norm(name).replace(" ", "_")
        own, ment = [], []
        for d, (rel, title, paras) in enumerate(by_doc):
            mine = slug in os.path.basename(rel).lower() or _norm(name) in _norm(title)
            for j, p in enumerate(paras):
                if mine and j < 3:
                    own.append((d, j))
                if pat.search(p):
                    ment.append((d, j))
        if not own and not ment:
            continue
        if ment:
            order, _ = _bm25_rank([(by_doc[d][1], by_doc[d][2][j]) for d, j in ment], q)
            ment = [ment[i] for i in order]
        groups.append((own, ment))
    if not groups:
        return ""
    share = limit // len(groups)
    chosen = []
    for own, ment in groups:
        size = 0
        for d, j in own + [x for dj in ment for x in (dj, (dj[0], dj[1] + 1))]:
            if size >= share:
                break
            if j < len(by_doc[d][2]) and (d, j) not in chosen:
                chosen.append((d, j))
                size += len(by_doc[d][2][j])
    return "\n".join("[%s] %s" % (by_doc[d][1], by_doc[d][2][j][:900]) for d, j in chosen)[:limit]


def reply_names(instr, reply, top=3):
    """Names a reply reached beyond the question (most mentioned first), then the question's."""
    import collections
    q = _entities(instr)
    found = re.findall(r"\b([A-Z][a-z][\w\-]*(?:\s+[A-Z][a-z][\w\-]*){1,2})\b", reply or "")
    c = collections.Counter(n for n in found if n not in q)
    return [n for n, _ in c.most_common(top)] + q


def kb_context(task, full_limit=600, max_docs=12):
    """Pick the documents mentioning the entities of the question and follow name links
    (a document that mentions another document's title pulls it in). Falls back to the
    whole KB when it is small or nothing matched."""
    docs = kb_docs(task)
    problems = []
    if not docs:
        return "", ["no knowledge base files"], (0, False, True)
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


def kb_fallback_text(task, small=20000, limit=12000, hint="", own_articles=0):
    """Second look at the base: the whole base if it fits `small`, else the best paragraphs
    (BM25, two rounds; `hint` = the first reply, whose names point at the next hop)."""
    full = kb_full_text(task, limit=small + 1)
    if len(full) <= small:
        return full
    return prose_passages(kb_docs(task), task.short_instruction(), limit, extra=hint, own_articles=own_articles)


def kb_bytes(task, max_files=3000):
    """Bytes of the task's files (the knowledge base of a search task); bounded walk."""
    total, n = 0, 0
    for dp, _, fs in os.walk(task.cwd):
        for f in fs:
            if f == "answer.txt":
                continue
            n += 1
            if n > max_files:
                return total
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


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

def _small_range(*a):
    r = range(*a)
    if len(r) > 100000:
        raise ValueError("range too large to evaluate")
    return r


_SAFE_FUNCS = {"len": len, "sorted": sorted, "abs": abs, "min": min, "max": max, "sum": sum, "set": set,
               "list": list, "tuple": tuple, "str": str, "int": int, "float": float, "bool": bool, "round": round,
               "range": _small_range, "dict": dict}


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
    if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)):
        a, b = _ev(n.left, known), _ev(n.right, known)
        num = (int, float)
        if not (isinstance(a, num) and isinstance(b, num)) or isinstance(a, bool) or isinstance(b, bool):
            raise _NoEval()  # numbers only: no sequence repetition, no string formatting
        if not isinstance(n.op, ast.Mult) and b == 0:
            raise _NoEval()
        op = {ast.Mult: lambda x, y: x * y, ast.Div: lambda x, y: x / y,
              ast.FloorDiv: lambda x, y: x // y, ast.Mod: lambda x, y: x % y}[type(n.op)]
        return _small(op(a, b))
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
    if name in impure:
        return False
    return _call_key(node) is not None


_MUTATORS = {"append", "extend", "insert", "pop", "remove", "clear", "update", "add", "discard",
             "setdefault", "popitem", "sort", "reverse", "write", "seek", "send", "__setitem__"}


# Modules whose functions build values from their arguments (no hidden state, no clock).
_PURE_MODULES = {"math", "re", "string", "datetime", "decimal", "fractions", "statistics", "functools",
                 "collections", "itertools", "operator", "json", "copy", "textwrap", "unicodedata", "cmath"}
import builtins as builtins_mod  # noqa: E402
_SAFE_BUILTIN_NAMES = {"ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError", "Exception",
                       "ArithmeticError", "OverflowError", "RuntimeError", "NotImplementedError", "AssertionError",
                       "isinstance", "issubclass", "callable", "getattr", "hasattr", "iter", "next", "print"}


def unproven_functions(sources):
    """Module-level names a test may call that are NOT provably pure, for the contradiction
    checks ("impure until proven otherwise"). A call m.f(...) counts as pure only if f is a plain
    module-level def (no decorator, no mutable default) whose body reads only its parameters,
    its locals, pure builtins, functions of _PURE_MODULES, other provably pure defs and scalar
    constants that are bound once and never rebound (global) or written through an attribute or
    index anywhere; Name.attr on module data (Config.limit) is not pure. Everything else bound at
    module level (classes, names bound by assignment such as bump = make_counter(), defs that do
    not qualify) is returned."""
    out = set()
    for src in sources:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        defs, others, pure_imports = {}, set(), {}
        bindings = collections.Counter()
        scalar = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name in defs:
                    others.add(n.name)  # defined twice
                defs[n.name] = n
            elif isinstance(n, ast.ClassDef):
                others.add(n.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    root = a.name.split(".")[0]
                    if root in _PURE_MODULES:
                        pure_imports[a.asname or root] = root
                    else:
                        others.add(a.asname or root)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if (n.module or "").split(".")[0] in _PURE_MODULES:
                        pure_imports[a.asname or a.name] = n.module
                    else:
                        others.add(a.asname or a.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            bindings[x.id] += 1
                            if isinstance(t, ast.Name) and _scalar_literal(n.value):
                                scalar.add(x.id)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                for x in ast.walk(n.target):
                    if isinstance(x, ast.Name):
                        bindings[x.id] += 2
        # names written anywhere through global/nonlocal, attribute or index
        rebound = set()
        for x in ast.walk(tree):
            if isinstance(x, (ast.Global, ast.Nonlocal)):
                rebound.update(x.names)
            elif isinstance(x, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = x.targets if isinstance(x, (ast.Assign, ast.Delete)) else [x.target]
                for t in targets:
                    if isinstance(t, (ast.Attribute, ast.Subscript)):
                        r = t
                        while isinstance(r, (ast.Attribute, ast.Subscript)):
                            r = r.value
                        if isinstance(r, ast.Name):
                            rebound.add(r.id)
        constants = {nm for nm in scalar if bindings[nm] == 1 and nm not in rebound}
        others |= {nm for nm in bindings if nm not in constants and nm not in defs}
        cand = set()
        for name, fn in defs.items():
            if fn.decorator_list or name in others:
                continue
            defaults = list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d is not None]
            if any(not _scalar_literal(d) for d in defaults):
                continue  # def add(x, seen=[]): the default keeps state between calls
            if any(isinstance(x, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom)) for x in ast.walk(fn)):
                continue
            cand.add(name)
        changed = True
        while changed:  # drop defs reading anything not proven pure
            changed = False
            for name in sorted(cand):
                fn = defs[name]
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
                if fn.args.vararg:
                    params.add(fn.args.vararg.arg)
                if fn.args.kwarg:
                    params.add(fn.args.kwarg.arg)
                local = params | {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
                local |= {x.name for x in ast.walk(fn) if isinstance(x, ast.FunctionDef) and x is not fn}
                ok = True
                for x in ast.walk(fn):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in local:
                        if x.id in cand or x.id in constants or x.id in pure_imports or x.id in _PURE_BUILTINS \
                                or x.id in ("True", "False", "None") or hasattr(builtins_mod, x.id) and x.id in _SAFE_BUILTIN_NAMES:
                            continue
                        ok = False
                        break
                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id not in local \
                            and x.value.id not in pure_imports:
                        ok = False  # Config.limit, REGISTRY.items: module data read through an attribute
                        break
                if not ok:
                    cand.discard(name)
                    changed = True
        out |= set(defs) - cand
        out |= others
    return out


def _scalar_literal(node):
    """A constant that is a scalar or a tuple of scalars (LIMIT = 3, SEP = '-', AXES = ('x', 'y'))."""
    try:
        v = ast.literal_eval(node)
    except Exception:
        return False
    flat = v if isinstance(v, tuple) else (v,)
    return all(isinstance(x, (int, float, str, bytes, bool, type(None))) for x in flat)


_CLOCK_CALLS = {"now", "today", "utcnow", "monotonic", "perf_counter", "time_ns", "process_time",
                "monotonic_ns", "perf_counter_ns", "urandom", "getenv", "getrandbits"}


# Value constructors imported by name (from datetime import date, timedelta; Decimal; Fraction).
# A module function of the same name is a definition and is resolved as usual.
_VALUE_TYPES = {"date", "datetime", "timedelta", "Decimal", "Fraction"}


_PURE_BUILTINS = {"len", "abs", "min", "max", "sum", "sorted", "round", "str", "int", "float", "bool",
                  "isinstance", "range", "list", "tuple", "dict", "set", "frozenset", "enumerate", "zip",
                  "any", "all", "reversed", "divmod", "pow", "repr", "ord", "chr", "map", "filter", "hash",
                  "type", "super", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError"}


def impure_functions(sources):
    """Names of functions/methods that may keep or change state, decided conservatively and
    transitively: module data or self attributes they change (directly, through mutator
    calls, heapq/bisect-style helpers, setattr, next() on stored iterators), self passed to
    any non-builtin call, global/nonlocal, generators, random/time - and anything that calls
    such a function or method. Only these never count as pure calls in contradiction checks."""
    out = set()
    defs, calls = {}, {}
    for src in sources:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        module_names = set()
        bindings = collections.Counter()
        scalar = set()
        for n in tree.body:
            if isinstance(n, ast.Assign):
                names = {t.id for t in n.targets if isinstance(t, ast.Name)}
                module_names |= names
                for nm in names:
                    bindings[nm] += 1
                    if _scalar_literal(n.value):
                        scalar.add(nm)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name):
                module_names.add(n.target.id)
                bindings[n.target.id] += 2  # an annotated or augmented binding is not a plain constant
        # Module data a function may READ and another call (or the test) may change: anything but
        # a scalar constant bound once (registries, caches, dicts of state, instances).
        state_names = {nm for nm in module_names if not (nm in scalar and bindings[nm] == 1)}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            fn_stores = {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            is_method = bool(fn.args.args) and fn.args.args[0].arg == "self"
            stateful = set(module_names) | ({"self"} if is_method else set())
            bad = False
            called = set()

            def root_name(x):
                while isinstance(x, (ast.Attribute, ast.Subscript, ast.Call)):
                    x = x.func if isinstance(x, ast.Call) else x.value
                return x.id if isinstance(x, ast.Name) else None

            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in state_names \
                        and x.id not in local and x.id not in fn_stores:
                    bad = True  # reads module state another call may change between two assertions
                if isinstance(x, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom)):
                    bad = True
                elif isinstance(x, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                    targets = x.targets if isinstance(x, (ast.Assign, ast.Delete)) else [x.target]
                    for tg in targets:
                        r = root_name(tg)
                        if r in stateful and (r not in local or r == "self") and r != getattr(tg, "id", None):
                            bad = True
                elif isinstance(x, ast.Call):
                    f = x.func
                    if isinstance(f, ast.Attribute):
                        base = root_name(f.value)
                        if base == "self" and isinstance(f.value, ast.Name):
                            called.add(f.attr)  # self.other_method(...)
                        if f.attr in _MUTATORS and base in stateful and (base not in local or base == "self"):
                            bad = True
                        if base in ("random", "time", "uuid", "itertools", "secrets", "os",
                                    "heapq", "bisect", "sys", "io"):
                            bad = True
                        # datetime builds values (date(...), timedelta(...), strptime): only the
                        # clock is state, under any import spelling (date.today(), datetime.now())
                        if f.attr in _CLOCK_CALLS:
                            bad = True
                    elif isinstance(f, ast.Name):
                        if f.id in ("setattr", "delattr", "next", "iter", "input", "open", "exec", "eval", "print"):
                            bad = True
                        elif f.id not in _PURE_BUILTINS:
                            called.add(f.id)  # a module function (resolved transitively)
                    # self or stored state handed to a non-builtin call may be changed there
                    fname = getattr(f, "id", None)
                    if fname not in _PURE_BUILTINS:
                        for a in list(x.args) + [k.value for k in x.keywords]:
                            r = root_name(a)
                            if r == "self" or (r in module_names and r not in local):
                                bad = True
            defs.setdefault(fn.name, False)
            defs[fn.name] = defs[fn.name] or bad
            calls.setdefault(fn.name, set()).update(called)
    out = {n for n, b in defs.items() if b}
    changed = True
    while changed:  # anything calling an impure (or unknown non-builtin) function is impure
        changed = False
        for n, cs in calls.items():
            if n not in out and any(c in out or (c not in defs and c not in _VALUE_TYPES) for c in cs):
                out.add(n)
                changed = True
    return out

_WRAPPERS = {"len", "sorted", "set", "str", "abs", "sum", "min", "max", "list", "tuple", "bool"}


def _expr_key(node, mods, funcs, impure):
    """A key for a pure call, or for a builtin applied to one (len(m.split('a b'))); else None."""
    if _pure_call(node, mods, funcs, impure):
        return _call_key(node)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _WRAPPERS \
            and len(node.args) == 1 and not node.keywords and _pure_call(node.args[0], mods, funcs, impure):
        inner = _call_key(node.args[0])
        return None if inner is None else ("wrap", node.func.id, inner)
    return None


def _is_assert(st):
    return isinstance(st, ast.Assert) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and
                                          isinstance(st.value.func, ast.Attribute) and
                                          st.value.func.attr.startswith("assert"))


def _substitute_pure_vars(tree, mods, funcs, impure):
    """In each test function, `r = m.f(<literals>)` followed only by assertions that read r
    (never r.method(...), never a reassignment, never any other statement naming r): the
    assertions are rewritten with the call itself, so the checks below compare it with the same
    call elsewhere. Anything that could change r ends the substitution."""
    import copy
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        live = {}
        for st in fn.body:
            names = {x.id for x in ast.walk(st) if isinstance(x, ast.Name)}
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                v = st.targets[0].id
                live.pop(v, None)
                for other in list(live):
                    if other in names:
                        live.pop(other)
                if _pure_call(st.value, mods, funcs, impure):
                    live[v] = st.value
                continue
            if not _is_assert(st):
                for v in list(live):
                    if v in names:
                        live.pop(v)
                continue
            # r.method(...) or r.attr inside the assertion may change or depend on r's identity, and
            # r handed to any call but a builtin wrapper or the assertion itself may be changed there
            touched = {x.value.id for x in ast.walk(st) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)}
            for c in ast.walk(st):
                if isinstance(c, ast.Call):
                    f = c.func
                    allowed = (isinstance(f, ast.Name) and f.id in _WRAPPERS) or (
                        isinstance(f, ast.Attribute) and f.attr.startswith("assert"))
                    if not allowed:
                        touched |= {a.id for a in list(c.args) + [k.value for k in c.keywords]
                                    if isinstance(a, ast.Name)}
            for v in list(live):
                if v in touched:
                    live.pop(v)

            class Sub(ast.NodeTransformer):
                def visit_Name(self, node):
                    if isinstance(node.ctx, ast.Load) and node.id in live:
                        return copy.deepcopy(live[node.id])
                    return node
            Sub().visit(st)


def _patches_module(tree, mods):
    """The test replaces something at run time: a patch/setattr/monkeypatch call or decorator,
    or an assignment into the module under test (m.LIMIT = 2). Importing mock alone is not."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = ast.unparse(n.func)
            last = name.rsplit(".", 1)[-1]
            if last in ("patch", "object", "dict", "multiple", "setattr", "setitem", "delattr", "setenv") and (
                    "patch" in name or last in ("setattr", "setitem", "delattr", "setenv")):
                return True
            if last in ("vars", "globals", "reload", "exec", "eval", "__import__"):
                return True  # vars(m)['LIMIT'] = 2, importlib.reload(m), ...
        if isinstance(n, ast.Attribute) and n.attr == "__dict__":
            return True
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]):
                r = t  # any write through the module: m.X = 2, m.Config.limit = 2, m.REG[...] = ...
                while isinstance(r, (ast.Attribute, ast.Subscript)):
                    r = r.value
                if r is not t and isinstance(r, ast.Name) and r.id in mods:
                    return True
    return False


def test_contradictions(src, impure=frozenset()):
    """Find assertions that no implementation can satisfy together: the same call required to
    equal two different literals, or a property of a call contradicting its required value."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    mods, funcs = _target_names(tree)
    # A test that patches or assigns into the module (mock.patch, monkeypatch, setattr, m.X = ...)
    # can change what any of its calls returns between two assertions: no verdict from it.
    if _patches_module(tree, mods):
        return []
    _substitute_pure_vars(tree, mods, funcs, impure)
    asserts = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert"):
            asserts.append(n)
    known, problems, almost = {}, [], {}
    wrapped, truth = {}, {}
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
                wkey = _expr_key(call, mods, funcs, impure) if ok else None
                if wkey is not None and wkey[0] == "wrap":
                    # len(m.f(x)) required to be 3 and 4
                    if wkey in wrapped and wrapped[wkey] != val and type(wrapped[wkey]) is type(val):
                        problems.append("%s expected both %r and %r" % (ast.unparse(call)[:80], wrapped[wkey], val))
                    wrapped.setdefault(wkey, val)
                    break
        if n.func.attr in ("assertTrue", "assertFalse") and len(n.args) >= 1 and \
                _pure_call(n.args[0], mods, funcs, impure) and _call_key(n.args[0]) is not None:
            key, want = _call_key(n.args[0]), n.func.attr == "assertTrue"
            if key in truth and truth[key] != want:
                problems.append("%s required to be both true and false" % ast.unparse(n.args[0])[:80])
            truth.setdefault(key, want)
    for key, want in truth.items():
        if key in known and type(known[key]) in (bool, int, str, list, tuple, dict, set) and bool(known[key]) != want:
            problems.append("a call required to equal %r and to be %s" % (known[key], "true" if want else "false"))
    problems += _raise_contradictions(tree, asserts, known, mods, funcs, impure)
    # Different permutations are different inputs. A name such as maximum_profit,
    # total_distance or count_switches cannot prove that their results must match.
    problems += _self_contradictions(asserts, mods, funcs, impure)
    problems += _object_contradictions(tree, impure)
    if not known:
        return problems
    for n in asserts:
        attr = n.func.attr
        if attr in ("assertEqual", "assertEquals") and len(n.args) >= 2:
            a, b = n.args[0], n.args[1]
            if _has_call(a) and _has_call(b) and not _lit(a)[0] and not _lit(b)[0]:
                # Calls on both sides (average(xs) == total(xs) / len(xs)): both sides evaluated
                # with the values other assertions require of those calls.
                ok1, v1 = _eval_subst(a, known)
                ok2, v2 = _eval_subst(b, known)
                if ok1 and ok2:
                    try:
                        differ = abs(v1 - v2) > 1e-9 * max(1.0, abs(v1), abs(v2))
                    except TypeError:
                        differ = v1 != v2
                    if differ:
                        problems.append("%s contradicts required values" % ast.unparse(n)[:120])
                continue
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


def _raise_contradictions(tree, asserts, known, mods, funcs, impure):
    """A pure call required to return a value somewhere and to raise somewhere else
    (assertEqual(f([]), 0) with assertRaises(ValueError, f, []) or a `with assertRaises` block
    whose only statement is that call)."""
    raising = []
    for n in asserts:  # self.assertRaises(Exc, f, *args)
        if n.func.attr == "assertRaises" and len(n.args) >= 2:
            raising.append(ast.Call(func=n.args[1], args=list(n.args[2:]), keywords=list(n.keywords)))
    for w in ast.walk(tree):
        if not isinstance(w, (ast.With, ast.AsyncWith)) or len(w.body) != 1:
            continue
        if not any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and
                   i.context_expr.func.attr in ("assertRaises", "raises") for i in w.items):
            continue
        st = w.body[0]
        val = st.value if isinstance(st, (ast.Expr, ast.Assign)) else None
        if isinstance(val, ast.Call):
            raising.append(val)
    # Calls an assertion expects to return (any value: assertEqual(f(x), date(2023, 3, 1)),
    # assertTrue(f(x)), ...), outside `with assertRaises` blocks.
    inside = set()
    for w in ast.walk(tree):
        if isinstance(w, (ast.With, ast.AsyncWith)) and any(
                isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and
                i.context_expr.func.attr in ("assertRaises", "assertRaisesRegex", "raises", "assertWarns")
                for i in w.items):
            inside.update(id(x) for st in w.body for x in ast.walk(st))
    returning = {}
    for n in asserts:
        if id(n) in inside or n.func.attr.startswith(("assertRaises", "assertWarns")):
            continue
        for a in n.args:
            if _pure_call(a, mods, funcs, impure):
                k = _call_key(a)
                if k is not None:
                    returning.setdefault(k, a)
    out = []
    for call in raising:
        if any(k.arg for k in getattr(call, "keywords", [])):
            continue  # assertRaises(Exc, f, x, msg=...) and the like: leave it
        if not _pure_call(call, mods, funcs, impure):
            continue
        key = _call_key(call)
        if key is not None and key in known:
            out.append("%s must both return %r and raise" % (ast.unparse(call)[:80], known[key]))
        elif key is not None and key in returning:
            out.append("%s must both return a value and raise" % ast.unparse(call)[:80])
    return out


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


def _object_contradictions(tree, impure):
    """In one test method: obj.m(<literals>) required to equal two different literals with
    nothing touching obj in between, where m does not change its object (per the source)."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seen = {}
        for st in fn.body:
            call = lit = None
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and \
                    getattr(st.value.func, "attr", "") in ("assertEqual", "assertEquals") and len(st.value.args) >= 2:
                a0, a1 = st.value.args[0], st.value.args[1]
                for c, l in ((a0, a1), (a1, a0)):
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) \
                            and _lit(l)[0] and _call_key(c) is not None and c.func.attr not in impure:
                        call, lit = c, _lit(l)[1]
                        break
            if call is not None:
                key = (call.func.value.id, _call_key(call))
                if key in seen and seen[key] != lit:
                    out.append("%s expected both %r and %r on an unchanged object" % (ast.unparse(call)[:80], seen[key], lit))
                seen.setdefault(key, lit)
                continue
            # any other statement mentioning an object forgets what we knew about it
            names = {x.id for x in ast.walk(st) if isinstance(x, ast.Name)}
            seen = {k: v for k, v in seen.items() if k[0] not in names}
    return out


def _uses_impure(node, impure):
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            f = x.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in impure:
                return True
            if not (x.args or x.keywords) and isinstance(f, ast.Name) and f.id[:1].isupper():
                return True  # a bare constructor: two instances may differ
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

"""Offline retrieval check on a labelled knowledge-base set (no model calls).

python3 -I bench/tools/retrieval_r01.py bench/sets/r01

For every solvable task with supporting quotes in keys.json, each method builds the excerpt the
model would see and the script counts the quotes that reach it (whitespace-insensitive):
the harness's first look (solver.prepare), its second look without a hint (the whole base when
small, else BM25 paragraphs up to 12k), and alternatives over the same paragraphs: character
3-5-gram TF-IDF ranking, and BM25 by the question followed by character n-grams over the names
the first paragraphs mention."""
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from harness import context as ctx  # noqa: E402
from harness import solver as S  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))


def norm(t):
    return re.sub(r"\s+", " ", t).strip().lower()


def ngrams(t, ns=(3, 4, 5)):
    t = " %s " % re.sub(r"\s+", " ", t.lower())
    return [t[i:i + n] for n in ns for i in range(len(t) - n + 1)]


def tfidf_order(docs_tok, q_tok):
    import math
    n = len(docs_tok)
    df = collections.Counter()
    for d in docs_tok:
        df.update(set(d))
    idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def vec(toks):
        tf = collections.Counter(toks)
        v = {w: (1 + math.log(c)) * idf.get(w, 0) for w, c in tf.items()}
        z = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / z for w, x in v.items()}
    qv = vec(q_tok)
    sc = []
    for d in docs_tok:
        dv = vec(d)
        sc.append(sum(x * dv.get(w, 0) for w, x in qv.items()))
    return sorted(range(n), key=lambda i: -sc[i])


def fill(order, paras, limit):
    out, size = [], 0
    for i in order:
        if size >= limit:
            break
        out.append(i)
        size += len(paras[i][1])
    return "\n".join("[%s] %s" % paras[i] for i in out)[:limit]


def main():
    root = sys.argv[1]
    keys = json.load(open(os.path.join(root, "keys.json")))
    sv = S.Solver(lambda *a, **k: "")
    res = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0, 0, 0]))
    for tid, k in sorted(keys.items()):
        if not k.get("feasible") or not k.get("support"):
            continue
        d = os.path.join(root, "tasks", tid)
        t = ctx.Task(d, json.load(open(os.path.join(d, "task.json"))))
        info = sv.prepare(t)
        q = t.short_instruction()
        docs = ctx.kb_docs(t)
        paras = ctx._passages(docs)
        excerpts = {
            "harness first look": info.get("ctx") or "",
            "harness second look (no hint)": ctx.kb_fallback_text(t),
        }
        qw = [w for w in ctx._norm(q).split() if w not in ctx._STOP and len(w) > 2]
        bm, _ = ctx._bm25_rank(paras, qw)
        ng = tfidf_order([ngrams(a + " " + b) for a, b in paras], ngrams(q))
        excerpts["char 3-5 n-grams, one round, 12k"] = fill(ng, paras, 12000)
        # BM25 round 1, then n-grams over the names of the first paragraphs
        first = bm[:6]
        names = " ".join(n for i in first for n in re.findall(r"\b[A-Z][\w\-]+(?:\s+[A-Z][\w\-]+)*\b", paras[i][1]))
        ng2 = tfidf_order([ngrams(a + " " + b) for a, b in paras], ngrams(q + " " + names))
        excerpts["BM25 then n-grams on names, 12k"] = fill(first + [i for i in ng2 if i not in first], paras, 12000)
        hops = k.get("hops", 1)
        for name, text in excerpts.items():
            nt = norm(text)
            found = [norm(s["quote"]) in nt for s in k["support"]]
            r = res[name][hops]
            r[0] += sum(found)
            r[1] += len(found)
            r[2] += all(found)
            r[3] += 1
    hop_list = sorted({h for by in res.values() for h in by})
    print("%-34s %s" % ("method", "  ".join("%d-hop recall / all" % h for h in hop_list)))
    for name, by in res.items():
        print("%-34s %s" % (name, "   ".join("%5.1f%% / %5.1f%%" % (100 * by[h][0] / max(1, by[h][1]),
                                                                    100 * by[h][2] / max(1, by[h][3]))
                                             for h in hop_list)))


main()

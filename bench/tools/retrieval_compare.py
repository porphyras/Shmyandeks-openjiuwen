"""Offline comparison of paragraph retrieval for large prose bases (no model calls).

python3 -I bench/tools/retrieval_compare.py MUSIQUE_DEV.jsonl [--n 300] [--pool 200] [--limit 9000]

Each MuSiQue question (2-4 hops, answerable) gets a base of its own 20 paragraphs plus distractor
paragraphs of other questions (up to --pool paragraphs). A method ranks the paragraphs for the
question and fills the excerpt up to --limit characters, like the harness's first look. The score
is the share of the question's supporting paragraphs that reach the excerpt (recall), and the
share of questions whose supporting paragraphs all reach it."""
import argparse
import collections
import json
import math
import os
import random
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from harness import context as ctx  # noqa: E402

WORD = re.compile(r"[a-z0-9]+")


def words(t):
    return [w for w in WORD.findall(t.lower()) if w not in ctx._STOP and len(w) > 2]


def char_ngrams(t, ns):
    t = " %s " % re.sub(r"\s+", " ", t.lower())
    out = []
    for n in ns:
        out += [t[i:i + n] for i in range(len(t) - n + 1)]
    return out


def tfidf_rank(docs_tokens, q_tokens):
    n = len(docs_tokens)
    df = collections.Counter()
    for toks in docs_tokens:
        df.update(set(toks))
    idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def vec(toks):
        tf = collections.Counter(toks)
        v = {w: (1 + math.log(c)) * idf.get(w, 0.0) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    qv = vec(q_tokens)
    scores = []
    for toks in docs_tokens:
        dv = vec(toks)
        scores.append(sum(x * dv.get(w, 0.0) for w, x in qv.items()))
    return sorted(range(n), key=lambda i: -scores[i])


def rrf(*orders, k=60):
    score = collections.defaultdict(float)
    for order in orders:
        for r, i in enumerate(order):
            score[i] += 1.0 / (k + r + 1)
    return sorted(score, key=lambda i: -score[i])


def fill(order, paras, limit):
    chosen, size = set(), 0
    for i in order:
        if size + len(paras[i][1]) > limit and chosen:
            break
        chosen.add(i)
        size += len(paras[i][1])
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--pool", type=int, default=200)
    ap.add_argument("--limit", type=int, default=9000)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.src, encoding="utf-8")]
    rows = [r for r in rows if r.get("answerable", True) and int(r["id"][0]) in (2, 3, 4)]
    rnd = random.Random(7)
    rnd.shuffle(rows)
    items, others = rows[:a.n], rows[a.n:]
    methods = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0, 0, 0]))
    for r in items:
        hops = int(r["id"][0])
        own = [(p["title"], p["paragraph_text"], p["is_supporting"]) for p in r["paragraphs"]]
        extra = []
        while len(own) + len(extra) < a.pool:
            o = rnd.choice(others)
            extra += [(p["title"], p["paragraph_text"], False) for p in o["paragraphs"]]
        paras = own + extra[:a.pool - len(own)]
        rnd.shuffle(paras)
        support = {i for i, p in enumerate(paras) if p[2]}
        q = r["question"]
        docs = [(t, x) for t, x, _ in paras]
        bm25_order, _ = ctx._bm25_rank(docs, [w for w in ctx._norm(q).split() if w not in ctx._STOP and len(w) > 2])
        wtok = [words(t + " " + x) for t, x in docs]
        tfidf_order = tfidf_rank(wtok, words(q))
        orders = {"BM25 (question only)": bm25_order, "TF-IDF words": tfidf_order}
        for ns in ((3,), (4,), (5,), (3, 4, 5)):
            orders["char n-grams %s" % "-".join(map(str, ns))] = tfidf_rank(
                [char_ngrams(t + " " + x, ns) for t, x in docs], char_ngrams(q, ns))
        orders["RRF BM25 + TF-IDF"] = rrf(bm25_order, tfidf_order)
        orders["RRF BM25 + char 3-5"] = rrf(bm25_order, orders["char n-grams 3-4-5"])
        # the harness's own first look: BM25 by the question, then by the names the first-round
        # paragraphs mention (bridge entities)
        text = ctx.prose_passages([("p%d.md" % i, "# %s\n\n%s" % (t, x)) for i, (t, x) in enumerate(docs)], q, a.limit)
        harness_sel = {i for i, (t, x) in enumerate(docs) if x[:80] in text}
        for name, order in list(orders.items()) + [("harness (BM25, two rounds)", None)]:
            sel = harness_sel if order is None else fill(order, docs, a.limit)
            s = methods[name][hops]
            s[0] += len(support & sel)
            s[1] += len(support)
            s[2] += support <= sel
            s[3] += 1
    print("pool %d paragraphs, excerpt limit %d characters, %d questions" % (a.pool, a.limit, len(items)))
    print("%-28s %s" % ("method", "  ".join("%d-hop recall / all found" % h for h in (2, 3, 4))))
    for name, by in methods.items():
        cells = []
        for h in (2, 3, 4):
            s = by[h]
            cells.append("%5.1f %% / %5.1f %%" % (100 * s[0] / max(1, s[1]), 100 * s[2] / max(1, s[3])))
        print("%-28s %s" % (name, "      ".join(cells)))


main()

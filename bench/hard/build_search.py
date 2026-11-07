"""Search tasks from MuSiQue (full v1.0 dev: answerable and unanswerable twins).

Each of the 20 paragraphs of an item becomes kb/<title-slug>.md ("# Title" + paragraph text).
Feasible: answerable 2-4 hop questions whose answer is a year or a short proper name with no
aliases, appearing verbatim in a supporting paragraph. "When ..." questions with a year answer are
reworded to "In what year ...". Infeasible: MuSiQue unanswerable twins (a supporting paragraph is
replaced) where neither the answer nor any alias occurs anywhere in the knowledge base.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hashlib  # noqa: E402

from common import SEARCH_HEAD, SEARCH_TAIL, slug  # noqa: E402


def stable_key(item_id):
    return hashlib.sha256(("search|" + item_id).encode()).hexdigest()

NAME_RE = re.compile(r"[A-Z][A-Za-z'\-\.]*(?: (?:[A-Z][A-Za-z'\-\.]*|de|of|von|van|da|del|la|le|and|on|upon))*")
QSTART = re.compile(r"^(Who|What|Where|When|Which|In which|In what)\b")
STOP_ANSWERS = {"United States", "America", "US", "USA", "UK", "England", "Europe", "January", "February",
                "March", "April", "May", "June", "July", "August", "September", "October", "November",
                "December", "North", "South", "East", "West", "Democratic", "Republican"}

# MuSiQue ids excluded after manual review of the picked questions (garbled or ambiguous wording).
EXCLUDE = {
    "2hop__754826_7292",      # paragraph lists several artists
    "2hop__66843_494136",     # actor had several spouses
    "2hop__144763_599630",    # "Yongjia" vs "Yongjia County"
    "2hop__142443_768138",    # the artist has two brothers
    "3hop2__43283_12039_93690",  # garbled question
    "3hop1__53690_161697_49779",  # several hosts share the nationality
    "3hop2__76623_90098_10557",  # garbled question
    "3hop1__377943_650651_7262",  # "a prominent figure": several names
    "4hop1__370129_621192_10659_59955",  # garbled, answer is not a country
    "3hop2__565959_467331_162182",  # near-duplicate of a feasible item
    "4hop1__638706_378185_282674_759393",  # near-duplicate of a feasible item
    "2hop__58939_213732",     # several children
    "2hop__435087_55984",     # ambiguous song reference
    "2hop__75207_365216",     # "Talca" vs "Talca Province"
    "2hop__661847_288421",    # duo: two members
    "2hop__701895_752697",    # garbled question
    "3hop1__139773_88110_21193",  # CAB has several compression methods
    "2hop__34734_92852",      # vague reference ("the country that started hostilities")
    "3hop1__491648_339990_54675",  # several mountains are visible
    "4hop2__71753_685934_70784_61381",  # garbled question
    "4hop2__161602_426860_88460_126088",  # garbled, time-dependent
    "4hop1__88342_75218_128008_67954",  # garbled question
    "2hop__575282_114648",    # garbled question
    "2hop__131951_643670",    # garbled question
    "3hop1__365934_87694_124169",  # garbled question ("What date ... end?")
    "4hop3__160230_470287_345497_72134",  # garbled question
    "4hop1__88342_49853_128008_48717",  # garbled question
    "2hop__135651_669373",    # several employers / name variants
    "3hop1__17192_78396_54865",  # 1776 vs 1783 is ambiguous
    "4hop2__71753_261673_70784_79935",  # garbled question
    "4hop1__88342_49853_128008_46894",  # garbled question
    "4hop1__88342_75218_128008_48717",  # garbled question
    "3hop1__764280_834494_34053",  # garbled question
    "4hop1__151650_5274_458768_33677",  # garbled question
    "4hop1__3496_5274_458768_33677",  # garbled question
    "3hop1__5544_34179_23375",  # garbled question
    "3hop1__857_846_7872",    # the city has several football clubs
}
# answers whose MuSiQue question families were all garbled or ambiguous in review
BAD_ANSWERS = {"Medieval Latin", "Tualatin Mountains", "Thein Sein", "1932", "1930", "1952", "1948", "Alex Bregman", "Bill Bergen"}
# relations that usually have several valid answers
AMBIGUOUS = re.compile(r"(?i)\b(shares? a border|border with|borders?\b|is part of|child|children|son|daughter|"
                       r"brother|sister|sibling|spouse|wife|husband|married|member|one of|a host|a prominent)\b")


def answer_kind(a):
    if re.fullmatch(r"1\d{3}|20\d{2}", a):
        return "year"
    if (NAME_RE.fullmatch(a) and not a.startswith("The ") and len(a.split()) <= 3 and len(a) <= 28 and a not in STOP_ANSWERS
            and not re.search(r"\d", a)):
        return "name"
    return None


def reword(q, kind):
    if kind == "year":
        q2 = re.sub(r"^When (was|were|did|does|do|is|are|had|has)\b", r"In what year \1", q)
        if q2.startswith("When "):
            return None
        return q2
    if q.startswith("When "):
        return None
    return q


def kb_files(paragraphs):
    files, seen = {}, {}
    for p in paragraphs:
        base = slug(p["title"]) or "doc"
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else "%s_%d" % (base, seen[base])
        text = "# %s\n\n%s\n" % (p["title"].strip(), p["paragraph_text"].strip())
        files["kb/%s.md" % name] = text.encode("utf-8")
    return files


def instruction(q):
    return "%s %s %s" % (SEARCH_HEAD, q, SEARCH_TAIL)


def candidates(rows):
    out = []
    for r in rows:
        if not r["answerable"] or r["answer_aliases"] or r["id"] in EXCLUDE:
            continue
        a = r["answer"].strip()
        if a in BAD_ANSWERS:
            continue
        kind = answer_kind(a)
        q = " ".join(r["question"].split())
        if kind is None or not QSTART.match(q) or len(q) > 190 or a.lower() in q.lower():
            continue
        if AMBIGUOUS.search(q):
            continue
        q = reword(q, kind)
        if q is None or not q.endswith("?"):
            continue
        sup = [p for p in r["paragraphs"] if p["is_supporting"]]
        last = r["question_decomposition"][-1].get("paragraph_support_idx")
        last_p = next((p for p in r["paragraphs"] if p["idx"] == last), None)
        if last_p is None or a not in last_p["paragraph_text"]:
            continue
        out.append({"row": r, "question": q, "answer": a, "akind": kind,
                    "hops": len(r["question_decomposition"]), "nsup": len(sup)})
    return out


def make(dl, n_feasible=25, n_inf=4):
    rows = [json.loads(l) for l in open(os.path.join(dl, "musique", "musique_full_v1.0_dev.jsonl"),
                                        encoding="utf-8")]
    cands = candidates(rows)
    by_hops = {2: [], 3: [], 4: []}
    for c in sorted(cands, key=lambda c: stable_key(c["row"]["id"])):
        by_hops[c["hops"]].append(c)
    want = {2: 7, 3: 10, 4: n_feasible - 17}
    picked, answers, hop_ids = [], set(), set()

    def fresh(c):
        ids = set(c["row"]["id"].split("__")[1].split("_"))
        return c["answer"] not in answers and not (ids & hop_ids)

    def take(c):
        picked.append(c)
        answers.add(c["answer"])
        hop_ids.update(c["row"]["id"].split("__")[1].split("_"))

    for h, n in want.items():
        years = [c for c in by_hops[h] if c["akind"] == "year"]
        names = [c for c in by_hops[h] if c["akind"] == "name"]
        ny = 0
        for c in years:
            if ny < n // 3 and fresh(c):
                take(c)
                ny += 1
        nn = 0
        for c in names:
            if nn < n - ny and fresh(c):
                take(c)
                nn += 1
    for h in (3, 2, 4):  # fill any shortfall (the 4-hop pool is mostly garbled questions)
        for c in by_hops[h]:
            if len(picked) < n_feasible and c not in picked and fresh(c):
                take(c)
    used_ids = {c["row"]["id"] for c in picked}
    tasks = []
    for c in picked:
        r = c["row"]
        tasks.append({"type": "search", "kind": "musique_%dhop" % c["hops"], "feasible": True,
                      "instruction": instruction(c["question"]), "check": "exact_str",
                      "expected": c["answer"], "files": kb_files(r["paragraphs"]),
                      "meta": {"source": "musique-full-dev", "id": r["id"], "answer_kind": c["akind"],
                               "original_question": r["question"],
                               "support_titles": [p["title"] for p in r["paragraphs"] if p["is_supporting"]]}})
    # infeasible: unanswerable twins whose answer (and aliases) are absent from every paragraph
    inf = []
    unans = [r for r in rows if not r["answerable"] and r["id"] not in used_ids and r["id"] not in EXCLUDE
             and r["answer"].strip() not in answers and r["answer"].strip() not in BAD_ANSWERS
             and not set(r["id"].split("__")[1].split("_")) & hop_ids]
    unans.sort(key=lambda r: stable_key(r["id"]))
    for r in unans:
        if len(inf) >= n_inf:
            break
        hops = len(r["question_decomposition"])
        if hops < 3 and len(inf) < n_inf - 1:
            continue
        a = r["answer"].strip()
        q = " ".join(r["question"].split())
        kind = answer_kind(a)
        if kind is None or not QSTART.match(q) or len(q) > 190:
            continue
        q = reword(q, kind)
        if q is None:
            continue
        text = "\n".join(p["title"] + "\n" + p["paragraph_text"] for p in r["paragraphs"]).lower()
        if any(s.lower() in text for s in [a] + list(r["answer_aliases"])):
            continue
        inf.append({"type": "search", "kind": "musique_unanswerable", "feasible": False,
                    "instruction": instruction(q), "check": "exact_str", "expected": None,
                    "files": kb_files(r["paragraphs"]),
                    "meta": {"source": "musique-full-dev", "id": r["id"], "hidden_answer": a,
                             "original_question": r["question"]}})
    print("search: MuSiQue %d items, %d answerable candidates after filters (2/3/4 hops: %d/%d/%d); "
          "picked %d feasible, %d infeasible" % (len(rows), len(cands), len(by_hops[2]), len(by_hops[3]),
                                                 len(by_hops[4]), len(tasks), len(inf)))
    return tasks + inf


if __name__ == "__main__":
    for t in make(sys.argv[1]):
        q = t["instruction"][len(SEARCH_HEAD) + 1:-len(SEARCH_TAIL) - 1]
        print(t["kind"], t["meta"]["id"], "|", q, "=>", t["expected"] or t["meta"].get("hidden_answer"))

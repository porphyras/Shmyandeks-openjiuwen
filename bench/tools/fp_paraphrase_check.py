"""Regression: paraphrased knowledge-base questions must never get a free "infeasible" verdict.

python3 -I bench/tools/fp_paraphrase_check.py      (from the repo root, harness venv python)

For every solvable card-base task of the bench sets that the fast path answers, the question is
rewritten in many other wordings; for each, the fast path may answer (then the answer must be the
same) or hand the task to the model (None), but must never declare it infeasible."""
import glob
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from harness import context as ctx  # noqa: E402
from harness import fastpath as F  # noqa: E402

PRE = F._SEARCH_PRE
N = r"([A-Z][\w'\-]+(?: [A-Z][\w'\-]+)+)"
SOURCES = {  # recognised question -> its paraphrases (X = the name; Y = another entity in chains)
    r"In which year was %s born\?" % N: [
        "What is the birth year of X?", "When was X born?", "In what year was X born?", "What year was X born in?",
        "What is X's year of birth?", "What is the year of birth of X?", "Born in which year is X?",
        "Which year was X born?", "X was born in which year?"],
    r"Which company does %s work for\?" % N: [
        "Which company employs X?", "Who employs X?", "What company does X work for?", "For which company does X work?",
        "Where does X work?", "What is X's employer?", "What is the employer of X?", "X works for which company?",
        "At which company is X employed?"],
    r"In which city is the headquarters of %s's employer\?" % N: [
        "Where is the headquarters of X's employer?", "In which city is X's employer based?",
        "Where is X's employer headquartered?", "In what city is the company X works for headquartered?",
        "Where is the company that employs X located?"],
    r"In which year was %s's employer founded\?" % N: [
        "When was X's employer founded?", "In what year was X's employer established?",
        "What is the founding year of X's employer?", "In which year was the company X works for founded?"],
    r"In which year was %s's mentor born\?" % N: [
        "When was X's mentor born?", "What is the birth year of X's mentor?", "In what year was X's advisor born?",
        "What year was the mentor of X born?"],
    r"Which company does %s's mentor work for\?" % N: [
        "Who employs X's mentor?", "Where does X's mentor work?", "What company employs the mentor of X?",
        "For which company does X's supervisor work?"],
}


def main():
    bad, total, answered, model = [], 0, 0, 0
    for kp in sorted(glob.glob("bench/sets/*/keys.json")):
        st = kp.split("/")[2]
        keys = json.load(open(kp))
        for tid, k in keys.items():
            if k["type"] != "search" or not k["feasible"]:
                continue
            d = "bench/sets/%s/tasks/%s" % (st, tid)
            if not os.path.isdir(d):
                continue
            data = json.load(open(d + "/task.json"))
            t = ctx.Task(d, data)
            sents = F._sentences(t.short_instruction())
            if len(sents) != 2 or sents[0] != PRE:
                continue
            for rx, paras in SOURCES.items():
                m = re.fullmatch(rx, sents[1])
                if not m:
                    continue
                base = F.solve(t, "search")
                if not base or base[0] != "solved":
                    continue
                for para in paras:
                    q = para.replace("X", m.group(1))
                    tmp = tempfile.mkdtemp()
                    shutil.copytree(d, os.path.join(tmp, tid))
                    d2 = os.path.join(tmp, tid)
                    data2 = dict(data, instruction=data["instruction"].replace(sents[1], q))
                    json.dump(data2, open(d2 + "/task.json", "w"))
                    out = F.solve(ctx.Task(d2, data2), "search")
                    shutil.rmtree(tmp)
                    total += 1
                    if out is None:
                        model += 1
                    elif out[0] == "infeasible" or out[1] != base[1]:
                        bad.append((st, tid, q, out, base[1]))
                    else:
                        answered += 1
                break
    print("paraphrases %d: answered %d, to the model %d, BAD %d" % (total, answered, model, len(bad)))
    for b in bad[:20]:
        print("  ", b)
    sys.exit(1 if bad else 0)


main()

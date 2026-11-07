"""Make a sub-set of a bench set: python3 -I bench/tools/subset.py SRC OUT [--types a,b] [--n N]"""
import argparse, json, os, shutil

p = argparse.ArgumentParser()
p.add_argument("src"); p.add_argument("out")
p.add_argument("--types", default=""); p.add_argument("--n", type=int, default=0)
a = p.parse_args()
keys = json.load(open(os.path.join(a.src, "keys.json")))
types = set(t for t in a.types.split(",") if t)
sel = {k: v for k, v in sorted(keys.items()) if not types or v["type"] in types}
if a.n:
    sel = dict(list(sel.items())[:a.n])
shutil.rmtree(a.out, ignore_errors=True)
os.makedirs(os.path.join(a.out, "tasks"))
for tid in sel:
    shutil.copytree(os.path.join(a.src, "tasks", tid), os.path.join(a.out, "tasks", tid))
    h = os.path.join(a.src, "hidden", tid)
    if os.path.isdir(h):
        shutil.copytree(h, os.path.join(a.out, "hidden", tid))
json.dump(sel, open(os.path.join(a.out, "keys.json"), "w"), indent=1)
om = os.path.join(a.src, "oracle_mock.json")
if os.path.exists(om):
    shutil.copy(om, a.out)
print(len(sel), "tasks")

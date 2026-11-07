"""Convert the partner's task set (task dirs + keys.json at the root) into our bench layout:
<out>/tasks/<id>/ (task.json without answer fields), <out>/keys.json, <out>/hidden/<id>/test_hidden.py."""
import json, os, shutil, sys

src, out = sys.argv[1], sys.argv[2]
keys = json.load(open(os.path.join(src, "keys.json")))
shutil.rmtree(out, ignore_errors=True)
os.makedirs(os.path.join(out, "tasks"))
for tid, k in keys.items():
    d = os.path.join(src, tid)
    dst = os.path.join(out, "tasks", tid)
    shutil.copytree(d, dst, symlinks=False)
    tj = os.path.join(dst, "task.json")
    data = json.load(open(tj))
    for f in ("expected", "check", "infeasible_reason"):
        data.pop(f, None)
    json.dump(data, open(tj, "w"), indent=2)
    if k.get("check") == "unittest":
        h = os.path.join(out, "hidden", tid)
        os.makedirs(h)
        shutil.copy(os.path.join(d, "workspace", "test_mylib.py"), os.path.join(h, "test_hidden.py"))
    k.setdefault("kind", k.get("infeasible_reason") or "alt")
json.dump(keys, open(os.path.join(out, "keys.json"), "w"), indent=1)
print(len(keys), "tasks imported")

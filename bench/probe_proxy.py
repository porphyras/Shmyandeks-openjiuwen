"""Probe the LLM proxy: model list, latency, usage fields. Logs usage (never the key)."""
import json, os, sys, time, urllib.request

BASE = os.environ["LLM_BASE_URL"].rstrip("/")
KEY = os.environ["LLM_API_KEY"]
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_usage.jsonl")


def req(path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if body else "GET",
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500], time.time() - t


def chat(model, prompt, max_tokens, tag, **extra):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0}
    body.update(extra)
    st, js, dt = req("/chat/completions", body)
    rec = {"ts": time.time(), "tag": tag, "model": model, "status": st, "latency": round(dt, 2)}
    if isinstance(js, dict):
        rec["usage"] = js.get("usage")
        msg = js["choices"][0]["message"]
        rec["content"] = (msg.get("content") or "")[:300]
        rec["reasoning_len"] = len(msg.get("reasoning_content") or msg.get("reasoning") or "")
        rec["msg_keys"] = sorted(msg.keys())
        rec["finish"] = js["choices"][0].get("finish_reason")
    else:
        rec["error"] = js
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))
    return rec


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "models"
    if what == "models":
        st, js, dt = req("/models")
        print(st, round(dt, 2), json.dumps(js)[:1500])
    elif what == "chat":
        for m in sys.argv[2:]:
            mt = 60 if m.endswith("noreason") else 400
            chat(m, "What is 17*23? Answer with just the number.", mt, "probe1")

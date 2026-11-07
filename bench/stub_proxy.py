"""Developer tool, not part of the image: local stand-in for the organizers' LLM proxy.
In the Docker replica only this stub (never the harness) is attached to the bridge network.

Counts tokens like the organizers (input/10 + output + reasoning, no cache discount)
and stops answering once the budget is spent.

Modes:
  forward  - relay to the real proxy (UPSTREAM_BASE_URL / UPSTREAM_API_KEY from env),
             count tokens from upstream `usage`.
  mock     - no LLM at all: answer from an oracle file (bench-only, for testing the
             harness mechanics for free) or with a fixed reply.

Usage:
  python stub_proxy.py --port 8000 --mode mock --oracle oracle.json --budget 100000 --stats stats.json
"""
import argparse
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
STATE = {"spent": 0.0, "requests": 0, "refused": 0, "in_tokens": 0, "out_tokens": 0,
         "reasoning_tokens": 0, "by_model": {}, "log": [], "exhausted_at": None, "started": time.time()}
ARGS = None
ORACLE = []


def approx_tokens(text):
    return max(1, len(text) // 4)


def save_stats():
    if not ARGS.stats:
        return
    tmp = ARGS.stats + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ARGS.stats)


def charge(model, pin, pout, preason, extra):
    cost = pin / 10.0 + pout + preason
    with LOCK:
        STATE["spent"] += cost
        STATE["requests"] += 1
        STATE["in_tokens"] += pin
        STATE["out_tokens"] += pout
        STATE["reasoning_tokens"] += preason
        m = STATE["by_model"].setdefault(model, {"n": 0, "cost": 0.0})
        m["n"] += 1
        m["cost"] += cost
        rec = {"t": round(time.time() - STATE["started"], 2), "model": model, "in": pin, "out": pout,
               "reason": preason, "cost": round(cost, 1), "spent": round(STATE["spent"], 1)}
        rec.update(extra)
        STATE["log"].append(rec)
        if ARGS.budget and STATE["spent"] >= ARGS.budget and STATE["exhausted_at"] is None:
            STATE["exhausted_at"] = rec["t"]
        save_stats()


def budget_left():
    return not ARGS.budget or STATE["spent"] < ARGS.budget


def mock_reply(body):
    msgs = body.get("messages", [])
    text = "\n".join(str(m.get("content", "")) for m in msgs)
    reply = ARGS.mock_default
    for key, ans in ORACLE:
        if key in text:
            reply = ans
            break
    if ARGS.mock_garbage and random.random() < ARGS.mock_garbage:
        reply = random.choice(["", "I cannot do that.", "```python\nraise SystemExit(1)\n```",
                               "lorem ipsum dolor sit amet"])
    pin = approx_tokens(text) + 8 * len(msgs)
    pout = approx_tokens(reply)
    return reply, pin, pout


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            ids = ["qwen3.8-27b", "qwen3.8-27b-noreason", "qwen3.6-35b-a3b", "qwen3.6-35b-a3b-noreason"]
            return self._send(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})
        if self.path.startswith("/stats"):
            with LOCK:
                return self._send(200, {k: v for k, v in STATE.items() if k != "log"})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        if not budget_left():
            with LOCK:
                STATE["refused"] += 1
                save_stats()
            if ARGS.exhaust == "drop":
                self.close_connection = True
                return
            return self._send(429, {"error": {"message": "budget exhausted", "type": "insufficient_quota"}})
        try:
            body = json.loads(raw)
        except Exception:
            return self._send(400, {"error": {"message": "bad json"}})
        model = body.get("model", "?")
        if ARGS.latency:
            time.sleep(ARGS.latency * random.uniform(0.7, 1.3))
        if ARGS.mode == "mock":
            reply, pin, pout = mock_reply(body)
            charge(model, pin, pout, 0, {})
            return self._send(200, {
                "id": "mock", "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout}})
        # forward
        req = urllib.request.Request(
            os.environ["UPSTREAM_BASE_URL"].rstrip("/") + "/chat/completions", data=raw, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + os.environ["UPSTREAM_API_KEY"]})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=ARGS.upstream_timeout) as r:
                js = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": {"message": "upstream http %d" % e.code}})
        except Exception as e:
            return self._send(502, {"error": {"message": "upstream error: %s" % type(e).__name__}})
        u = js.get("usage") or {}
        pin = int(u.get("prompt_tokens") or 0)
        pout = int(u.get("completion_tokens") or 0)
        det = u.get("completion_tokens_details") or {}
        rt = int(det.get("reasoning_tokens") or 0)
        msg = (js.get("choices") or [{}])[0].get("message") or {}
        rtext = msg.get("reasoning_content") or msg.get("reasoning") or ""
        # Reasoning counts as output whatever field it is in. If completion_tokens already
        # includes it (OpenAI convention: reasoning_tokens is a breakdown), don't double count;
        # if reasoning text exists but is not reflected anywhere, estimate it.
        extra_reason = 0
        if rtext and not rt:
            est = approx_tokens(rtext)
            content_est = approx_tokens(msg.get("content") or "")
            if pout < est + content_est * 0.5:
                extra_reason = est
        charge(model, pin, pout, extra_reason,
               {"lat": round(time.time() - t0, 2), "rt_field": rt, "rtext_chars": len(rtext)})
        return self._send(200, js)


def main():
    global ARGS, ORACLE
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--mode", choices=["mock", "forward"], default="mock")
    p.add_argument("--budget", type=float, default=0, help="0 = unlimited")
    p.add_argument("--exhaust", choices=["429", "drop"], default="429")
    p.add_argument("--latency", type=float, default=0.0)
    p.add_argument("--oracle", default="", help="json list of [substring, reply]")
    p.add_argument("--mock-default", default="ANSWER: 0")
    p.add_argument("--mock-garbage", type=float, default=0.0, help="probability of a garbage reply")
    p.add_argument("--stats", default="")
    p.add_argument("--upstream-timeout", type=float, default=180)
    ARGS = p.parse_args()
    if ARGS.oracle:
        with open(ARGS.oracle) as f:
            ORACLE = [tuple(x) for x in json.load(f)]
    save_stats()
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), H)
    srv.daemon_threads = True
    print("stub proxy on %s:%d mode=%s budget=%s" % (ARGS.host, ARGS.port, ARGS.mode, ARGS.budget), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

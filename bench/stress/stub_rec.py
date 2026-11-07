"""bench/stub_proxy.py with request recording (same CLI plus --rec FILE).

Every chat request appends to the jsonl file:
  {"k": "req", "t0", "t1", "inflight"}        - wall times and concurrency at request start
  {"k": "prompt", "t", "model", "chars", "text"} - full prompt text of answered requests
Refused requests (budget spent) have a "req" line but no "prompt" line.
"""
import json
import os
import sys
import threading
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import stub_proxy as sp  # noqa: E402

REC = None
_argv = sys.argv[1:]
if "--rec" in _argv:
    i = _argv.index("--rec")
    REC = _argv[i + 1]
    del _argv[i:i + 2]
sys.argv = [sys.argv[0]] + _argv

LOCK = threading.Lock()
INFLIGHT = [0]


def _write(rec):
    if not REC:
        return
    line = json.dumps(rec, ensure_ascii=True)
    with LOCK:
        with open(REC, "a") as f:
            f.write(line + "\n")


_orig_mock = sp.mock_reply


def mock_reply(body):
    msgs = body.get("messages", [])
    text = "\n".join(str(m.get("content", "")) for m in msgs)
    _write({"k": "prompt", "t": time.time(), "model": body.get("model"), "chars": len(text), "text": text})
    return _orig_mock(body)


_orig_post = sp.H.do_POST


def do_post(self):
    with LOCK:
        INFLIGHT[0] += 1
        n = INFLIGHT[0]
    t0 = time.time()
    try:
        return _orig_post(self)
    finally:
        with LOCK:
            INFLIGHT[0] -= 1
        _write({"k": "req", "t0": t0, "t1": time.time(), "inflight": n})


sp.mock_reply = mock_reply
sp.H.do_POST = do_post

if __name__ == "__main__":
    sp.main()

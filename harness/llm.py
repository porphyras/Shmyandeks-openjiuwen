"""LLM access through openJiuwen (`init_model` + `Model.invoke`) with exact budget accounting.

Accounting follows the organizers: cost = prompt_tokens / 10 + completion_tokens, where the
completion already contains reasoning tokens (verified on the proxy: reasoning is included in
completion_tokens). If a provider reports reasoning separately and it is larger than the
completion, the difference is added.
"""
import asyncio
import os
import threading
import time


def configure_openjiuwen_logging():
    """openJiuwen creates ./logs/... on import by default; the container root is read-only.
    Must run before any other openjiuwen import."""
    os.environ.setdefault("OPENJIUWEN_TOKENIZER_CACHE_DIR", "/tmp/oj_tok")
    from openjiuwen.core.common.logging.log_config import configure_log_config
    configure_log_config({"level": "ERROR", "output": [], "interface_output": [], "performance_output": [],
                          "propagate": False, "log_path": "/tmp/ojlogs/"})


class BudgetExhausted(Exception):
    pass


class CallFailed(Exception):
    pass


class LLM:
    def __init__(self, base_url, api_key, budget=None, log=None, loop=None):
        from openjiuwen.core.foundation.llm.model import init_model
        self._init_model = init_model
        self.base_url = base_url
        self.api_key = api_key
        self.budget = budget
        self.log = log or (lambda rec: None)
        if loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="llm-loop", daemon=True).start()
        self.loop = loop
        self.models = {}
        self.rate_wait = 0.0
        self.lock = threading.Lock()
        self.spent = 0.0
        self.calls = 0
        self.exhausted = False
        self.consecutive_fail = 0
        self.by_model = {}

    def _model(self, name):
        with self.lock:
            m = self.models.get(name)
            if m is None:
                m = self._init_model("OpenAI", name, self.api_key, self.base_url, timeout=120.0,
                                     max_retries=0, verify_ssl=True)
                self.models[name] = m
            return m

    def remaining(self):
        return None if not self.budget else self.budget - self.spent

    def _charge(self, model, usage, text_len):
        pin = pout = rt = 0
        if usage is not None:
            pin = int(getattr(usage, "input_tokens", 0) or 0)
            pout = int(getattr(usage, "output_tokens", 0) or 0)
            rt = int(getattr(usage, "reasoning_tokens", 0) or 0)
        if not pin and not pout:
            pout = max(1, text_len // 3)  # usage missing: pessimistic estimate
        extra = max(0, rt - pout)
        cost = pin / 10.0 + pout + extra
        with self.lock:
            self.spent += cost
            self.calls += 1
            s = self.by_model.setdefault(model, [0, 0.0])
            s[0] += 1
            s[1] += cost
        return pin, pout, rt, cost

    async def _invoke(self, model, messages, max_tokens, temperature, timeout):
        m = self._model(model)
        return await m.invoke(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)

    def chat(self, model, messages, max_tokens, temperature=0.0, timeout=90.0, tag=""):
        """Blocking call usable from worker threads (runs on the harness event loop)."""
        if self.exhausted:
            raise BudgetExhausted()
        last = None
        attempt = 0
        while attempt < 3:
            t0 = time.time()
            try:
                coro = self._invoke(model, messages, max_tokens, temperature, timeout)
                fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
                msg = fut.result(timeout + 60)
            except Exception as e:  # classify provider errors
                last = e
                kind = classify_error(e)
                self.log({"ev": "llm_error", "tag": tag, "model": model, "kind": kind,
                          "err": str(e)[:300], "dt": round(time.time() - t0, 2)})
                if kind == "budget":
                    self.exhausted = True
                    raise BudgetExhausted() from e
                if kind == "rate":
                    # Concurrency/rate limit: wait and retry without burning an attempt, but a
                    # proxy that keeps refusing for minutes is treated as out of budget.
                    with self.lock:
                        self.rate_wait += 2.0
                        too_long = self.rate_wait > 240
                    if too_long:
                        self.exhausted = True
                        raise BudgetExhausted() from e
                    time.sleep(2.0)
                    continue
                with self.lock:
                    self.consecutive_fail += 1
                    many = self.consecutive_fail >= 6
                if many:
                    # The proxy stopped answering: most likely the budget is gone.
                    self.exhausted = True
                    raise BudgetExhausted() from e
                if kind == "fatal":
                    break
                attempt += 1
                time.sleep(1.5 * attempt)
                continue
            with self.lock:
                self.consecutive_fail = 0
                self.rate_wait = 0.0
            text = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            reasoning = getattr(msg, "reasoning_content", None) or ""
            pin, pout, rt, cost = self._charge(model, getattr(msg, "usage_metadata", None), len(text) + len(reasoning))
            self.log({"ev": "llm", "tag": tag, "model": model, "in": pin, "out": pout, "rt": rt,
                      "cost": round(cost, 1), "spent": round(self.spent, 1), "dt": round(time.time() - t0, 2),
                      "finish": getattr(msg, "finish_reason", None)})
            return text, cost, getattr(msg, "finish_reason", None)
        raise CallFailed(str(last)[:200])


def classify_error(e):
    text = []
    cur, seen = e, 0
    while cur is not None and seen < 6:
        text.append(repr(cur))
        status = getattr(cur, "status_code", None) or getattr(getattr(cur, "response", None), "status_code", None)
        if status:
            text.append("status=%s" % status)
        cur = cur.__cause__ or cur.__context__
        seen += 1
    t = " ".join(text).lower()
    if any(s in t for s in ("insufficient_quota", "budget", "quota", "status=402", "error 402", "limit exceeded")):
        return "budget"
    if "429" in t or "ratelimit" in t or "rate limit" in t or "одновремен" in t or "too many" in t:
        return "rate"
    if any(s in t for s in ("status=401", "status=403", "error 401", "error 403", "invalid_api_key")):
        return "budget"  # the proxy refusing the key is treated like a spent budget
    if any(s in t for s in ("status=400", "error 400", "status=404", "error 404")):
        return "fatal"
    return "transient"

"""LLM access through openJiuwen (`init_model` + `Model.invoke`) with exact budget accounting.

Accounting follows the organizers: cost = prompt_tokens / 10 + completion_tokens, where the
completion already contains reasoning tokens (verified on the proxy: reasoning is included in
completion_tokens). If a provider reports reasoning separately and it is larger than the
completion, the difference is added.

The accounting and the retry policy are openJiuwen rails (harness/oj_rails.py) fired by the
SDK's `@rail` decorator around every `Model.invoke`; this module keeps the blocking interface
the solver uses (`chat`, `remaining`, `spent`, `calls`, `by_model`, `exhausted`).
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


REASONING_STAND_IN_TOKENS = 3000


class BudgetExhausted(Exception):
    pass


class CallFailed(Exception):
    pass


class LLM:
    def __init__(self, base_url, api_key, budget=None, log=None, loop=None, fallback=()):
        from openjiuwen.core.foundation.llm.model import init_model
        from harness.oj_rails import CostLedger, ProxyBudgetRail, ProxyResilienceRail, RailedCaller
        self._init_model = init_model
        self.base_url = normalize_base(base_url)
        self.api_key = api_key
        self.budget = budget
        self.log = log or (lambda rec: None)
        if loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="llm-loop", daemon=True).start()
        self.loop = loop
        self.alt_tried = False
        self.models = {}
        # Models the proxy says it does not serve, and the order in which to replace them.
        self.dead = set()
        self.fallback = list(fallback)
        self.rate_wait = 0.0
        self.lock = threading.Lock()
        self.ledger = CostLedger(total=budget)
        self.consecutive_fail = 0
        self.caller = RailedCaller(self._model)
        asyncio.run_coroutine_threadsafe(
            self.caller.register(ProxyBudgetRail(self.ledger),
                                 ProxyResilienceRail(self.ledger, classify_error, self._try_alt_base, self.log,
                                                     self._mark_dead)),
            self.loop).result(30)

    # the ledger is the single source of these numbers (read by the planner and the trace)
    @property
    def spent(self):
        return self.ledger.spent

    @property
    def calls(self):
        return self.ledger.calls

    @property
    def by_model(self):
        return self.ledger.by_model

    @property
    def exhausted(self):
        return self.ledger.exhausted

    @exhausted.setter
    def exhausted(self, value):
        self.ledger.proxy_exhausted = bool(value)

    def serving(self, name):
        """The model a call for `name` goes to: itself, or the first live fallback if the proxy
        said `name` does not exist (the other no-reasoning model first, the reasoning one last)."""
        if name not in self.dead:
            return name
        for alt in self.fallback:
            if alt not in self.dead:
                return alt
        return name

    def _mark_dead(self, e, name):
        """If the error says the proxy does not serve `name`, route its calls elsewhere. True when
        a live replacement exists (the call is retried there)."""
        with self.lock:
            if name in self.dead and model_missing(e, name):
                return self.serving(name) != name  # an attempt sent before the switch: retry it
            name = self.serving(name)  # the model this attempt went to
            if not model_missing(e, name):
                return False
            self.dead.add(name)
            alt = self.serving(name)
        self.log({"ev": "model_missing", "model": name, "now": alt if alt != name else None})
        return alt != name

    def _model(self, name):
        name = self.serving(name)
        with self.lock:
            m = self.models.get(name)
            if m is None:
                m = self._init_model("OpenAI", name, self.api_key, self.base_url, timeout=120.0,
                                     max_retries=0, verify_ssl=True)
                self.models[name] = m
            return m

    def remaining(self):
        return self.ledger.remaining()

    def chat(self, model, messages, max_tokens, temperature=0.0, timeout=90.0, tag="", max_wait=180.0):
        """Blocking call usable from worker threads (runs on the harness event loop).

        Failed calls cost nothing, so errors never end the run: rate limits, refused keys,
        outages are retried with backoff for up to `max_wait` seconds per call (then the task
        is left for a later pass). Only an explicit "out of quota/budget" answer stops new
        calls, and a 400 context-length error fails just this call."""
        from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
        if self.exhausted:
            raise BudgetExhausted()
        served = self.serving(model)
        if served != model and "noreason" in model and "noreason" not in served:
            # a reasoning model standing in for a missing no-reasoning one: room to think first
            max_tokens = max(max_tokens, REASONING_STAND_IN_TOKENS)
        t0 = time.time()
        ctx = AgentCallbackContext(agent=self.caller, inputs=ModelCallInputs(messages=messages))
        ctx.extra.update({"tag": tag, "model": model, "give_up": t0 + max_wait})
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.caller.model_call(ctx, model, max_tokens, temperature, timeout), self.loop)
            try:
                msg = fut.result(max_wait + timeout + 60)
            except BaseException:
                fut.cancel()  # do not keep a proxy slot busy with an abandoned call
                raise
        except Exception as e:
            if ctx.extra.get("kind") == "budget" or self.exhausted:
                raise BudgetExhausted() from e
            raise CallFailed(str(e)[:200])
        if isinstance(msg, dict):  # refused by the budget rail before the call
            raise BudgetExhausted()
        text = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        pin, pout, rt, cost = ctx.extra["charge"]
        self.log({"ev": "llm", "tag": tag, "model": self.serving(model), "in": pin, "out": pout, "rt": rt,
                  "cost": round(cost, 1), "spent": round(self.spent, 1), "dt": round(time.time() - t0, 2),
                  "finish": getattr(msg, "finish_reason", None)})
        return text, cost, getattr(msg, "finish_reason", None)

    def _try_alt_base(self, err):
        """A 404 from a base URL given without /v1 (or with /chat/completions): switch once."""
        t = str(err).lower()
        if self.alt_tried or "404" not in t:
            return False
        self.alt_tried = True
        alt = alternative_base(self.base_url)
        if not alt:
            return False
        with self.lock:
            self.base_url = alt
            self.models = {}
        self.log({"ev": "base_url_switched"})
        return True


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
    if ("status=400" in t or "error 400" in t or "status=413" in t) and ("context" in t or "too long" in t or "length" in t):
        return "fatal"  # prompt too long for the model: a per-task problem, not the budget
    if ("insufficient_quota" in t or "budget exhausted" in t or "budget exceeded" in t or "out of budget" in t
            or "status=402" in t or "error 402" in t or "error code: 402" in t or "payment required" in t):
        return "budget"  # an explicit "spent" answer (402 Payment Required in any form)
    if ("status=400" in t or "error 400" in t or "status=404" in t or "error 404" in t) and "429" not in t:
        return "fatal"
    # "одновремен" (ru): "concurrent" - the dev proxy words its concurrency 429 in Russian
    if "429" in t or "ratelimit" in t or "rate limit" in t or "rate_limit" in t or "одновремен" in t or "too many" in t:
        return "rate"
    # Refused keys, quota wording without a clear "spent" meaning, outages: retried with
    # backoff (failed calls are free).
    return "transient"


_MISSING_WORDS = ("does not exist", "not exist", "not found", "not a valid", "not available", "not supported",
                  "unsupported", "invalid model", "unknown model", "no such model")


def model_missing(e, model):
    """A 400/404 that names the requested model as not served ("The model `qwen3.8-27b` does not
    exist", code model_not_found). The SDK wraps every error as "model call failed", so the
    model's own name (or the explicit code) is required - a wrong base URL's 404 is not this."""
    t = " ".join(repr(x) for x in _chain(e)).lower()
    if "429" in t:
        return False
    if "model_not_found" in t:
        return True
    return bool(model) and model.lower() in t and any(w in t for w in _MISSING_WORDS)


def _chain(e):
    seen = 0
    while e is not None and seen < 6:
        yield e
        e = e.__cause__ or e.__context__
        seen += 1


def normalize_base(url):
    """Accept a base given with a trailing /chat/completions or slash."""
    u = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions"):
        if u.endswith(tail):
            u = u[: -len(tail)]
    return u


def alternative_base(url):
    from urllib.parse import urlparse
    u = normalize_base(url)
    path = urlparse(u).path
    if not path.rstrip("/").endswith("/v1"):
        return u + "/v1"
    return None

"""openJiuwen rails around every model call: the budget ledger and the retry policy live in
rail hooks (`before_model_call` / `after_model_call` / `on_model_exception`) that the SDK's
`@rail` decorator fires around `Model.invoke`, the same way the SDK's own agents are built.

Rails follow the SDK samples:
  - ProxyBudgetRail       <- agent_teams.workflow.backends.budget_rail.SwarmflowBudgetRail
                             (ledger: agent_teams.workflow.engine.budget.BudgetLedger),
                             rsi.harness_rsi.auto_harness.rails.budget_rail.BudgetRail
  - ProxyResilienceRail   <- harness.rails.tool_call_resilience_rail.ToolCallResilienceRail
                             (same decision layers, hooked on the model call instead of a tool call)

The accounting and the retry policy are exactly those of harness/llm.py before this change
(organisers' formula prompt/10 + completion, reasoning inside completion; free failed calls
retried with backoff up to `max_wait` seconds, 402/quota answers stop new calls, 400/404 fail
the call), so the sequence of requests sent to the proxy is unchanged.
"""
import asyncio
import time

from openjiuwen.agent_teams.workflow.engine.budget import BudgetLedger
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
    ModelCallInputs,
    rail,
)
from openjiuwen.harness.rails.tool_call_resilience_rail import ToolCallResilienceRail


class CostLedger(BudgetLedger):
    """The SDK's run-scoped token ledger, counting in the organisers' units (a float: output
    tokens plus a tenth of the prompt tokens). `exhausted` additionally honours an explicit
    "quota spent" answer of the proxy, which can come before the counted ceiling."""

    def __init__(self, total=None):
        super().__init__(total=total)
        self.spent = 0.0
        self.calls = 0
        self.by_model = {}
        self.proxy_exhausted = False

    def add(self, tokens):
        if tokens > 0:
            self.spent += tokens

    def bill(self, model, cost):
        self.add(cost)
        self.calls += 1
        s = self.by_model.setdefault(model, [0, 0.0])
        s[0] += 1
        s[1] += cost

    def remaining(self):
        return None if not self.total else self.total - self.spent

    @property
    def exhausted(self):
        return self.proxy_exhausted


def charge_of(usage, text_len, max_tokens=None):
    """Organisers' accounting for one reply: prompt/10 + completion; the completion already
    contains the reasoning (verified on the proxy); a provider reporting reasoning separately
    and larger than the completion is charged the difference. No usage: pessimistic estimate."""
    pin = pout = rt = 0
    if usage is not None:
        pin = int(getattr(usage, "input_tokens", 0) or 0)
        pout = int(getattr(usage, "output_tokens", 0) or 0)
        rt = int(getattr(usage, "reasoning_tokens", 0) or 0)
    if not pin and not pout:
        pout = max(1, text_len // 3)
        if max_tokens:
            pout = min(pout, int(max_tokens))  # a completion is never billed past max_tokens
    extra = max(0, rt - pout)
    return pin, pout, rt, pin / 10.0 + pout + extra


class ProxyBudgetRail(AgentRail):
    """Bills every returned model call to the shared ledger (SwarmflowBudgetRail pattern) and
    refuses to start a call once the proxy said the quota is spent."""

    priority = 950  # ahead of the retry rail: a call that cannot be paid is refused first

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.ledger.exhausted:
            ctx.request_force_finish({"budget_exhausted": True})

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ModelCallInputs) or inputs.response is None:
            return  # a failed attempt costs nothing
        msg = inputs.response
        text = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        reasoning = getattr(msg, "reasoning_content", None) or ""
        pin, pout, rt, cost = charge_of(getattr(msg, "usage_metadata", None), len(text) + len(reasoning),
                                        ctx.extra.get("max_tokens"))
        self.ledger.bill(ctx.extra.get("model", "?"), cost)
        ctx.extra["charge"] = (pin, pout, rt, cost)


class ProxyResilienceRail(ToolCallResilienceRail):
    """The SDK's resilience rail, applied to the proxy call: failed calls are free, so transport
    errors, rate limits and outages are retried with backoff until `give_up`; an explicit
    "quota spent" answer marks the ledger exhausted; "model not found" moves the call (and later
    ones) to another model; another 400/404 fails only this call (after one switch of the base
    URL on a 404). The retry itself is the `@rail` decorator's loop."""

    priority = 70

    def __init__(self, ledger, classify, switch_base, log, mark_dead=lambda e, model, served: False):
        # the attempt budget is a time budget here (`give_up`), as in the previous retry loop
        super().__init__(max_attempts=10 ** 6)
        self.ledger = ledger
        self.classify = classify
        self.switch_base = switch_base
        self.log = log
        self.mark_dead = mark_dead

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        e = ctx.exception
        kind = self.classify(e)
        n = ctx.retry_attempt + 1
        ctx.extra["kind"] = kind
        self.log({"ev": "llm_error", "tag": ctx.extra.get("tag", ""), "model": ctx.extra.get("model"),
                  "kind": kind, "err": str(e)[:300], "dt": round(time.time() - ctx.extra.get("t0", time.time()), 2),
                  "transport": self._is_retryable_exception(e)})
        if kind == "budget":
            self.ledger.proxy_exhausted = True
            return
        if kind in ("fatal", "transient") and self.mark_dead(e, ctx.extra.get("model"), ctx.extra.get("served")):
            # "model not found" (named in the error): the base URL is right, the model is not
            # served - retry the call on another model, never switch the base URL for this.
            ctx.extra["kind"] = "model"
            ctx.request_retry()
            return
        if kind == "fatal":
            if self.switch_base(e):
                ctx.request_retry()
            return
        if time.time() > ctx.extra.get("give_up", 0) or self.ledger.exhausted:
            return
        ctx.request_retry(min(3.0, 1.0 + n) if kind == "rate" else min(30.0, 2.0 ** min(n, 5)))


class RailedCaller:
    """The object the rails are registered on (what an agent is to the SDK's rail chain):
    `model_call` is the one railed method, so each proxy call fires the hooks above."""

    def __init__(self, resolve_model, tokens_for=lambda model, max_tokens: max_tokens, serving=lambda model: model):
        self.agent_callback_manager = AgentCallbackManager("proxy-caller-%d" % id(self))
        self.resolve_model = resolve_model
        self.tokens_for = tokens_for
        self.serving = serving

    async def register(self, *rails):
        for r in rails:
            await self.agent_callback_manager.register_rail(r, self)

    @rail(before=AgentCallbackEvent.BEFORE_MODEL_CALL, after=AgentCallbackEvent.AFTER_MODEL_CALL,
          on_exception=AgentCallbackEvent.ON_MODEL_EXCEPTION)
    async def model_call(self, ctx, model, max_tokens, temperature, timeout):
        ctx.extra["t0"] = time.time()
        # resolved per attempt: the base URL may have been switched, or the model found missing
        # (a stand-in reasoning model gets room to think on this very retry)
        ctx.extra["served"] = self.serving(model)  # the model this attempt actually goes to
        max_tokens = self.tokens_for(model, max_tokens)
        ctx.extra["max_tokens"] = max_tokens
        m = self.resolve_model(model)
        # The SDK's own timeout ends a slow attempt at `timeout` s; the outer cap is the backstop
        # for a transport that never answers (as the previous per-attempt wait was).
        msg = await asyncio.wait_for(
            m.invoke(ctx.inputs.messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout),
            timeout + 60)
        ctx.inputs.response = msg
        return msg

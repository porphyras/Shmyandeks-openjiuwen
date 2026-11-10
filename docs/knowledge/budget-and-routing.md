---
{
  "type": "Reference",
  "title": "Budget accounting and task routing",
  "description": "Cost units, forecasts, reservations, model ladders and retry admission.",
  "group": "Runtime",
  "tags": [
    "budget",
    "tokens",
    "forecast",
    "reasoning",
    "бюджет",
    "токены"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "run",
      "resource": "../../run_agent.py",
      "sha256": "cb8f82ece57b7a67814204e3c9049353b086e65ebf2b75af574ad6d365d3c0a8"
    },
    {
      "id": "llm",
      "resource": "../../harness/llm.py",
      "sha256": "2c1a09762cd67fa1a09ac7d7f4327e56d1ef3c8fd1086b4511fed596b9996d6d"
    },
    {
      "id": "rails",
      "resource": "../../harness/oj_rails.py",
      "sha256": "0a5578d060334365447908040b5c69a1ed6db51c6556559d990efc786fef9e56"
    },
    {
      "id": "fit",
      "resource": "../../bench/tools/fit_cost.py",
      "sha256": "0e3dbdb61f25b935378590fb934e531e78da9891bbe74767d179e8c1962aa2f5"
    }
  ]
}
---
# Budget accounting and task routing

CostLedger and charge_of in oj_rails.py implement charged units: prompt/10 plus
completion, with handling for separately reported reasoning. Model usage is
billed after replies; an explicit proxy-quota error controls exhaustion.[^rails]
LLM.chat bridges worker threads to the SDK event loop. classify_error separates
quota, fatal per-call errors, rate limits and transient failures.[^llm]
A 400/404 that names the requested model as not served (or carries the code
model_not_found) marks that model dead: its calls move to the next live model
of the ladder order, the base URL is left alone, and a reasoning model standing
in for a no-reasoning one gets room to think. The SDK wraps every error as
"model call failed", so a 404 without the model's name is not taken for this.[^llm][^rails]

Run owns model ladders, PRIOR_P, COST_FIT, argument/environment precedence,
reasoning limits and economy mode. Read those definitions for exact defaults.
estimate returns a forecast, not a guaranteed bound; task priority includes reward,
success probability and the p90 factor. ask checks affordability again.[^run]

Reservations represent forecast cost of active tasks. Actual spend and reserved
cost are distinct quantities. Inspect the current one/worker/pool code before
changing admission: an entry that does not fit beside active reservations is
deferred, then requeued once the workers have joined and reservations are
released; one that does not fit the remaining budget alone is dropped. A comment claiming budget safety is not evidence of a hard cap.[^run]

fit_cost fits from recorded run estimates. Its in-sample error and a hold-out
calibration result answer different questions; preserve that distinction.[^fit]
See [verification evidence](testing-and-evidence.md) for reproducible comparisons.

[^rails]: [Cost ledger and rail hooks](../../harness/oj_rails.py).
[^llm]: [Client, remaining budget and error classification](../../harness/llm.py).
[^run]: [Selection and admission](../../run_agent.py).
[^fit]: [Forecast fitting](../../bench/tools/fit_cost.py).

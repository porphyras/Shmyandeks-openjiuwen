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
      "sha256": "855bc3de3640d3582942a4ec55defbb034665fa51f0c6ecb83e9bc6c7fd66609"
    },
    {
      "id": "llm",
      "resource": "../../harness/llm.py",
      "sha256": "56325f9d9bbde2975d53686d0aa9a30cb0a4be7e34e24a98d1f41e7628cc02de"
    },
    {
      "id": "rails",
      "resource": "../../harness/oj_rails.py",
      "sha256": "ead935d0fbe22844f9ee70be1d1061647dc46f2d0cba5c27c645fdf9fa2e91cd"
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

Run owns model ladders, PRIOR_P, COST_FIT, argument/environment precedence,
reasoning limits and economy mode. Read those definitions for exact defaults.
estimate returns a forecast, not a guaranteed bound; task priority includes reward,
success probability and the p90 factor. ask checks affordability again.[^run]

Reservations represent forecast cost of active tasks. Actual spend and reserved
cost are distinct quantities. Inspect the current one/worker/pool code before
changing admission: this baseline skips entries that do not fit beside active
reservations. A comment claiming budget safety is not evidence of a hard cap.[^run]

fit_cost fits from recorded run estimates. Its in-sample error and a hold-out
calibration result answer different questions; preserve that distinction.[^fit]
See [verification evidence](testing-and-evidence.md) for reproducible comparisons.

[^rails]: [Cost ledger and rail hooks](../../harness/oj_rails.py).
[^llm]: [Client, remaining budget and error classification](../../harness/llm.py).
[^run]: [Selection and admission](../../run_agent.py).
[^fit]: [Forecast fitting](../../bench/tools/fit_cost.py).

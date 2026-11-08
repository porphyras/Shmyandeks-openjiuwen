---
{"okf_version": "0.2"}
---
# Repository knowledge

Read the topic for your change, then its linked sources. Source fingerprints
detect drift; they do not assert human review or semantic correctness.

# Development

* [Catalog maintenance and upstream provenance](catalog-maintenance.md) - Maintain the OKF profile, regenerate navigation and detect source drift locally.
* [SSOT and change-driven development](development-workflow.md) - Separate current behavior, intended changes, historical results and verification.
* [Verification and evidence](testing-and-evidence.md) - Choose offline tests, generated-set grading, proxy comparisons or Docker checks.

# Runtime

* [Budget accounting and task routing](budget-and-routing.md) - Cost units, forecasts, reservations, model ladders and retry admission.
* [Execution environment and SDK boundary](environment.md) - Container inputs, read-only execution, sandbox processes and SDK dependencies.
* [Solver paths and local checks](solving-and-verification.md) - Find the math, data, knowledge-base and code-repair paths and their validators.
* [Task intake and outputs](task-lifecycle.md) - How a task batch becomes separate model requests and persisted answers.

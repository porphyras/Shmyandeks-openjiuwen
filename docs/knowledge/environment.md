---
{
  "type": "Reference",
  "title": "Execution environment and SDK boundary",
  "description": "Container inputs, read-only execution, sandbox processes and SDK dependencies.",
  "group": "Runtime",
  "tags": [
    "docker",
    "sandbox",
    "sdk",
    "openjiuwen",
    "environment",
    "окружение"
  ],
  "status": "stable",
  "generated": {
    "by": "repo-catalog/0.1",
    "at": "2026-11-08T11:31:44+00:00"
  },
  "sources": [
    {
      "id": "docker",
      "resource": "../../Dockerfile",
      "sha256": "28fbd62db1d87e4a835ca87d51a8bfd837f0c14b4ccce5d9d81d51efb4e8e488"
    },
    {
      "id": "ignore",
      "resource": "../../.dockerignore",
      "sha256": "45b1a1f1b7120eec8f94d09b5491dc93f9df37f5726e97f8932dfea1c3ed9ba5"
    },
    {
      "id": "sandbox",
      "resource": "../../harness/sandbox.py",
      "sha256": "eebfb5b134b4a6ef4185d5bfd3f37441a3b140aa7a681a3a42e356ff4a3d9678"
    },
    {
      "id": "llm",
      "resource": "../../harness/llm.py",
      "sha256": "7d54bd2ac12976d12750e61dfaf12067fe84663c78da80426c7823c956286d7b"
    },
    {
      "id": "run",
      "resource": "../../run_agent.py",
      "sha256": "934295ab9fddc76e1f180957fdf5782e4b0f6a1659fbe2006a54107af2946b9f"
    },
    {
      "id": "deps",
      "resource": "../../requirements.in",
      "sha256": "b54616535bfe397a3793ccbe30c8d5c531932fca6b53bb86e73911e83b16066c"
    },
    {
      "id": "lock",
      "resource": "../../requirements.txt",
      "sha256": "9efa603ad9ad2f71fc5d7ceec7fb804850086f34fe65ff7b4b15e87378e57406"
    }
  ]
}
---
# Execution environment and SDK boundary

Dockerfile owns the image and installation; its ENV turns off model-hub downloads
and telemetry exporters of SDK paths the harness never calls (HF_*_OFFLINE, OTEL_*).
While no model call has succeeded, a line on stderr each minute says the run is
waiting for the proxy (the policy is to wait, not to quit). requirements.in names direct packages,
and requirements.txt locks the resolved graph. Consult the files for versions.
Only run_agent.py, harness/ and build dependencies are copied into the image;
.dockerignore excludes developer docs, skills, tools, tests and bench artifacts.[^docker][^ignore][^deps][^lock]

The harness reads the mounted task directory and optional output/budget arguments.
Model credentials come from the environment at startup. main takes the key,
installs signal handling and configures SDK logging before creating Run.
Avoid importing the SDK before logging configuration when changing startup.[^run][^llm]
Run probes the first task folder with a temporary file before any work: a task
directory the container's user cannot write (root without capabilities on a host
folder) is reported on stderr at once, and the run ends with a one-line summary
of answers written and units spent, also when a signal or the time limit stops
it. The first failed proxy call prints one hint about LLM_BASE_URL/LLM_API_KEY
(the error text stays in the trace, where the key is redacted); a model the proxy
does not serve is handled by the fallback and prints no hint.[^run][^llm]

sandbox.py defines the interpreter, clean environment, subprocess launcher,
timeouts, output capture and resource limits. Model programs execute in task
working directories. This process boundary is not a separate per-task filesystem:
answer/source protection and the judge's container restrictions serve different
purposes.[^sandbox]

Reproduce container-specific behavior with the judge simulator described in
[testing](testing-and-evidence.md). A host-only unit pass does not validate PID 1,
Linux resource limits, read-only mounts or the full SDK import graph.

[^docker]: [Image construction](../../Dockerfile).
[^ignore]: [Build-context allowlist](../../.dockerignore).
[^deps]: [Direct dependencies](../../requirements.in).
[^sandbox]: [Subprocess execution](../../harness/sandbox.py).
[^run]: [Startup, key handling and signals](../../run_agent.py).
[^llm]: [SDK logging and model access](../../harness/llm.py).

[^lock]: [Resolved dependency versions](../../requirements.txt).

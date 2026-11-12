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
      "sha256": "a17039365822c69a8954e1e55051e8e4f3eb7fd105ff7c7814496f5aa9b2b98d"
    },
    {
      "id": "ignore",
      "resource": "../../.dockerignore",
      "sha256": "eeb5babe28f41d8a66498b97d0c7596960c839d0993b29494500b14c7750b072"
    },
    {
      "id": "sandbox",
      "resource": "../../harness/sandbox.py",
      "sha256": "0941812400eaab539d3b850e0dac10ae687fb2620bd25aa09ebceefab8dbecbc"
    },
    {
      "id": "llm",
      "resource": "../../harness/llm.py",
      "sha256": "7d54bd2ac12976d12750e61dfaf12067fe84663c78da80426c7823c956286d7b"
    },
    {
      "id": "run",
      "resource": "../../run_agent.py",
      "sha256": "08abe487e6b6791d60a95719013431978fdfdd153399fc5fc509b3bba948f0bf"
    },
    {
      "id": "deps",
      "resource": "../../requirements.in",
      "sha256": "9da3bdf4266dd037d91afedc94412e90f1f4d0a4ad31d525fbfcb94517cf1888"
    },
    {
      "id": "lock",
      "resource": "../../requirements.txt",
      "sha256": "203738e2d461bbc9077ed4c1d42dcec2ce9f8e0d11ef1b9b0ad1df2b6df1d4ce"
    }
  ]
}
---
# Execution environment and SDK boundary

Dockerfile owns the image and installation (pip retries a connection 10 times with a 100 s timeout,
and the whole pip install is repeated up to 5 times with its download cache on, since pip 25.0
does not retry a download cut mid-stream: the organisers build the image, and a network hiccup
must not fail the build); its ENV turns off model-hub downloads
and telemetry exporters of SDK paths the harness never calls (HF_*_OFFLINE, OTEL_*)
and keeps BLAS/OpenMP to one thread and the tokenizers (rayon) pool to two (the
container sees the host's CPU count, not the --cpus quota, and threads count against
--pids-limit).
While no model call has succeeded, a line on stderr each minute says the run is
waiting for the proxy (the policy is to wait, not to quit). requirements.in names direct packages,
and requirements.txt locks the resolved graph. Consult the files for versions.
Only run_agent.py, harness/, models/ (the int8 embedding model of the last search look,
sha256-checked by harness/embed.py before loading) and build dependencies are copied into the image;
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
working directories with one BLAS/OpenMP thread, under -s -P rather than -I so that
SAFE_ENV's PYTHONHASHSEED=0 keeps set order the same from run to run; test runs add the task root (and src/) to PYTHONPATH; output over the cap keeps its
first and last halves, so the answer line and the unittest summary survive. This process boundary is not a separate per-task filesystem:
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

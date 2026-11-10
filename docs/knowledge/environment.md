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
      "sha256": "2c1a09762cd67fa1a09ac7d7f4327e56d1ef3c8fd1086b4511fed596b9996d6d"
    },
    {
      "id": "run",
      "resource": "../../run_agent.py",
      "sha256": "cb8f82ece57b7a67814204e3c9049353b086e65ebf2b75af574ad6d365d3c0a8"
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

Dockerfile owns the image and installation; requirements.in names direct packages,
and requirements.txt locks the resolved graph. Consult the files for versions.
Only run_agent.py, harness/ and build dependencies are copied into the image;
.dockerignore excludes developer docs, skills, tools, tests and bench artifacts.[^docker][^ignore][^deps][^lock]

The harness reads the mounted task directory and optional output/budget arguments.
Model credentials come from the environment at startup. main takes the key,
installs signal handling and configures SDK logging before creating Run.
Avoid importing the SDK before logging configuration when changing startup.[^run][^llm]

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

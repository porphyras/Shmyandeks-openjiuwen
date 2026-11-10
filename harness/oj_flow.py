"""The per-task pipeline as an openJiuwen Workflow: start -> inspect -> solve -> finish -> end.

Each node runs one blocking step of the harness (inspection and prechecks; the model-backed
solve with its budget reservation; the write-out with its second opinions) on the node
executor. The workflow state carries only the task name and three flags (`go`: the next step
applies, `stop`: the proxy quota is spent, `defer`: the task waits for in-flight reservations
to be released), while the task objects, inspections and outcomes
stay in the Run's own registries: the SDK deep-copies everything it stores, and the task
object must stay the one the planner holds.

A workflow object is built per invocation (one shared instance invoked concurrently from
several worker threads loses results); a build plus a run costs about 60 ms.
"""
import asyncio
import threading

from openjiuwen.core.session import WORKFLOW_EXECUTE_TIMEOUT
from openjiuwen.core.workflow import End, Start, Workflow, WorkflowComponent, create_workflow_session

# The SDK cuts a workflow after 60 s by default; a task may legitimately run for many minutes
# (retries while the proxy is down, test runs), so the session is created without a limit -
# the harness keeps its own time limit and watchdog.
SESSION_ENVS = {WORKFLOW_EXECUTE_TIMEOUT: -1}

KEYS = ("task", "level", "go", "stop", "defer")


class StepComponent(WorkflowComponent):
    def __init__(self, fn, executor):
        super().__init__()
        self.fn = fn
        self.executor = executor

    async def invoke(self, inputs, session, context):
        return await asyncio.get_running_loop().run_in_executor(self.executor, self.fn, inputs)


def _schema(prev):
    """Inputs of a node: the previous node's outputs, or (after the start node) the workflow's
    own inputs, which the state keeps at the root rather than under the start node's id."""
    if prev == "start":
        return {k: "${%s}" % k for k in KEYS}
    return {k: "${%s.%s}" % (prev, k) for k in KEYS}


def build_task_flow(steps, executor):
    """`steps`: ordered (name, fn) pairs; every fn takes and returns a dict with KEYS."""
    wf = Workflow()
    wf.set_start_comp("start", Start())
    prev = "start"
    for name, fn in steps:
        wf.add_workflow_comp(name, StepComponent(fn, executor), inputs_schema=_schema(prev))
        wf.add_connection(prev, name)
        prev = name
    wf.set_end_comp("end", End(), inputs_schema=_schema(prev))
    wf.add_connection(prev, "end")
    return wf


_LOOP = None
_LOOP_LOCK = threading.Lock()


def background_loop():
    """An event loop on its own thread, for a client that brings none (test doubles)."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            threading.Thread(target=_LOOP.run_forever, name="flow-loop", daemon=True).start()
        return _LOOP


def run_task_flow(loop, steps, executor, task_name, level, timeout):
    """Blocking, from a worker thread: runs the flow for one task on the harness event loop
    and returns the final state dict."""
    inputs = {"task": task_name, "level": level, "go": True, "stop": False, "defer": False}

    async def run():  # the workflow and its session are built on the loop (they need a current loop)
        return await build_task_flow(steps, executor).invoke(inputs, create_workflow_session(envs=SESSION_ENVS))

    out = asyncio.run_coroutine_threadsafe(run(), loop or background_loop()).result(timeout)
    result = getattr(out, "result", None)
    if isinstance(result, dict) and isinstance(result.get("output"), dict):
        return result["output"]
    return inputs

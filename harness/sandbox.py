"""Run model-written code inside a task directory: clean env, timeout, capped output.

Output goes to size-limited temp files (not pipes): a flooding program cannot fill the
harness memory, and a daemonised grandchild cannot keep us waiting on an open pipe.
"""
import os
import signal
import sys
import subprocess
import tempfile
import threading
import time

SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    # Never read a __pycache__ shipped in the task: a source rewritten with the same size in the
    # same second as a cached .pyc would otherwise run the stale cached code.
    "PYTHONPYCACHEPREFIX": "/tmp/.no-pycache",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "MPLBACKEND": "Agg",
    "MPLCONFIGDIR": "/tmp/mpl",
    # One BLAS/OpenMP thread: os.cpu_count() in the judge's container is the host's (88), not the
    # --cpus quota, and a thread per CPU in each program presses on --pids-limit and RLIMIT_AS.
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
MAX_OUT = 64 * 1024
FILE_LIMIT = 32 << 20  # RLIMIT_FSIZE: also caps the captured output files

# Session ids of the sandboxed process groups we started (used to ignore signals they send).
CHILD_SIDS = set()
_LOCK = threading.Lock()
_TMP = "/tmp" if os.path.isdir("/tmp") else None
# Model programs and tests run on the harness's own interpreter (in the image it is the
# image's python3 with pandas/numpy; on a dev machine the venv's).
PYTHON = sys.executable or "python3"


# Children start through this tiny launcher (it only sets resource limits and the signal mask,
# then execs the given command) instead of a preexec_fn (running Python code
# between fork and exec is unsafe in a multi-threaded process): it resets the signal mask
# the harness threads use, sets resource limits and execs the real command.
_BOOT = ("import os, signal, sys\n"
         "try:\n    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})\nexcept Exception:\n    pass\n"
         "import resource\n"
         "for lim, val in ((resource.RLIMIT_AS, 2 << 30), (resource.RLIMIT_FSIZE, %d)):\n"
         "    try:\n        resource.setrlimit(lim, (val, val))\n    except Exception:\n        pass\n"
         "os.execvp(sys.argv[1], sys.argv[1:])\n" % FILE_LIMIT)


def _limits():
    try:
        # The harness blocks SIGTERM/SIGINT in all its threads (they are read with
        # sigwaitinfo); children must start with a normal signal mask.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    except Exception:
        pass
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    except Exception:
        pass


def _read_head(f):
    """The output, or its first and last MAX_OUT/2 bytes: the end carries the answer line and
    unittest's "Ran N tests" / "OK"."""
    size = f.seek(0, 2)
    f.seek(0)
    if size <= MAX_OUT:
        return f.read().decode("utf-8", "replace")
    half = MAX_OUT // 2
    head = f.read(half)
    f.seek(size - half)
    return (head.decode("utf-8", "replace") + "\n...[%d bytes skipped]...\n" % (size - 2 * half)
            + f.read(half).decode("utf-8", "replace"))


def run(argv, cwd, timeout=20, extra_env=None):
    """Run argv in cwd (a task directory) in its own process session, with the clean SAFE_ENV
    (plus `extra_env`, e.g. PYTHONPATH for tests), memory/file-size limits, a timeout and output
    captured to capped temp files. Returns (returncode, stdout, stderr); returncode None on timeout."""
    env = dict(SAFE_ENV)
    env.update(extra_env or {})
    if not os.path.isdir("/usr/local/bin"):
        env["PATH"] = os.environ.get("PATH", env["PATH"])
    try:
        fo = tempfile.TemporaryFile(dir=_TMP)
        fe = tempfile.TemporaryFile(dir=_TMP)
    except OSError as e:
        return -1, "", str(e)
    try:
        try:
            p = subprocess.Popen([PYTHON, "-I", "-S", "-c", _BOOT] + list(argv), cwd=cwd, env=env,
                                 stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                 start_new_session=True, close_fds=True)
        except OSError as e:
            return -1, "", str(e)
        with _LOCK:
            CHILD_SIDS.add(p.pid)
        rc = None
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        # Kill the whole group either way: leftovers must not outlive the call.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        if rc is None:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return rc, _read_head(fo), _read_head(fe)
    finally:
        fo.close()
        fe.close()


def run_python_code(code, cwd, timeout=20):
    """Script file lives in /tmp so the task directory is not polluted."""
    fd, path = tempfile.mkstemp(prefix="s_", suffix=".py", dir=_TMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        # -s -P, not -I: -I implies -E, which would drop SAFE_ENV's PYTHONHASHSEED=0 (set order
        # of strings, hence ties, would change from run to run) and the bytecode settings.
        # The environment is SAFE_ENV only; -P keeps /tmp (the script's folder) off sys.path.
        return run([PYTHON, "-s", "-P", path], cwd, timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def is_sandbox_sender(pid):
    """True if `pid` belongs to one of the sandboxed sessions (a program we ran)."""
    if not pid:
        return False
    with _LOCK:
        if pid in CHILD_SIDS:
            return True
    try:
        sid = os.getsid(pid)
    except OSError:
        return True  # already gone: it was a short-lived child, not the judge
    with _LOCK:
        return sid in CHILD_SIDS

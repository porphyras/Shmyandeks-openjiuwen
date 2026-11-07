"""Run model-written code inside a task directory: clean env, timeout, capped output."""
import os
import signal
import subprocess
import tempfile

SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "MPLBACKEND": "Agg",
    "MPLCONFIGDIR": "/tmp/mpl",
}
MAX_OUT = 64 * 1024


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    except Exception:
        pass


def run(argv, cwd, timeout=20, stdin_text=None):
    """Returns (returncode, stdout, stderr); returncode None on timeout."""
    env = dict(SAFE_ENV)
    if os.path.isdir("/usr/local/bin") is False:
        env["PATH"] = os.environ.get("PATH", env["PATH"])
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                             preexec_fn=_limits if os.name == "posix" else None)
    except OSError as e:
        return -1, "", str(e)
    try:
        out, err = p.communicate(stdin_text.encode() if stdin_text is not None else None, timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        rc = None
    return rc, out[:MAX_OUT].decode("utf-8", "replace"), err[:MAX_OUT].decode("utf-8", "replace")


def run_python_code(code, cwd, timeout=20):
    """Script file lives in /tmp so the task directory is not polluted."""
    fd, path = tempfile.mkstemp(prefix="s_", suffix=".py", dir="/tmp" if os.path.isdir("/tmp") else None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        return run(["python3", "-I", path], cwd, timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

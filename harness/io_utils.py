"""Atomic answer/summary writing. Temp file lives next to the target (same filesystem)."""
import json
import os
import threading

_LOCK = threading.Lock()


def atomic_write(path, text):
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, ".%s.%d.%d.tmp" % (os.path.basename(path), os.getpid(), threading.get_ident()))
    try:
        data = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o666)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        # A directory we may not create files in can still hold a writable file: write in place.
        try:
            data = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
            with open(path, "wb") as f:
                f.write(data)
            return True
        except Exception:
            return False


def atomic_write_bytes(path, data):
    return atomic_write(path, data)


MAX_ANSWER = 1000


def write_answer(task_dir, answer):
    """answer.txt goes to the task dir and, if present, to workspace/ (the task's cwd).
    Returns True if at least one copy was written."""
    text = str(answer).strip()[:MAX_ANSWER] + "\n"
    ok = False
    if not os.path.islink(task_dir):
        ok = atomic_write(os.path.join(task_dir, "answer.txt"), text)
    ws = os.path.join(task_dir, "workspace")
    if os.path.isdir(ws) and not os.path.islink(ws):
        ok = atomic_write(os.path.join(ws, "answer.txt"), text) or ok
    return ok


class Summary:
    """Solutions file for $OUT: a directory gets solutions.json inside, otherwise the path itself."""

    def __init__(self, out):
        self.path = None
        self.data = {}
        if not out:
            return
        try:
            if os.path.isdir(out) or out.endswith(os.sep):
                os.makedirs(out, exist_ok=True)
                self.path = os.path.join(out, "solutions.json")
            else:
                parent = os.path.dirname(out)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                self.path = out
        except OSError:
            self.path = None

    def record_estimates(self, estimates):
        """Per-task token forecast (made before solving) and actual spend, next to the solutions."""
        if not self.path:
            return
        path = os.path.join(os.path.dirname(self.path), "token_estimates.json")
        with _LOCK:
            atomic_write(path, json.dumps(estimates, ensure_ascii=False, indent=1, sort_keys=True))

    def record(self, task_id, answer):
        with _LOCK:
            self.data[task_id] = answer
            if self.path:
                atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True))

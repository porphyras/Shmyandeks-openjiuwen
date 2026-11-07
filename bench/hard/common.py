"""Shared helpers for the hard set builders (standard library only).

Every builder module exposes make(dl_dir) -> list of task specs:
    {"type", "kind", "feasible", "instruction", "check", "expected",
     "files": {relpath: bytes},            # workspace files
     "hidden": {name: str},                # coding only: test_hidden.py, mylib_ref.py
     "meta": {...}}
"""
import ast
import hashlib
import operator
import random
import re
from fractions import Fraction

TAIL = ("Work only inside the current directory. When you are done, write ONLY the final answer "
        "to the file answer.txt in the current directory (for example with bash: echo 42 > answer.txt).")
MATH_TAIL = "Compute it with the bash tool (python3 -c). " + TAIL
DATA_TAIL = "Use bash with python3 to compute it. " + TAIL
SEARCH_HEAD = "The directory kb/ contains a small knowledge base of markdown files."
SEARCH_TAIL = "Use the file tools or bash (ls, grep, cat) to find it. " + TAIL
CODING_INSTRUCTION = (
    "Run `python3 test_mylib.py` in the current directory; some tests fail. Fix the bugs in mylib.py "
    "(do not edit the tests) until all tests pass. Work only inside the current directory. When you are "
    "done, write ONLY the word DONE to the file answer.txt in the current directory (for example with "
    "bash: echo 42 > answer.txt).")


def rng_for(*parts):
    """Deterministic RNG derived from a label via SHA-256."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}


def safe_arith(expr, exact=False):
    """Evaluate a plain arithmetic expression (numbers, + - * / // % **, parentheses) via the AST.
    With exact=True numbers are Fractions (exact rational arithmetic). Raises ValueError on
    anything else. No code is executed."""
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return Fraction(repr(n.value)) if exact else n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow):
                if abs(b) > 12 or abs(a) > 1e6 or (exact and b != int(b)):
                    raise ValueError("pow not supported")
                if exact:
                    b = int(b)
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        raise ValueError("not arithmetic: " + ast.dump(n)[:60])

    return ev(tree)


def slug(s, maxlen=60):
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return (s[:maxlen].rstrip("_")) or "doc"

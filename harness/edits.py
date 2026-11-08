"""A compact exact-edit tool: JSON arguments in, validated source replacements out.

No filesystem access. The caller supplies the only eligible source files and runs
the existing tests before accepting any change.
"""
import ast
import json
import re

MAX_REPLY = 32768
MAX_EDITS = 16


class EditError(ValueError):
    pass


def apply_exact_edits(sources, reply):
    """Apply [[old, new], ...] or [[file, old, new], ...] in memory.

    File names are exact keys in `sources`, supplied by the caller. Each old
    string must match exactly one location, including overlapping occurrences.
    Returns only changed files; any rejected edit leaves the input untouched.
    """
    if not isinstance(reply, str) or len(reply) > MAX_REPLY:
        raise EditError("Edit reply is too large or is not text.")
    text = reply.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n?```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        edits = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise EditError("Expected JSON [[old, new], ...].") from exc
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise EditError("Expected 1 to %d edits." % MAX_EDITS)
    updated = dict(sources)
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, list) or len(edit) not in (2, 3) or not all(isinstance(v, str) for v in edit):
            raise EditError("Edit %d must be [old, new] or [file, old, new]." % index)
        old, new = edit[-2:]
        if not old or not old.strip() or old == new:
            raise EditError("Edit %d needs nonempty old text and a different replacement." % index)
        eligible = [edit[0]] if len(edit) == 3 else list(updated)
        if any(name not in updated for name in eligible):
            raise EditError("Edit %d names a file outside the supplied source files." % index)
        matches = []
        for name in eligible:
            start = updated[name].find(old)
            if start < 0:
                continue
            matches.append(name)
            if updated[name].find(old, start + 1) >= 0:
                raise EditError("Edit %d is ambiguous; include more exact context." % index)
        if len(matches) != 1:
            raise EditError("Edit %d must match one location; include context or an exact file name." % index)
        name = matches[0]
        updated[name] = updated[name].replace(old, new, 1)
    changed = {name: text for name, text in updated.items() if text != sources[name]}
    if not changed:
        raise EditError("Edits made no net change.")
    for name, text in changed.items():
        try:
            ast.parse(text)
        except (SyntaxError, ValueError, UnicodeError) as exc:
            raise EditError("Edits produce invalid Python in %s." % name) from exc
    return changed

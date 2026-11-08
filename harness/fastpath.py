"""Deterministic fast path for tasks whose statement is understood completely.

The organisers allow solving without the model. A task is answered here only when every
sentence of its statement matches a known form with an unambiguous meaning and all the data
it refers to checks out (file, header, row count, knowledge-base cards). Anything else -
an unknown sentence, a missing card, a value absent from the table, a negative stock - is
left to the model path, so a misparse can only cost tokens, never a wrong answer.
"""
import csv
import io
import os
import re

from . import context as ctx

_SENT = re.compile(r"(?<=[.?!])\s+(?=[A-Z])")


def _sentences(text):
    return [s.strip() for s in _SENT.split(text.strip()) if s.strip()]


# ------------------------------------------------------------------ math (warehouse stock)

_START = re.compile(r"A warehouse starts with (\d+|some) boxes\.")
_QUESTION = re.compile(r"How many boxes does it have at the end\?")
_OPS = [
    (re.compile(r"(?:Then it|It) ships (\d+) boxes per day for (\d+) days\."), lambda x, n, d: x - n * d),
    (re.compile(r"(?:Then it|It) receives (\d+) deliveries of (\d+) boxes each\."), lambda x, k, m: x + k * m),
    (re.compile(r"(?:Then the stock is|The stock is then) split equally into (\d+) warehouses and it keeps one "
                r"share \(rounded down\)\."), lambda x, w: x // w),
    (re.compile(r"Then the stock is doubled by merging with a sister warehouse\."), lambda x: 2 * x),
    (re.compile(r"Then the stock is tripled by merging with two sister warehouses of the same size\."),
     lambda x: 3 * x),
    (re.compile(r"Then (\d+) percent of the stock is lost to damage \(the number of lost boxes is rounded down\)\."),
     lambda x, p: x - (x * p) // 100),
    (re.compile(r"Then the stock grows by (\d+) percent \(rounded down\)\."), lambda x, p: x + (x * p) // 100),
    (re.compile(r"Then it sells half of the stock \(the number of boxes sold is rounded up\)\."),
     lambda x: x - (x + 1) // 2),
    (re.compile(r"(?:Then it|It) sends (\d+) boxes to each of (\d+) stores\."), lambda x, s, n: x - s * n),
    (re.compile(r"Customers return (\d+) boxes, which are added back to the stock\."), lambda x, r: x + r),
    (re.compile(r"(?:Then it|It) ships (\d+) boxes\."), lambda x, n: x - n),
    (re.compile(r"(?:Then it|It) ships (\d+) boxes and then receives (\d+) boxes\."), lambda x, a, b: x - a + b),
    (re.compile(r"(?:Then it|It) receives (\d+) deliveries of (\d+) boxes each, but (\d+) boxes in each delivery are "
                r"damaged and thrown away\."), lambda x, k, m, d: x + k * (m - d) if d <= m else None),
    (re.compile(r"Then it packs the stock into crates of (\d+) boxes, ships all full crates and keeps only the "
                r"leftover boxes\."), lambda x, n: x % n),
]
# Quantities the statement never gives: the result cannot be computed exactly (an infeasible
# task), unless a later "it has exactly N boxes" states the stock again.
_VAGUE = [
    re.compile(r"(?:Then it|It) receives (?:a few|several|some) deliveries of \d+ boxes each\."),
    re.compile(r"Then the stock is split equally (?:among|into) (?:several|a few|some) warehouses and it keeps one "
               r"share \(rounded down\)\."),
    re.compile(r"Then the stock is multiplied by merging with (?:a few|several|some) sister warehouses\."),
    re.compile(r"Then it receives all the boxes held by a partner depot\."),
]
_EXACTLY = re.compile(r"After that it has exactly (\d+) boxes\.")


def solve_math(task):
    """Returns ("solved", answer) / ("infeasible", note) / None."""
    sents = _sentences(task.short_instruction())
    if len(sents) < 3:
        return None
    m = _START.fullmatch(sents[0])
    if not m or not _QUESTION.fullmatch(sents[-1]):
        return None
    x = None if m.group(1) == "some" else int(m.group(1))  # None: a stock the text never gives
    for s in sents[1:-1]:
        e = _EXACTLY.fullmatch(s)
        if e:
            n = int(e.group(1))
            if x is not None and x != n:
                return ("infeasible", "the stated stock %d contradicts the operations (%d)" % (n, x))
            x = n
            continue
        if any(rx.fullmatch(s) for rx in _VAGUE):
            x = None
            continue
        for rx, fn in _OPS:
            mm = rx.fullmatch(s)
            if mm:
                if x is not None:
                    args = [int(g) for g in mm.groups()]
                    try:
                        x = fn(x, *args)
                    except ZeroDivisionError:
                        return ("infeasible", "a division by zero (0 warehouses or crates)")
                    if x is None:
                        return None  # an odd case (more damaged boxes than delivered): the model looks
                break
        else:
            return None  # a sentence we do not understand: the model decides
        if x is not None and x < 0:
            return None  # negative stock: something is off, let the model look
    if x is None:
        return ("infeasible", "a quantity the statement does not give")
    return ("solved", str(x))


# ------------------------------------------------------------------ data (sales table)

_HEAD = re.compile(r"The file ([\w./-]+\.csv) has columns ([\w]+(?:,[\w]+)+) \((\d+) rows\)\.")
# (question, row filter, aggregate). An empty selection is never answered here.
_DATA_Q = [
    (re.compile(r"How many units of product '([^']+)' were sold in months (\d+) through (\d+) inclusive\?"),
     lambda r, p, a, b: r["product"] == p and int(a) <= r["month"] <= int(b), lambda sel: sum(r["units"] for r in sel)),
    (re.compile(r"How many units of product '([^']+)' were sold in region '([^']+)' in months (\d+) through (\d+) "
                r"inclusive\?"),
     lambda r, p, g, a, b: r["product"] == p and r["region"] == g and int(a) <= r["month"] <= int(b),
     lambda sel: sum(r["units"] for r in sel)),
    (re.compile(r"How many units of product '([^']+)' were sold in region '([^']+)'\?"),
     lambda r, p, g: r["product"] == p and r["region"] == g, lambda sel: sum(r["units"] for r in sel)),
    (re.compile(r"What is the total revenue \(sum of units \* price\) for region '([^']+)'\?"),
     lambda r, g: r["region"] == g, lambda sel: sum(r["units"] * r["price"] for r in sel)),
    (re.compile(r"What is the total revenue \(sum of units \* price\) for product '([^']+)'\?"),
     lambda r, p: r["product"] == p, lambda sel: sum(r["units"] * r["price"] for r in sel)),
    (re.compile(r"How many orders are there for product '([^']+)'\?"),
     lambda r, p: r["product"] == p, len),
    (re.compile(r"How many orders were placed in region '([^']+)'\?"),
     lambda r, g: r["region"] == g, len),
    (re.compile(r"What is the largest number of units in a single order in region '([^']+)'\?"),
     lambda r, g: r["region"] == g, lambda sel: max(r["units"] for r in sel)),
]
_SALES_COLS = ["order_id", "region", "product", "month", "units", "price"]


def solve_data(task):
    text = task.short_instruction()
    sents = _sentences(text)
    if len(sents) != 2:
        return None
    h = _HEAD.fullmatch(sents[0])
    if not h:
        return None
    fname, cols, nrows = h.group(1), h.group(2).split(","), int(h.group(3))
    if cols != _SALES_COLS:
        return None
    path = os.path.join(task.cwd, fname)
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    raw = ctx.read_text(path, 32 << 20)
    try:
        table = list(csv.reader(io.StringIO(raw.lstrip("\ufeff"))))
    except csv.Error:
        return None
    if not table or [c.strip() for c in table[0]] != _SALES_COLS:
        return None
    body = [r for r in table[1:] if r]
    if len(body) != nrows:
        return None
    rows = []
    try:
        for r in body:
            if len(r) != 6:
                return None
            rows.append({"order_id": r[0], "region": r[1].strip(), "product": r[2].strip(), "month": int(r[3]),
                         "units": int(r[4]), "price": int(r[5])})
    except ValueError:
        return None
    for rx, keep, agg in _DATA_Q:
        m = rx.fullmatch(sents[1])
        if not m:
            continue
        # A quoted value that also occurs in another spelling (case, spaces) is ambiguous.
        quoted = set(re.findall(r"'([^']+)'", sents[1]))
        for col in ("product", "region"):
            vals = {r[col] for r in rows}
            for q in quoted:
                if any(v != q and v.strip().lower() == q.strip().lower() for v in vals):
                    return None
        sel = [r for r in rows if keep(r, *m.groups())]
        if not sel:
            return None  # nothing matches (absent value or period): the model decides
        return ("solved", str(agg(sel)))
    return None


# ------------------------------------------------------------------ search (fact cards)

_SEARCH_PRE = "The directory kb/ contains a small knowledge base of markdown files."
_NAME = r"([A-Z][\w'\-]+(?: [A-Z][\w'\-]+)+)"
_SEARCH_Q = [
    (re.compile(r"In which year was %s born\?" % _NAME), ["Born"]),
    (re.compile(r"In which city is the headquarters of %s's employer\?" % _NAME), ["Employer", "Headquarters"]),
    (re.compile(r"Which company does %s work for\?" % _NAME), ["Employer"]),
    (re.compile(r"Who is the employer of %s\?" % _NAME), ["Employer"]),
    (re.compile(r"In which year was %s's employer founded\?" % _NAME), ["Employer", "Founded"]),
    (re.compile(r"Which company does %s's mentor work for\?" % _NAME), ["Mentor", "Employer"]),
    (re.compile(r"In which year was %s's mentor born\?" % _NAME), ["Mentor", "Born"]),
    (re.compile(r"In which city is the headquarters of the employer of %s's mentor\?" % _NAME),
     ["Mentor", "Employer", "Headquarters"]),
]


def _cards(task):
    """{lowercased title: {field: value}} for a base made only of "# Title" + "Key: Value" cards."""
    cards = {}
    for rel, text in ctx.kb_docs(task):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines or not lines[0].startswith("# "):
            return None
        fields = {}
        for l in lines[1:]:
            m = re.fullmatch(r"([A-Z][\w ]{0,30}):\s*(.+)", l)
            if not m:
                return None  # prose or anything unusual: not a pure card base
            key = m.group(1).strip()
            if key in fields:
                return None  # the same field twice: ambiguous
            fields[key] = m.group(2).strip()
        title = lines[0][2:].strip().lower()
        if title in cards:
            return None  # two cards with one name: ambiguous
        cards[title] = fields
    return cards


# Field names that may carry the same fact under another label: such a card is left to the model.
_FIELD_KIN = {"Born": ("born", "birth"), "Employer": ("employ", "work", "company", "job"),
              "Headquarters": ("headquarter", "hq", "based", "office", "seat", "location", "city"),
              "Founded": ("found", "establish", "since", "year"), "Mentor": ("mentor", "coach", "teacher", "advisor")}
_ANY_ATTR = re.compile(r"What is the ([a-z][a-z ]{1,30}) of %s\?" % _NAME)


def _words(s):
    import unicodedata
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _maybe_same(a, b):
    """Two names that may denote one entity: the words of one all occur in the other
    (another order, a middle name, a suffix; accents ignored)."""
    wa, wb = _words(a), _words(b)
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)


def _absent(name, cards, texts, mentioned_ok=False):
    """True when nothing in a pure card base can be this entity: no card title may denote it,
    and (for the entity the question names) the name never occurs as a phrase in any document.
    A name reached through another card's field is expected to occur in that card."""
    if any(_maybe_same(name, t) for t in cards):
        return False
    if mentioned_ok:
        return True
    parts = name.lower().split()
    orders = [parts, parts[::-1]] if len(parts) == 2 else [parts]  # "Wang Alice" / "Alice Wang"
    for o in orders:
        phrase = re.compile(r"\b%s\b" % r"\W+".join(map(re.escape, o)), re.I)
        if any(phrase.search(t) for t in texts):
            return False
    return True


def _no_such_field(card, field):
    kin = _FIELD_KIN.get(field, (field.lower(),))
    return not any(any(k in key.lower() for k in kin) for key in card)


def solve_search(task):
    sents = _sentences(task.short_instruction())
    if len(sents) != 2 or sents[0] != _SEARCH_PRE:
        return None
    for rx, chain in _SEARCH_Q:
        m = rx.fullmatch(sents[1])
        if not m:
            continue
        cards = _cards(task)
        if not cards:
            return None
        texts = [t for _, t in ctx.kb_docs(task)]
        cur = m.group(1).lower()
        value = None
        for i, field in enumerate(chain):
            card = cards.get(cur)
            if card is None:
                if _absent(cur, cards, texts, mentioned_ok=i > 0):
                    # A pure card base holds an entity's facts only in its own card.
                    return ("infeasible", "no card or mention of %s" % (m.group(1) if i == 0 else value))
                return None  # a similar name exists (alias, other order): the model takes a look
            if field not in card:
                if _no_such_field(card, field):
                    return ("infeasible", "the card of %s has no %s" % (m.group(1) if i == 0 else value, field))
                return None
            value = card[field]
            if field in ("Born", "Founded"):
                if not re.fullmatch(r"\d{4}", value):
                    return None  # "12 March 1983", "1983 (Prague)": the model extracts the year
            elif not re.fullmatch(r"[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*", value):
                return None  # punctuation, brackets, "X in Y": not a plain name
            cur = value.lower()
        return ("solved", value)
    # "What is the email address of X?": an attribute that no card of the base has at all.
    m = _ANY_ATTR.fullmatch(sents[1])
    if m:
        cards = _cards(task)
        if not cards or m.group(2).lower() not in cards:
            return None
        attr = _words(m.group(1)) - {"the", "of", "a", "an", "his", "her"}
        if attr and not any(attr & _words(k) for c in cards.values() for k in c):
            return ("infeasible", "no card has a field for %s" % m.group(1))
    return None


def solve(task, kind):
    try:
        if kind == "math":
            return solve_math(task)
        if kind == "data":
            return solve_data(task)
        if kind == "search":
            return solve_search(task)
    except Exception:
        return None
    return None

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

_SENT = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9])")


def _sentences(text):
    return [s.strip() for s in _SENT.split(text.strip()) if s.strip()]


# ------------------------------------------------------------------ math (warehouse stock)

_START = re.compile(r"A warehouse starts with (\d+|some) boxes\.")
_QUESTION = re.compile(r"How many boxes does it have at the end\?")
_OPS = [
    (re.compile(r"(?:Then it|It) ships (\d+) box(?:es)? per day for (\d+) days?\."), lambda x, n, d: x - n * d),
    (re.compile(r"(?:Then it|It) receives (\d+) deliver(?:y|ies) of (\d+) box(?:es)? each\."), lambda x, k, m: x + k * m),
    (re.compile(r"(?:Then the stock is|The stock is then) split equally into (\d+) warehouses? and it keeps one "
                r"share \(rounded down\)\."), lambda x, w: x // w),
    (re.compile(r"Then the stock is doubled by merging with a sister warehouse\."), lambda x: 2 * x),
    (re.compile(r"Then the stock is tripled by merging with two sister warehouses(?: of the same size)?\."),
     lambda x: 3 * x),
    (re.compile(r"Then (\d+) percent of the stock is lost to damage \(the number of lost boxes is rounded down\)\."),
     lambda x, p: x - (x * p) // 100),
    (re.compile(r"Then the stock grows by (\d+) percent \(rounded down\)\."), lambda x, p: x + (x * p) // 100),
    (re.compile(r"Then it sells half of the stock \(the number of boxes sold is rounded up\)\."),
     lambda x: x - (x + 1) // 2),
    (re.compile(r"(?:Then it|It) sends (\d+) box(?:es)? to each of (\d+) stores?\."), lambda x, s, n: x - s * n),
    (re.compile(r"Customers return (\d+) box(?:es)?, which (?:are|is) added back to the stock\."), lambda x, r: x + r),
    (re.compile(r"(?:Then it|It) ships (\d+) box(?:es)?\."), lambda x, n: x - n),
    (re.compile(r"(?:Then it|It) ships (\d+) box(?:es)? and then receives (\d+) box(?:es)?\."), lambda x, a, b: x - a + b),
    (re.compile(r"(?:Then it|It) receives (\d+) deliver(?:y|ies) of (\d+) box(?:es)? each, but (\d+) box(?:es)? in each delivery (?:are|is) "
                r"damaged and thrown away\."), lambda x, k, m, d: x + k * (m - d) if d <= m else None),
    (re.compile(r"Then it packs the stock into crates of (\d+) box(?:es)?, ships all full crates and keeps only the "
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
_EXACTLY = re.compile(r"After that it has exactly (\d+) box(?:es)?\.")

# Other wordings of the same statements (generated variants): a different place or item, words
# of sequence ("First,", "Next,", "Finally,"), two operations in one sentence (", and").
_START2 = re.compile(r"An? (warehouse|depot|store|shop) (?:starts with|holds|has) (\d+|some) ([a-z]+)(?: at the start)?\.")
_QUESTION2 = re.compile(r"How many ([a-z]+) does (?:it|the (warehouse|depot|store|shop)) (?:have|hold) (?:at the end|now)\?")
_SEQ = re.compile(r"^(?:First|Next|Then|After that|Afterwards|Finally|Later),?\s+", re.I)
_FRACTION = {"half": 2, "third": 3, "quarter": 4, "fifth": 5}
_NUMWORD = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
# Who the boxes go to or come from: a fixed list, so that a tail that changes the meaning ("to a
# customer who returns them", "from a supplier that recalls them") never matches.
_PARTY = (r"(?:a|an|the|its) (?:partner(?: store| warehouse| depot)?|charity|customer|client|shop|store|school|"
          r"hospital|supplier|depot|retailer|factory|sister warehouse|local [a-z]+ shop)s?")
_OPS2 = [
    (r"it ships (\d+) box(?:es)? per day for (\d+) days?", lambda x, n, d: x - n * d),
    (r"it receives (\d+) deliver(?:y|ies) of (\d+) box(?:es)? each", lambda x, k, m: x + k * m),
    (r"(\d+) trucks? arrives?, each carrying (\d+) box(?:es)?", lambda x, k, m: x + k * m),
    (r"(\d+) trucks? arrives? with (\d+) box(?:es)? each", lambda x, k, m: x + k * m),
    (r"(?:its|the) stock is doubled (?:again )?(?:by merging with (?:a|another) sister warehouse|after a merger)",
     lambda x: 2 * x),
    (r"it merges with a sister warehouse, which doubles its stock", lambda x: 2 * x),
    (r"(?:its|the) stock is tripled (?:after a merger|by merging with two sister warehouses(?: of the same size)?)",
     lambda x: 3 * x),
    (r"(?:its|the) stock is (?:split equally into|divided evenly among) (\d+) warehouses? and it keeps (?:one|a single) "
     r"share \(round(?:ed)? down\)", lambda x, w: x // w),
    (r"customers return (\d+) box(?:es)?(?:, which (?:are|is) added back to the stock)?", lambda x, r: x + r),
    (r"it sends one (half|third|quarter|fifth) of its stock \(rounded down\) to a partner (?:store|warehouse)",
     lambda x, f: x - x // _FRACTION[f]),
    (r"(\d+)% of the stock \(rounded down\) is (?:damaged by water and thrown away|lost to damage)",
     lambda x, p: x - x * int(p) // 100),
    (r"it ships (\d+) boxes", lambda x, n: x - n),
    # a plain count of boxes leaving or arriving, in any of the common verbs
    (r"it (?:gives away|donates|sells|loses|throws away|sends away|sends) (\d+) boxes"
     r"(?: to " + _PARTY + ")?", lambda x, n: x - n),
    (r"(\d+) boxes are (?:given away|donated|sold|lost|stolen|thrown away|damaged and thrown away)",
     lambda x, n: x - n),
    (r"it (?:receives|buys|gets|orders and receives) (\d+) (?:more |new |extra )?boxes"
     r"(?: from " + _PARTY + ")?", lambda x, n: x + n),
    (r"(\d+) (?:more |new |extra )?boxes (?:arrive|are delivered|are added to the stock)", lambda x, n: x + n),
    # receipts and shipments on each of several days (no rounding: their order within a day does not matter)
    (r"(?:every|each) day for (\d+) days?,? it receives (\d+) box(?:es)? and ships (\d+) box(?:es)?",
     lambda x, d, a, b: x + d * (a - b)),
    (r"for (\d+) days?,? it receives (\d+) box(?:es)? and ships (\d+) box(?:es)? (?:every|each) day",
     lambda x, d, a, b: x + d * (a - b)),
    (r"it receives (\d+) box(?:es)? and ships (\d+) box(?:es)? (?:every|each|per) day for (\d+) days?",
     lambda x, a, b, d: x + d * (a - b)),
    (r"(?:every|each) day for (\d+) days?,? (\d+) box(?:es)? arrive and (\d+) box(?:es)? (?:are shipped|leave)",
     lambda x, d, a, b: x + d * (a - b)),
    (r"it (?:dispatches|sends out) (\d+) box(?:es)?(?: per day| every day| each day) for (\d+) days?",
     lambda x, n, d: x - n * d),
    (r"it (?:dispatches|sends out) (\d+) box(?:es)?", lambda x, n: x - n),
    (r"(\d+) (?:shipments?|deliver(?:y|ies)) of (\d+) box(?:es)? (?:each )?arrives?", lambda x, k, m: x + k * m),
    (r"(\d+) shipments? arrives?, each (?:with|carrying|containing) (\d+) box(?:es)?", lambda x, k, m: x + k * m),
    (r"it returns (\d+) box(?:es)? to (?:a|the|its) (?:supplier|manufacturer|factory)", lambda x, n: x - n),
    (r"(?:its|the) stock (?:doubles|is doubled)", lambda x: 2 * x),
    (r"(?:its|the) stock (?:triples|is tripled)", lambda x: 3 * x),
    (r"it (?:sells|gives away|loses|sends away|ships) (?:a|one) (third|quarter|fifth) of (?:the|its) stock "
     r"\(rounded down\)(?: to " + _PARTY + ")?", lambda x, f: x - x // _FRACTION[f]),
    (r"(\d+) ?(?:%|percent) of (?:the|its) stock is (?:lost|damaged|stolen|thrown away|lost to damage) \(rounded down\)",
     lambda x, p: x - x * p // 100),
    (r"it (?:sells|gives away|loses|sends away) half of (?:the|its) stock \((?:the number of boxes (?:sold|given away|lost|sent away) is )?rounded "
     r"(up|down)\)", lambda x, ud: x - ((x + 1) // 2 if ud == "up" else x // 2)),
]
_VAGUE2 = [r"a flood destroys part of the stock", r"it ships \d+ boxes per day for (?:several|a few|some) days",
           r"(?:part|some) of the stock is (?:lost|damaged|stolen)",
           r"(?:several|a few|some|many) (?:deliveries|shipments|trucks) of \d+ box(?:es)? (?:each )?arrive",
           r"(?:several|a few|some|many) (?:deliveries|shipments|trucks) arrive, each (?:with|carrying|containing) \d+ "
           r"box(?:es)?",
           r"it receives (?:deliveries|shipments) of \d+ box(?:es)? each",
           # an end left vague ("until the stock is low") or a split with no number of parts
           r"it ships \d+ box(?:es)? (?:per|every|each) day until (?:the|its) stock (?:is|runs|gets) "
           r"(?:low|very low|almost empty|nearly empty)",
           r"(?:its|the) stock is (?:split equally|divided evenly) (?:among|between) (?:the|its) sister warehouses and "
           r"it keeps (?:one|a single) share(?: \(round(?:ed)? down\))?",
           r"it (?:gives away|donates|sells|loses|receives|buys|ships) (?:some|a few|several|many) (?:more )?boxes"
           r"(?: (?:to|from) " + _PARTY + ")?"]


def _variant_clauses(sentence, item, place):
    """The sentence as normalised clauses: sequence word dropped, item -> boxes, place ->
    warehouse, split at ', and '."""
    s = _SEQ.sub("", sentence.rstrip("."))
    s = re.sub(r"\b%s\b" % re.escape(item), "boxes", s)
    if item.endswith("s"):
        s = re.sub(r"\b%s\b" % re.escape(item[:-1]), "box", s)
    if place != "warehouse":
        s = re.sub(r"\b%ss\b" % place, "warehouses", s)
        s = re.sub(r"\b%s\b" % place, "warehouse", s)
    # fixed quantities written in words
    s = re.sub(r"\bfor a week\b", "for 7 days", s)
    s = re.sub(r"\bfor (?:two weeks|a fortnight)\b", "for 14 days", s)
    s = re.sub(r"\ba dozen boxes\b", "12 boxes", s)
    s = re.sub(r"\b(%s) (?=(?:more |new |extra )?(?:box|boxes|deliveries|delivery|days|day|trucks|truck|shipments|"
               r"shipment|warehouses|stores)\b)" % "|".join(_NUMWORD), lambda m: str(_NUMWORD[m.group(1).lower()]) + " ",
               s, flags=re.I)
    return [c.strip() for c in re.split(r",\s+and\s+", s) if c.strip()]


def _apply_variant(x, clause):
    """(matched, new x) for one normalised clause; x None stays None (unknown stock)."""
    for pat in _VAGUE2:
        if re.fullmatch(pat, clause, re.I):
            return True, None
    for pat, fn in _OPS2:
        m = re.fullmatch(pat, clause, re.I)
        if m:
            if x is None:
                return True, None
            args = [int(g) if g.isdigit() else g for g in m.groups()]
            return True, fn(x, *args)
    return False, x


def solve_math(task):
    """Returns ("solved", answer) / ("infeasible", note) / None."""
    sents = _sentences(task.short_instruction())
    if len(sents) < 3:
        return None
    m = _START.fullmatch(sents[0])
    item, place = "boxes", "warehouse"
    if m and _QUESTION.fullmatch(sents[-1]):
        x = None if m.group(1) == "some" else int(m.group(1))  # None: a stock the text never gives
    else:
        m2, q2 = _START2.fullmatch(sents[0]), _QUESTION2.fullmatch(sents[-1])
        if not m2 or not q2 or q2.group(1) != m2.group(3) or (q2.group(2) and q2.group(2) != m2.group(1)):
            return None
        place, item = m2.group(1), m2.group(3)
        x = None if m2.group(2) == "some" else int(m2.group(2))
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
            # Another wording: every clause of the sentence must be a known operation.
            clauses = _variant_clauses(s, item, place)
            for cl in clauses:
                try:
                    ok, x = _apply_variant(x, cl)
                except ZeroDivisionError:
                    return ("infeasible", "a division by zero (0 warehouses or crates)")
                if not ok:
                    return None  # a sentence we do not understand: the model decides
                if x is not None and x < 0:
                    return None
            if not clauses:
                return None
            continue
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
    # the same questions in other words seen in generated variants
    (re.compile(r"In total, how many units of product '([^']+)' were sold in months (\d+) through (\d+) inclusive\?"),
     lambda r, p, a, b: r["product"] == p and int(a) <= r["month"] <= int(b), lambda sel: sum(r["units"] for r in sel)),
    (re.compile(r"What is the total revenue \(units \* price\) of all orders in region '([^']+)'\?"),
     lambda r, g: r["region"] == g, lambda sel: sum(r["units"] * r["price"] for r in sel)),
    (re.compile(r"How many orders of product '([^']+)' were placed in region '([^']+)'\?"),
     lambda r, p, g: r["product"] == p and r["region"] == g, len),
    (re.compile(r"How many units were sold in region '([^']+)' in month (\d+)\?"),
     lambda r, g, mo: r["region"] == g and r["month"] == int(mo), lambda sel: sum(r["units"] for r in sel)),
    (re.compile(r"What is the average number of units per order for product '([^']+)', rounded down to an integer\?"),
     lambda r, p: r["product"] == p, lambda sel: sum(r["units"] for r in sel) // len(sel)),
    (re.compile(r"What is the average price of product '([^']+)', rounded down to an integer\?"),
     lambda r, p: r["product"] == p, lambda sel: sum(r["price"] for r in sel) // len(sel)),
    (re.compile(r"What is the average price of product '([^']+)', rounded to the nearest integer\?"),
     lambda r, p: r["product"] == p, lambda sel: _nearest(sum(r["price"] for r in sel), len(sel))),
]


def _nearest(total, n):
    """total / n to the nearest integer; None on an exact half (the rounding rule is unstated)."""
    q, rem = divmod(total, n)
    if 2 * rem == n:
        return None
    return q + (1 if 2 * rem > n else 0)
_SALES_COLS = ["order_id", "region", "product", "month", "units", "price"]


_ROUND = re.compile(r"Round the result to (\d) decimal places?\.")
_NAME_ONLY = "Answer with the product name."


def _round_half_up(value, places):
    """A Fraction rounded half up to `places` decimals as text; None on an exact half (the
    rounding rule would matter and is not stated beyond 'round')."""
    from fractions import Fraction
    q = Fraction(value) * 10 ** places
    lo = q.numerator // q.denominator
    rem = q - lo
    if rem == Fraction(1, 2):
        return None
    n = lo + (1 if rem > Fraction(1, 2) else 0)
    return "%d.%0*d" % (n // 10 ** places, places, n % 10 ** places) if places else str(n)


def _argmax_product(sel):
    tot = {}
    for r in sel:
        tot[r["product"]] = tot.get(r["product"], 0) + r["units"]
    best = sorted(tot.items(), key=lambda kv: -kv[1])
    if len(best) > 1 and best[0][1] == best[1][1]:
        return None  # a tie: no single answer
    return best[0][0]


_DATA_Q2 = [  # (question, row filter, aggregate, allowed closing instruction)
    (re.compile(r"What is the average price of product '([^']+)' over all its orders\?"),
     lambda r, p: r["product"] == p, lambda sel: __import__("fractions").Fraction(sum(r["price"] for r in sel), len(sel)),
     "round"),
    (re.compile(r"What was the total revenue \(units multiplied by price, summed over orders\) from region '([^']+)' "
                r"in month (\d+)\?"),
     lambda r, g, mo: r["region"] == g and r["month"] == int(mo), lambda sel: sum(r["units"] * r["price"] for r in sel),
     None),
    (re.compile(r"How many orders from region '([^']+)' had at least (\d+) units\?"),
     lambda r, g, n: r["region"] == g and r["units"] >= int(n), len, None),
    (re.compile(r"Which product had the highest total number of units sold in region '([^']+)'\?"),
     lambda r, g: r["region"] == g, _argmax_product, "name"),
    (re.compile(r"What is the largest number of units in a single order of product '([^']+)' in region '([^']+)'\?"),
     lambda r, p, g: r["product"] == p and r["region"] == g, lambda sel: max(r["units"] for r in sel), None),
]


def solve_data(task):
    text = task.short_instruction()
    sents = _sentences(text)
    tail = None
    if len(sents) == 3:
        rm = _ROUND.fullmatch(sents[2])
        tail = ("round", int(rm.group(1))) if rm else ("name", None) if sents[2] == _NAME_ONLY else None
        if tail is None:
            return None
        sents = sents[:2]
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
    whole_prices = True
    try:
        for r in body:
            if len(r) != 6:
                return None
            price = r[5].strip()
            if re.fullmatch(r"\d+", price):
                price = int(price)
            elif re.fullmatch(r"\d+\.\d{1,2}", price):
                whole_prices = False  # 12.50: kept, but questions about prices go to the model
                price = None
            else:
                return None
            rows.append({"order_id": r[0], "region": r[1].strip(), "product": r[2].strip(), "month": int(r[3]),
                         "units": int(r[4]), "price": price})
    except ValueError:
        return None
    for rx, keep, agg, closing in [(a, b, c, None) for a, b, c in _DATA_Q] + _DATA_Q2:
        m = rx.fullmatch(sents[1])
        if not m:
            continue
        if not whole_prices and re.search(r"price|revenue", sents[1], re.I):
            return None  # the answer format of a decimal amount is not stated: the model decides
        if (tail[0] if tail else None) != closing and not (closing == "name" and tail is None):
            return None  # a closing instruction this question does not take (or a missing one)
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
        value = agg(sel)
        if value is None:
            return None
        if closing == "round":
            if not tail:
                return None  # an average without a stated rounding: the model decides the format
            value = _round_half_up(value, tail[1])
            if value is None:
                return None
        return ("solved", str(value))
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
    (re.compile(r"Where is the headquarters of %s's employer\?" % _NAME), ["Employer", "Headquarters"]),
    (re.compile(r"In which year was %s founded\?" % _NAME), ["Founded"]),
    (re.compile(r"In which city is the headquarters of %s\?" % _NAME), ["Headquarters"]),
    (re.compile(r"In which year was the company that employs %s's mentor founded\?" % _NAME),
     ["Mentor", "Employer", "Founded"]),
    (re.compile(r"In which city is the company that employs %s's mentor headquartered\?" % _NAME),
     ["Mentor", "Employer", "Headquarters"]),
    (re.compile(r"What is the founding year of the company where %s works\?" % _NAME), ["Employer", "Founded"]),
    (re.compile(r"Who is the mentor of %s's mentor\?" % _NAME), ["Mentor", "Mentor"]),
    (re.compile(r"Who is the mentor of the mentor of %s\?" % _NAME), ["Mentor", "Mentor"]),
    (re.compile(r"Who is %s's mentor's mentor\?" % _NAME), ["Mentor", "Mentor"]),
    (re.compile(r"Who mentors %s's mentor\?" % _NAME), ["Mentor", "Mentor"]),
    (re.compile(r"Who mentored %s's mentor\?" % _NAME), ["Mentor", "Mentor"]),
    (re.compile(r"Who mentors %s\?" % _NAME), ["Mentor"]),
    (re.compile(r"Who is %s's mentor\?" % _NAME), ["Mentor"]),
    (re.compile(r"Who is the mentor of %s\?" % _NAME), ["Mentor"]),
    (re.compile(r"What is the birth year of %s\?" % _NAME), ["Born"]),
    (re.compile(r"In what year was %s born\?" % _NAME), ["Born"]),
    (re.compile(r"(?:What|Which) year was %s born(?: in)?\?" % _NAME), ["Born"]),
    (re.compile(r"In what year was %s founded\?" % _NAME), ["Founded"]),
    (re.compile(r"What is the founding year of %s\?" % _NAME), ["Founded"]),
    (re.compile(r"Where is %s headquartered\?" % _NAME), ["Headquarters"]),
    (re.compile(r"Where is the headquarters of %s\?" % _NAME), ["Headquarters"]),
    (re.compile(r"Who employs %s\?" % _NAME), ["Employer"]),
    (re.compile(r"For which company does %s work\?" % _NAME), ["Employer"]),
    # two sentences: "X's mentor works for a company. In which city is that company headquartered?"
    (re.compile(r"%s's mentor works for a company\. In which city is that company headquartered\?" % _NAME),
     ["Mentor", "Employer", "Headquarters"]),
    (re.compile(r"%s's mentor works for a company\. In which year was that company founded\?" % _NAME),
     ["Mentor", "Employer", "Founded"]),
]
_OLDER = re.compile(r"How many years older is %s than %s\?" % (_NAME, _NAME))
_BEFORE = re.compile(r"Was %s born before (\d{4})\?" % _NAME)
_STAFF = re.compile(r"How many people work for %s\?" % _NAME)


# Card bodies written as sentences instead of "Key: Value" lines; {T} is the card's title.
_CARD_SENT = [
    (r"Year of birth: (\d{4})", "Born"),
    (r"{T} was born in (\d{4})", "Born"),
    (r"{T} joined (.+?) as an? [a-z][a-z ]*", "Employer"),
    (r"{T} works (?:at|for) (.+?)", "Employer"),
    (r"Mentor at work: (.+?)", "Mentor"),
    (r"(.+?) is {T}'s mentor", "Mentor"),
    (r"{T}'s mentor is (.+?)", "Mentor"),
    (r"{T} was founded in (\d{4})", "Founded"),
    (r"The company is headquartered in (.+?)", "Headquarters"),
    (r"{T} is headquartered in (.+?)", "Headquarters"),
    (r"Born in (\d{4}), {T} is an? [a-z][a-z ]* at (.+?)", ("Born", "Employer")),
    (r"{T} \(born (\d{4})\) is employed by (.+?)", ("Born", "Employer")),
    (r"{T} is employed by (.+?)", "Employer"),
    (r"{T} is mentored by (.+?)", "Mentor"),
    (r"(.+?) has been the mentor of {T} for (?:several|many|a few|two|three|four|five|\d+) years", "Mentor"),
    (r"(.+?) has been the mentor of {T}", "Mentor"),
    (r"Headquartered in (.+?), {T} has been operating since it was founded in (\d{4})", ("Headquarters", "Founded")),
    (r"Founded in (\d{4}), {T} is headquartered in (.+?)", ("Founded", "Headquarters")),
]


def _sentence_fields(title, body):
    """{field: value} if every sentence of a card body is one of the known forms, else None."""
    fields = {}
    text = " ".join(body)
    parts = [p.strip() for p in re.split(r"(?<=\.)\s+|,\s+and\s+", text) if p.strip()]
    for part in parts:
        part = part.rstrip(".").strip()
        for pat, key in _CARD_SENT:
            m = re.fullmatch(pat.replace("{T}", re.escape(title)), part)
            if m:
                for k, v in zip(key if isinstance(key, tuple) else (key,), m.groups()):
                    if k in fields:
                        return None  # stated twice: ambiguous
                    fields[k] = v.strip()
                break
        else:
            return None
    return fields


def _cards(task, docs=None):
    """{lowercased title: {field: value}} for a base made only of "# Title" + "Key: Value" cards."""
    cards = {}
    for rel, text in (ctx.kb_docs(task) if docs is None else docs):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines or not lines[0].startswith("# "):
            return None
        fields = {}
        prose = []
        for l in lines[1:]:
            m = re.fullmatch(r"([A-Z][\w ]{0,30}):\s*([^.]+?)\.?", l)
            if m and not re.search(r"\.\s+[A-Z]", l):  # one "Key: Value" line, not a sentence run-on
                key = {"Year of birth": "Born", "Mentor at work": "Mentor"}.get(m.group(1).strip(), m.group(1).strip())
                if key in fields:
                    return None  # the same field twice: ambiguous
                fields[key] = m.group(2).strip()
            else:
                prose.append(l)
        if prose:
            more = _sentence_fields(lines[0][2:].strip(), prose)
            if more is None or set(more) & set(fields):
                return None  # prose beyond the known card sentences, or a fact stated twice
            fields.update(more)
        title = lines[0][2:].strip().lower()
        if title in cards:
            # Two cards under one name: a field they both give differently is contradictory
            # (asked for, the task is infeasible); a field given once or alike stays usable.
            merged = dict(cards[title])
            for k, v in fields.items():
                if k in merged and merged[k].strip().lower() != v.strip().lower():
                    merged[k] = _CONFLICT
                elif k not in merged:
                    merged[k] = v
            cards[title] = merged
            continue
        cards[title] = fields
    return cards


_CONFLICT = "\x00conflict"  # a field two cards of one name give differently


# Field names that may carry the same fact under another label: such a card is left to the model.
_FIELD_KIN = {"Born": ("born", "birth"), "Employer": ("employ", "work", "company", "job"),
              "Headquarters": ("headquarter", "hq", "based", "office", "seat", "location", "city"),
              "Founded": ("found", "establish", "since", "year"), "Mentor": ("mentor", "coach", "teacher", "advisor")}
# Questions about facts outside the card schema (birthplace, job title, founder, start date,
# chief executive, education), each with the words a card or a sentence would carry if the base
# did state it. Infeasible only for a fully parsed card base whose entity has a card and in which
# none of these words occurs anywhere.
_OFF_SCHEMA = [
    (r"In which city was %s born\?", ("birthplace", "place of birth", "born in", "hometown", "city of birth", "native")),
    (r"Where was %s born\?", ("birthplace", "place of birth", "born in", "hometown", "city of birth", "native")),
    (r"What is %s's job title\?", ("title", "position", "role", "job", "rank")),
    (r"What position does %s hold\?", ("title", "position", "role", "job", "rank")),
    (r"Who founded %s\?", ("founder", "founded by", "co-founder", "owner", "established by", "started by")),
    (r"Who is the founder of %s\?", ("founder", "founded by", "co-founder", "owner", "established by", "started by")),
    (r"In which year did %s join (?:his|her|their|the) (?:company|employer)\?", ("joined", "since", "hired", "started", "join")),
    (r"Who is the CEO of %s\?", ("ceo", "chief", "director", "head of", "headed", "president", "manager", "leader", "runs", "led by")),
    (r"Which university did %s graduate from\?", ("university", "college", "school", "graduat", "degree", "studied", "education", "alma")),
]


def _off_schema(task, sent):
    for pat, kin in _OFF_SCHEMA:
        m = re.fullmatch(pat % _NAME, sent)
        if not m:
            continue
        cards = _cards(task)
        if not cards or m.group(1).lower() not in cards:
            return None  # not a parsed card base, or no card for the entity: the model looks
        # Every value must be bare (a year, a plain name, a one-word role): "Born: 1980 in Lima",
        # "Founded: 1999 by Ann Lee", "Employer: X (since 2015)" may carry the asked fact.
        bare = re.compile(r"\d{4}|[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*|[a-z]+")
        if any(v == _CONFLICT or not bare.fullmatch(v.strip()) for c in cards.values() for v in c.values()):
            return None
        texts = " ".join(t.lower() for _, t in ctx.kb_docs(task))
        keys = " ".join(k.lower() for c in cards.values() for k in c)
        if any(w in texts or w in keys for w in kin):
            return None
        return ("infeasible", "the base states nothing like %s" % kin[0])
    return None


# Only attributes a fact-card base never carries (contact details, pay) can be declared absent;
# any other wording ("the birth year of X") is a paraphrase of a known field and goes to the model.
_NO_CARD_ATTRS = ("email address", "email", "e-mail address", "phone number", "telephone number",
                  "mobile number", "salary", "home address", "postal address")
_ANY_ATTR = re.compile(r"What is the (%s) of %s\?" % ("|".join(re.escape(a) for a in _NO_CARD_ATTRS), _NAME))


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


def _only_referenced(name, cards, texts):
    """True when a name without a card of its own occurs in the base only as the exact value of
    fields of other cards ("Mentor: X", "Z is X's mentor"): nothing about it is stated."""
    refs = sum(1 for f in cards.values() for v in f.values() if v != _CONFLICT and v.strip().lower() == name.lower())
    if not refs:
        return False
    phrase = re.compile(r"\b%s\b" % r"\W+".join(map(re.escape, name.split())), re.I)
    mentions = sum(len(phrase.findall(t)) for t in texts)
    return mentions == refs


def _no_such_field(card, field):
    kin = _FIELD_KIN.get(field, (field.lower(),))
    return not any(any(k in key.lower() for k in kin) for key in card)


_BARE = re.compile(r"\d{4}|[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*|[a-z]+")


def _bare_base(cards, kin=()):
    """True when no card value can hide a fact: every value is bare (a year, a plain name, one
    lowercase word) and no lowercase word is one of the asked field ("Cobalt Works" is a name). "Note: Dana Kim is mentored by
    Carla Diaz", "Details: born in 1990 in Lima", "Role: engineer at Delta Foods" state facts
    outside their field, so absence or a count over the fields proves nothing."""
    for c in cards.values():
        for v in c.values():
            if v == _CONFLICT:
                return False  # the two values that differ are no longer seen: either may be a sentence
            v = v.strip()
            if not _BARE.fullmatch(v) or v.islower() and any(k in v for k in kin):  # "Role: mentor"
                return False
    return True


def _walk(task, name, chain):
    """Follow the fields `chain` from the card `name`; ("solved", value) / ("infeasible", why) /
    None (anything unclear: the model looks)."""
    if "^Mentor" in chain:
        return _walk_inverse(task, name, chain)
    cards = _cards(task)
    if not cards:
        return None
    texts = [t for _, t in ctx.kb_docs(task)]
    cur = name.lower()
    value = None
    for i, field in enumerate(chain):
        card = cards.get(cur)
        if card is None or field not in card:
            # Any free verdict below needs a base whose values cannot state the missing fact.
            kin = _FIELD_KIN.get(field, (field.lower(),))
            if not _bare_base(cards, kin):
                return None
        if card is None:
            if i == 0 and not any(_maybe_same(cur, t) for t in cards) and _only_referenced(name, cards, texts):
                return ("infeasible", "%s has no card; the base only names it in other cards" % name)
            if _absent(cur, cards, texts, mentioned_ok=i > 0):
                # A pure card base holds an entity's facts only in its own card.
                return ("infeasible", "no card or mention of %s" % (name if i == 0 else value))
            return None  # a similar name exists (alias, other order): the model takes a look
        if field not in card:
            if _no_such_field(card, field):
                return ("infeasible", "the card of %s has no %s" % (name if i == 0 else value, field))
            return None
        value = card[field]
        if value == _CONFLICT:
            return ("infeasible", "two cards named %s disagree on %s" % (name if i == 0 else cur, field))
        if field in ("Born", "Founded"):
            if not re.fullmatch(r"\d{4}", value):
                return None  # "12 March 1983", "1983 (Prague)": the model extracts the year
        elif not re.fullmatch(r"[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*", value):
            return None  # punctuation, brackets, "X in Y": not a plain name
        cur = value.lower()
    return ("solved", value)


def _inverse_base(task):
    """A complete, bounded snapshot for inverse queries, where uniqueness is global.

    The ordinary retrieval reader may truncate files. An unseen second mentee must
    never turn into a supposedly unique answer. Stay within a small markdown-only
    workspace and decode strictly; anything outside this profile goes to the model.
    """
    # Unlike general retrieval, do not silently skip unreadable/symlinked subtrees.
    def unreadable(error):
        raise error

    paths, directories = [], 0
    try:
        for root, dirs, files in os.walk(task.cwd, onerror=unreadable):
            directories += 1
            if directories > 1000 or any(os.path.islink(os.path.join(root, d)) for d in dirs):
                return None
            for filename in sorted(files):
                if filename == "answer.txt" or filename.startswith(".answer.txt"):
                    continue  # harness output is not a KB document
                path = os.path.join(root, filename)
                if os.path.islink(path) or not os.path.isfile(path):
                    return None
                paths.append(path)
                if len(paths) > 1000:
                    return None
    except OSError:
        return None
    if not paths:
        return None
    docs, titles, size = [], {}, 0
    for path in paths:
        if os.path.splitext(path)[1].lower() not in (".md", ".markdown"):
            return None
        try:
            with open(path, "rb") as stream:
                raw = stream.read(65537)
            size += len(raw)
            if len(raw) > 65536 or size > 2 * 1024 * 1024:
                return None
            text = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            return None
        docs.append((os.path.relpath(path, task.cwd), text))
        title = text.splitlines()[0][2:].strip() if text.startswith("# ") else ""
        if not re.fullmatch(r"[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*", title):
            return None
        titles.setdefault(title.lower(), title)
    cards = _cards(task, docs)
    if not cards:
        return None
    for card in cards.values():
        # Unknown labels or contradictions might conceal another matching edge.
        if set(card) - {"Born", "Employer", "Mentor", "Founded", "Headquarters"}:
            return None
        for field, value in card.items():
            pattern = (r"\d{4}" if field in ("Born", "Founded") else
                       _NAME if field == "Mentor" else r"[A-Z][\w'\-]*(?: [A-Z&][\w'\-]*)*")
            if value == _CONFLICT or not re.fullmatch(pattern, value):
                return None
    return cards, titles


def _walk_inverse(task, name, chain):
    """Unique inverse Mentor edges mixed with forward edges; never a free refusal."""
    base = _inverse_base(task)
    if base is None:
        return None
    cards, titles = base
    cur, value = name.lower(), name
    for field in chain:
        if any(title != cur and _maybe_same(cur, title) for title in cards):
            return None  # a possible alias makes identity uncertain
        if field == "^Mentor":
            matches = []
            for title, card in cards.items():
                mentor = card.get("Mentor", "").lower()
                if mentor == cur:
                    matches.append(title)
                elif mentor and _maybe_same(mentor, cur):
                    return None  # do not silently ignore a possible second match
            if len(matches) != 1:
                return None
            value = titles[matches[0]]
        else:
            value = cards.get(cur, {}).get(field)
            if value is None:
                return None
        cur = value.lower()
    return ("solved", value)


# Relation chains of any length, parsed as a whole ("the mentor of X's mentor", "the company that
# employs the mentor of X"): a person chain is NAME, "P's mentor", "the mentor of P" or
# "the person mentored by P". A company chain is "P's employer", "the employer of P",
# "the company that employs P", "the company P works for" or a company NAME.
# Anything else does not parse and goes to the model.
def _person_chain(s):
    s = s.strip()
    if re.fullmatch(_NAME, s):
        return s, []
    # Prefixes bind the entire expression to their right: "the person mentored
    # by Alice Wang's mentor" means inverse(forward(Alice)), not forward(inverse(Alice)).
    for prefix, edge in (("the mentor of ", "Mentor"), ("the person mentored by ", "^Mentor"),
                         ("the mentee of ", "^Mentor")):
        if s.startswith(prefix):
            r = _person_chain(s[len(prefix):])
            return (r[0], r[1] + [edge]) if r else None
    for suffix, edge in (("'s mentor", "Mentor"), ("'s mentee", "^Mentor")):
        if s.endswith(suffix):
            r = _person_chain(s[:-len(suffix)])
            return (r[0], r[1] + [edge]) if r else None
    return None


def _company_chain(s):
    s = s.strip()
    for pre, post in (("", "'s employer"), ("the employer of ", ""), ("the company that employs ", ""),
                      ("the company ", " works for"), ("the company where ", " works")):
        if s.startswith(pre) and s.endswith(post) and len(s) > len(pre) + len(post):
            r = _person_chain(s[len(pre):len(s) - len(post)])
            if r:
                return r[0], r[1] + ["Employer"]
    if re.fullmatch(_NAME, s):
        return s, []
    return None


_CHAIN_Q = [
    (r"Who is mentored by (.+)\?", _person_chain, "^Mentor"),
    (r"Who is (the person mentored by .+)\?", _person_chain, None),
    (r"Who is (the mentee of .+)\?", _person_chain, None),
    (r"Who is (.+'s mentee)\?", _person_chain, None),
    (r"Who is (.+'s mentor)\?", _person_chain, None),
    (r"In which year was (.+) born\?", _person_chain, "Born"),
    (r"What is the birth year of (.+)\?", _person_chain, "Born"),
    (r"In what year was (.+) born\?", _person_chain, "Born"),
    (r"Which company employs (.+)\?", _person_chain, "Employer"),
    (r"Which company does (.+) work for\?", _person_chain, "Employer"),
    (r"Who is the employer of (.+)\?", _person_chain, "Employer"),
    (r"Who is the mentor of (.+)\?", _person_chain, "Mentor"),
    (r"Who mentors (.+)\?", _person_chain, "Mentor"),
    (r"In which year was (.+) founded\?", _company_chain, "Founded"),
    (r"What is the founding year of (.+)\?", _company_chain, "Founded"),
    (r"In which city is the headquarters of (.+)\?", _company_chain, "Headquarters"),
    (r"Where is the headquarters of (.+)\?", _company_chain, "Headquarters"),
    (r"In which city is (.+) headquartered\?", _company_chain, "Headquarters"),
    (r"Where is (.+) headquartered\?", _company_chain, "Headquarters"),
]


def _parse_question(q):
    for pat, parse, field in _CHAIN_Q:
        m = re.fullmatch(pat, q)
        if m:
            r = parse(m.group(1))
            if r:
                return r[0], r[1] + ([field] if field else [])
    return None


def solve_search(task):
    sents = _sentences(task.short_instruction())
    if len(sents) not in (2, 3) or sents[0] != _SEARCH_PRE:
        return None
    sents = [sents[0], " ".join(sents[1:])]  # a question told in two sentences stays one unit
    for rx, chain in _SEARCH_Q:
        m = rx.fullmatch(sents[1])
        if m:
            return _walk(task, m.group(1), chain)
    parsed = _parse_question(sents[1])
    if parsed:
        return _walk(task, *parsed)
    for rx in (_OLDER, _BEFORE, _STAFF):
        m = rx.fullmatch(sents[1])
        if m:
            return _search_special(task, rx, m)
    off = _off_schema(task, sents[1])
    if off is not None:
        return off
    # "What is the email address of X?": an attribute that no card of the base has at all.
    m = _ANY_ATTR.fullmatch(sents[1])
    if m:
        cards = _cards(task)
        if not cards or m.group(2).lower() not in cards:
            return None
        attr = _words(m.group(1)) - {"the", "of", "a", "an", "his", "her"}
        if attr and not any(attr & _words(k) for c in cards.values() for k in c) and _bare_base(cards, tuple(attr)):
            return ("infeasible", "no card has a field for %s" % m.group(1))
    return None


def _born(cards, name):
    card = cards.get(name.lower())
    v = card.get("Born") if card else None
    return int(v) if v and re.fullmatch(r"\d{4}", v) else None


def _search_special(task, rx, m):
    """Year arithmetic and head counts over a pure card base; anything unclear -> None."""
    cards = _cards(task)
    if not cards:
        return None
    if rx is _OLDER:
        a, b = _born(cards, m.group(1)), _born(cards, m.group(2))
        if a is None or b is None or b - a <= 0:
            return None
        return ("solved", str(b - a))
    if rx is _BEFORE:
        a = _born(cards, m.group(1))
        if a is None:
            return None
        return ("solved", "yes" if a < int(m.group(2)) else "no")
    if rx is _STAFF:
        company = m.group(1)
        if company.lower() not in cards or not _bare_base(cards, _FIELD_KIN["Employer"]):
            return None  # "Note: joined Delta Foods in 2020" on a card without Employer would be missed
        n = 0
        for title, f in cards.items():
            emp = f.get("Employer")
            if emp is None:
                continue
            if emp.lower() == company.lower():
                n += 1
            elif _maybe_same(emp, company):
                return None  # the company written another way: the model counts
        return ("solved", str(n)) if n else None
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

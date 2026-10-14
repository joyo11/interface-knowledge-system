"""Answer rate questions from tables: pick the right product, the right tier, and only the current copy.

Rules, in order:
  1. Product: the table (title + headings + rows) must cover the question's product words.
  2. Tier: rows whose term / model year / balance / jumbo / refinance qualifiers contradict the question
     are dropped. Qualifiers are different facts, never conflicts.
  3. Currency: tables that answer the same thing compete; the copy with the newest "Rates are effective
     as of" date wins, then the more authoritative page (rate sheet > product page > promo > calculator).
     Losing copies are never quoted; they are reported by conflicts().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .qparse import Query, Range, money_value
from .search import tokens
from .store import Store, Unit

GENERIC = set(tokens(
    "rate apr apy interest dividend yield annual percentage month year term earn pay pays paying charge offer much "
    "highest lowest best standard balance between tier right now provide like know see account still available "
    "offered anymore today currently finance financing borrow borrowing get getting would qualify option options "
    "brand new used list all every each top highest lowest need needed think thinking want looking plan planning avoid show "
    "open opening ago last previous back then earlier end beginning start mid middle early late "
    "january february march april may june july august september october november december"))

# Product words that the site writes differently in different places.
ALIASES = {k: set(tokens(v)) for k, v in {
    "mortgage": "mortgage home conventional", "home": "home mortgage", "auto": "auto vehicle motorcycle",
    "card": "card visa", "credit": "credit visa",
    "saving": "saving savings", "checking": "checking", "boat": "boat rv", "rv": "rv boat",
    "share": "share certificate saving",
    "certificate": "certificate ira",      # "IRA certificate": IRA tables answer certificate questions   # credit-union jargon: "share certificate" = CD, "share savings"
}.items()}
ALIASES = {tokens(k)[0]: v for k, v in ALIASES.items() if tokens(k)}
for _k in ("vehicle", "motorcycle", "truck", "suv"):
    ALIASES[tokens(_k)[0]] = set(tokens("auto vehicle motorcycle"))
for _k in ("visa",):
    ALIASES[tokens(_k)[0]] = set(tokens("visa card credit"))

ROW_TERM_RE = re.compile(r"\b(\d{1,3})\s*[- ]?\s*(months?|mo\b|years?|yr)", re.I)
ROW_MODEL_RE = re.compile(r"(?:model year\s*)?((?:19|20)\d\d)\s*-\s*((?:19|20)?\d\d)\b|((?:19|20)\d\d)\s*model year\s*\(and older\)",
                          re.I)
ROW_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


@dataclass
class RowHit:
    unit: Unit
    row: list[str]
    label: str
    score: float


@dataclass
class Group:
    """A set of tables that answer the same thing; `winner` is the current copy."""
    winner: Unit
    rows: list[RowHit]
    losers: list[tuple[Unit, list[RowHit]]] = field(default_factory=list)
    coverage: float = 0.0
    alt_sources: list[Unit] = field(default_factory=list)


def _label_cells(header: list[str], row: list[str]) -> list[str]:
    """Cells that describe the row (product, tier, term) rather than holding a rate."""
    out = []
    for i, c in enumerate(row):
        h = header[i].lower() if i < len(header) else ""
        if re.search(r"rate|apr|apy|yield|dividend|points|payment|min\.|minimum", h) and not re.search(
                r"program|type|term|tier|balance|product|certificate|loan amount", h):
            continue
        if re.fullmatch(r"[\d.,%$\s\-]+(apr|apy)?", c.lower()) and not re.search(r"\$", c):
            continue
        out.append(c)
    return out


def _row_term_months(text: str) -> Optional[int]:
    m = ROW_TERM_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    return n * 12 if m.group(2).lower().startswith("y") else n


def _row_model_range(text: str) -> Optional[Range]:
    m = ROW_MODEL_RE.search(text)
    if not m:
        return None
    if m.group(3):
        return Range(0, int(m.group(3)))
    a = int(m.group(1))
    b = m.group(2)
    b = int(b) if len(b) == 4 else int(str(a)[:2] + b)
    return Range(min(a, b), max(a, b))


def _row_amount_range(text: str) -> Optional[Range]:
    t = text.lower().replace("–", "-")
    vals = [money_value(n, None) for n in ROW_MONEY_RE.findall(t)]
    if not vals:
        # "$10,000-24,999" style: second number lacks a $
        return None
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*-\s*\$?\s?([\d,]+(?:\.\d+)?)", t)
    if m:
        return Range(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
    if re.search(r"\+|or more|and (up|over|above)|over", t):
        return Range(vals[0], float("inf"))
    if re.search(r"or less|and under|up to|under", t):
        return Range(0, vals[0])
    return None


def _has(text: str, word: str) -> bool:
    return re.search(rf"\b{word}", text, re.I) is not None


def row_ok(q: Query, table_ctx: str, label: str) -> Optional[float]:
    """None = row contradicts the question; otherwise a small bonus for each qualifier it satisfies."""
    bonus = 0.0
    full = f"{table_ctx} {label}"
    if q.term_months is not None or len(q.terms) > 1:
        rt = _row_term_months(label)
        wanted = {q.term_months} if q.term_months is not None else q.terms
        if rt is not None:
            if rt not in wanted:
                return None
            bonus += 2
    if q.model_year is not None:
        rr = _row_model_range(label)
        if rr is not None:
            if not rr.contains(q.model_year):
                return None
            bonus += 2
    if q.amount is not None:
        ar = _row_amount_range(label)
        if ar is not None:
            if not (ar.contains(q.amount.lo) and (q.amount.hi == q.amount.lo or ar.contains(q.amount.hi - 0.01) or ar.hi == float("inf"))):
                return None
            bonus += 2
    # Qualifiers named in the row but absent from the question are a different product.
    for word, flag in (("jumbo", "jumbo"), ("refinanc", "refinance"), ("roth", "roth"), ("traditional", "traditional"),
                       ("introductory", "intro"), ("business", "business"), ("mini-max", "minimax")):
        in_row = _has(label, word)
        if flag == "jumbo" and q.list_intent and re.search(r"\b(all|every|list)\b", q.raw.lower()):
            continue    # "all special certificate rates" includes the jumbo tier
        asked = flag in q.flags or (flag == "intro" and ("introductory" in q.flags or "balance transfer" in q.flags))
        if flag in ("roth", "traditional") and "ira" in q.flags and not ({"roth", "traditional"} & q.flags):
            asked = True    # "IRA certificate" covers both the Roth and the Traditional row
        if flag == "business" and q.list_intent:
            asked = True   # "what cards do you offer" covers business cards too
        if in_row and not asked:
            return None
        if asked and in_row:
            bonus += 1.5
    for flag in ("jumbo", "refinance", "roth", "traditional"):
        if flag in q.flags and not _has(full, flag[:8]):
            return None
    if "purchase" not in q.flags and "refinance" not in q.flags and _has(label, "refinanc"):
        return None
    return bonus


# Product types a member may ask about that a site may simply not publish a rate for. If one is named and the
# table doesn't mention it, the table is about something else (a 7/1 ARM question must not get the 30-year rate).
PRODUCT_TYPES = set(tokens(
    "adjustable arm construction student land lot commercial sba fha va usda reverse jumbo heloc equity boat rv "
    "motorcycle atv snowmobile jet ski business ira roth traditional hsa health money market checking certificate "
    "visa card personal auto mortgage refinance improvement launchpad bright holiday loan"))


TABLE_QUALIFIERS = [(tokens(w)[0], tokens(trig)) for w, trig in (
    ("equity", "equity heloc"), ("improvement", "improvement remodel renovation"))]


def _wanted(q: Query, store: Store) -> list[str]:
    """Question words that can discriminate between rate tables: words that occur in some rate table (or name a
    product type). Filler like 'going', 'mid', 'these days' never blocks a match."""
    vocab = store.table_vocab
    return [t for t in q.toks if t not in GENERIC and not t.isdigit() and not re.fullmatch(r"\d+(k|m)?", t)
            and (t in vocab or t in PRODUCT_TYPES or t in ALIASES)]


def _coverage(q: Query, text_toks: set[str], store: Store) -> float:
    want = _wanted(q, store)
    if not want:
        return 0.0
    idf = store.index.idf
    tot = sum(idf.get(t, 1.0) for t in want)
    got = sum(idf.get(t, 1.0) for t in want if t in text_toks or ALIASES.get(t, set()) & text_toks)
    return got / tot if tot else 0.0


def _shapes(label: str, heading: str) -> set[str]:
    """Ways two rows can be recognised as the same tier: raw shape, shape without the product name, and the
    tier boundaries themselves ($ amounts / terms / model years)."""
    out = {_shape(label), _shape(label, heading)}
    sig = re.findall(r"\$\s?[\d,]+(?:\.\d+)?\+?|\b\d{1,3}\s*-?\s*(?:month|year|mo)\b|\b(?:19|20)\d\d\b", label.lower())
    if sig:
        out.add("sig:" + "|".join(x.replace(" ", "").replace(",", "").replace(".00", "") for x in sig))
    return {x for x in out if x}


def _shape(label: str, heading: str = "") -> str:
    """Row structure without the numbers or the product name: '1st Rate Checking $100,000+' and '$100,000+'
    under a '1st Rate Checking' heading are the same row."""
    words = set(re.findall(r"[a-z]+", heading.lower())) - {"month", "months", "year", "years", "model"}
    s = re.sub(r"\d+(st|nd|rd|th)?", "#", label.lower())
    s = " ".join(w for w in re.findall(r"[a-z#$+]+", s) if w not in words)
    return s.strip()


def candidate_groups(store: Store, view, q: Query, top: int = 100) -> list[Group]:
    qt = list(q.toks)
    for t in q.toks:
        qt += sorted(ALIASES.get(t, ()))
    hits = store.index.search(qt, top=top, allow=store.table_units(view))
    tables = [store.units[i] for _, i in hits if store.units[i].kind == "table"]
    scored = []
    for u in tables:
        t = u.table
        if t.ends and t.ends < view.taken_on:
            continue            # an expired promotion is never a current answer
        # Only rate tables: fee schedules and forms also contain "%" but are not rate sheets.
        if not re.search(r"rate|apr|apy|yield|dividend", " ".join(t.header) + " " + u.heading + (t.rows[0][0] if t.prose else ""), re.I):
            continue
        ctx = f"{u.title} {u.heading}"
        text_toks = set(tokens(f"{ctx} {' '.join(t.header)} {' '.join(' '.join(r) for r in t.rows)}"))
        named = [w for w in q.toks if w in PRODUCT_TYPES]
        # Product types must be named by the table itself (heading, header, rows), not the page title:
        # a "Business Loan & Deposit Rates" page also holds savings tables.
        own = set(tokens(f"{u.heading} {' '.join(t.header)} {' '.join(' '.join(r) for r in t.rows)}"))
        if any(w not in own and not (ALIASES.get(w, set()) & own) for w in named):
            continue
        # Table-level qualifiers: home-equity / home-improvement tables only answer questions that ask for them.
        if any(w in tokens(ctx) and not (set(trig) & set(q.toks)) for w, trig in TABLE_QUALIFIERS):
            continue
        cov = _coverage(q, text_toks, store)
        if cov < 0.5:
            continue
        rows = []
        for r in t.rows:
            label = " ".join(_label_cells(t.header, r))
            if not label or not any("%" in c for c in r):
                continue
            if (q.term_months is not None or len(q.terms) > 1) and _row_term_months(label) is None:
                continue    # the member named a term; a row without one (savings tier, relief program) can't answer
            b = row_ok(q, ctx, label)
            if b is None:
                continue
            row_toks = set(tokens(label))
            rows.append(RowHit(u, r, label, b + _coverage(q, row_toks | set(tokens(u.heading)), store)))
        if rows:
            scored.append((cov, u, rows))
    if not scored:
        return []
    if ("intro" in q.flags or "introductory" in q.flags or re.search(r"\bpromo(tional)?\b", q.raw.lower())):
        promo = [x for x in scored if re.search(r"\bintro|promo", x[1].heading + " " + " ".join(x[1].table.header) + " " +
                                                " ".join(r.label for r in x[2]), re.I)]
        if promo:
            scored = promo      # the member asked for the introductory/promotional rate
    fresh = [x for x in scored if x[1].uid not in view.carried]
    if fresh:
        scored = fresh          # pages missing from this download only answer when nothing current does
    # Personal questions are answered from personal pages; business rate sheets only when asked.
    if "business" not in q.flags and not q.list_intent:
        personal = [x for x in scored if "business" not in x[1].url]
        if personal:
            scored = personal

    # Tables compete when the rows they offer have the same shape ("# month fixed special").
    groups: list[Group] = []
    for cov, u, rows in sorted(scored, key=lambda x: (-x[0], _neg(_rank(x[1])))):
        strong = {x for x in (_shape(r.label) for r in rows) if re.search(r"[a-z]{3}", x)}
        weak = set().union(*(_shapes(r.label, u.heading) for r in rows))
        for g in groups:
            g_rows = list(g.rows) + [r for _, rr in g.losers for r in rr]
            g_strong = {x for x in (_shape(r.label) for r in g_rows) if re.search(r"[a-z]{3}", x)}
            g_weak = set().union(*(_shapes(r.label, r.unit.heading) for r in g_rows))
            same_side = ("business" in u.url + u.heading.lower()) == ("business" in g.winner.url + g.winner.heading.lower())
            n_strong = len(strong & g_strong)
            # Identical row labels are strong evidence; matching tiers alone ("$25,000+") also need the same
            # product name, otherwise Money Market and HSA tiers would be treated as copies of each other.
            same = same_side and (n_strong >= 2 or ((n_strong >= 1 or weak & g_weak) and _same_product(u, g.winner)))
            if same:
                if _rank(u) > _rank(g.winner):
                    g.losers.append((g.winner, g.rows))
                    g.winner, g.rows = u, rows
                else:
                    g.losers.append((u, rows))
                g.coverage = max(g.coverage, cov)
                break
        else:
            groups.append(Group(u, rows, coverage=cov))
    for g in groups:
        wvals = _values(g.rows)
        keep = []
        for lu, lrows in g.losers:
            if _values(lrows) == wvals:
                g.alt_sources.append(lu)       # identical copy: another source for the same answer
            else:
                keep.append((lu, lrows))
        g.losers = keep
    biz_asked = "business" in q.flags
    groups.sort(key=lambda g: (-round(g.coverage, 2), (not biz_asked) and "business" in g.winner.url,
                               -max(r.score for r in g.rows), _neg(_rank(g.winner))))
    return groups


def _same_product(a: Unit, b: Unit) -> bool:
    """Do two tables describe the same product? Compare their nearest heading words."""
    ha = set(tokens(a.heading.split(" / ")[-1])) - GENERIC
    hb = set(tokens(b.heading.split(" / ")[-1])) - GENERIC
    if not ha or not hb:
        return True
    return bool(ha & hb) or bool(set(tokens(a.title)) & hb) or bool(set(tokens(b.title)) & ha)


def _rank(u: Unit) -> tuple:
    eff = u.table.effective if u.table else None
    return (eff or date.min, u.authority)


def _neg(rank: tuple) -> tuple:
    eff, auth = rank
    return (-eff.toordinal(), -auth)


def _values(rows: list[RowHit]) -> frozenset:
    return frozenset((r.label.lower(), tuple(c for c in r.row if "%" in c)) for r in rows)


def min_columns(header: list[str], row: list[str]) -> list[tuple[str, str]]:
    """'Min. to Open' / 'Min. to Earn' cells, for 'what's the minimum to open X' questions."""
    out = []
    for i, c in enumerate(row):
        h = header[i] if i < len(header) else ""
        if re.search(r"min", h, re.I) and re.search(r"\d", c):
            out.append((re.sub(r"\.\s*", " ", h).strip(), c))
    return out


def rate_columns(header: list[str], row: list[str], metric: Optional[str], intro: Optional[bool] = None) -> list[tuple[str, str]]:
    """Rate cells of a row, named by their column. intro=True keeps introductory columns, False drops them
    (when a table has both); None keeps everything."""
    cols = _rate_columns(header, row, metric)
    named = [(i, c) for i, c in enumerate(cols)]
    has_intro = any(n.startswith("introductory") for n, _ in cols)
    if has_intro and intro is not None:
        cols = [c for _, c in named if c[0].startswith("introductory") == intro] or cols
    return cols


def _rate_columns(header: list[str], row: list[str], metric: Optional[str]) -> list[tuple[str, str]]:
    out = []
    for i, c in enumerate(row):
        h = header[i] if i < len(header) else ""
        hl = h.lower()
        if "%" not in c:
            continue
        if re.search(r"payment|points|min", hl):
            continue
        name = ("APY" if "yield" in hl or "apy" in hl else "dividend rate" if "dividend" in hl else
                "APR" if "apr" in hl or "annual percentage rate" in hl else "rate" if "rate" in hl else "")
        # "Dividend Rate for 1st 6 Months" / "Intro APY" are introductory; "APY After Promotional Period" is not.
        if re.search(r"\bintro|\b(1st|first) \d+ months?|promotional rate", hl) and not re.search(r"\bafter\b", hl):
            name = f"introductory {name}".strip()
        out.append((name, c))
    if metric and len(out) > 1:
        pick = [x for x in out if x[0].lower().startswith(metric)]
        if pick:
            out = pick
    return out

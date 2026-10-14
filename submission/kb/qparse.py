"""Turn a member question into explicit qualifiers: term, model year, amount, metric, date, intent."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .parse import MONTHS
from .search import tokens

LINK_WORDS = re.compile(r"\b(link|url|web ?page|website|web address)\b", re.I)


@dataclass
class Range:
    lo: float
    hi: float

    def contains(self, x: float) -> bool:
        return self.lo <= x <= self.hi

    def overlaps(self, other: "Range") -> bool:
        return self.lo <= other.hi and other.lo <= self.hi


@dataclass
class Query:
    raw: str
    toks: list[str]
    term_months: Optional[int] = None          # "60-month", "5-year" -> 60
    term_raw: Optional[tuple[int, str]] = None  # (5, "year") as written
    model_year: Optional[int] = None
    amount: Optional[Range] = None             # balance / loan amount the member mentions
    metric: Optional[str] = None               # "apr" | "apy" | "dividend" | None
    rate_intent: bool = False
    link_intent: bool = False
    list_intent: bool = False
    as_of_text: Optional[str] = None           # date found in the question itself
    flags: set[str] = field(default_factory=set)


_TERM_RE = re.compile(r"\b(\d{1,3})\s*[- ]?\s*(months?|mos?|years?|yrs?)\b", re.I)
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(k|thousand|million|m)?\b", re.I)
_BARE_K_RE = re.compile(r"\b(\d{1,4})\s?(k|thousand)\b", re.I)


def money_value(num: str, suffix: Optional[str]) -> float:
    v = float(num.replace(",", ""))
    s = (suffix or "").lower()
    if s in ("k", "thousand"):
        v *= 1_000
    elif s in ("m", "million"):
        v *= 1_000_000
    return v


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty": 30,
    "thirty-six": 36, "forty-eight": 48, "sixty": 60, "seventy-two": 72, "eighty-four": 84,
}
_NUMWORD_RE = re.compile(r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")(?=[\s-]+(?:months?|years?|yrs?|mos?)\b)", re.I)


# Member phrasings mapped onto the site's words before parsing.
PHRASES = [
    (r"\bsign(?:ing)? up\b|\bbecome a member\b|\bapply for membership\b|\bopen a membership\b", "join"),
    (r"\bcd'?s?\b", "certificate"),
    (r"\bwhat'?s\b|\bwhats\b", "what is"),
    (r"\bthese days\b|\bright now\b|\bat the moment\b|\bgoing rate\b", "current rate"),
    (r"\bhome loan\b", "mortgage home loan"),
]


def parse_question(q: str, as_of: Optional[str] = None) -> Query:
    q = _NUMWORD_RE.sub(lambda m: str(NUMBER_WORDS[m.group(1).lower()]), str(q or ""))
    for pat, rep in PHRASES:
        q = re.sub(pat, rep, q, flags=re.I)
    low = q.lower().replace("–", "-").replace("—", "-")
    qu = Query(raw=q, toks=tokens(q))

    m = _TERM_RE.search(low)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        qu.term_raw = (n, "year" if unit.startswith(("y",)) else "month")
        qu.term_months = n * 12 if qu.term_raw[1] == "year" else n

    if re.search(r"\b(model|vehicle|auto|car|truck|motorcycle|suv|boat|rv|atv)\b", low):
        for y in _YEAR_RE.findall(low):
            # a year that is not the "as of" date of the question
            if not re.search(rf"(as of|in|during|on)\s+(\w+\s+)?{y}", low):
                qu.model_year = int(y)

    amounts = [money_value(n, s) for n, s in _MONEY_RE.findall(q)]
    amounts += [money_value(n, s) for n, s in _BARE_K_RE.findall(q)]
    if amounts:
        if len(amounts) >= 2 and re.search(r"\b(between|from|to|and|-)\b", low):
            qu.amount = Range(min(amounts), max(amounts))
        else:
            qu.amount = Range(amounts[0], amounts[0])
        qu.flags.add("amount")

    asked = [m for m, pat in (("apy", r"\bapy\b|annual percentage yield"), ("apr", r"\bapr\b|annual percentage rate"),
                              ("dividend", r"\bdividend"), ("rate", r"\b(interest )?rate\b(?! (sheet|page))"))
             if re.search(pat, low)]
    # "rate and APR" asks for both columns; a single named metric narrows the answer.
    named = [m for m in asked if m != "rate"]
    qu.metric = named[0] if len(named) == 1 and len(asked) == 1 else None

    # Rate questions name a rate, or ask what a deposit product pays/earns ("what does the 17-month special pay").
    qu.rate_intent = bool(re.search(r"\b(rates?|aprs?|apys?|interest|dividends?|yields?)\b", low)) or bool(
        re.search(r"\b(pay|pays|paying|earn|earns|earning)\b", low) and (
            qu.term_months or re.search(r"\b(certificate|savings|special|ira|money market|checking|account)\b", low)))
    qu.link_intent = bool(LINK_WORDS.search(low))
    qu.list_intent = bool(re.search(r"\b(what|which)\b.*\b(do you offer|are available|options|types|kinds)\b", low)) or bool(
        re.search(r"\b(all|list|every|each)\b", low)) or bool(re.search(r"\brates\b|\bcards\b|\baccounts\b", low))

    for word in ("jumbo", "special", "refinance", "refi", "business", "roth", "traditional", "ira", "intro",
                 "introductory", "balance transfer", "purchase", "variable", "fixed", "heloc", "student",
                 "personal", "rewards", "platinum", "equity", "improvement"):
        m = re.search(rf"\b{word}", low)
        if m and not re.search(r"\b(not|non|no|without|excluding|except)\b[\s-]+(?:the |a |an )?$", low[:m.start()]) \
                and not low[max(0, m.start() - 4):m.start()].endswith("non-"):
            qu.flags.add("refinance" if word == "refi" else word)

    qu.as_of_text = date_in_question(low)
    return qu


def date_in_question(low: str) -> Optional[str]:
    """Dates written in the question: 'as of May 2026' (end of month), 'early/mid/end of June 2026',
    'on June 1, 2026', '5/15/2026', '2026-05-15'."""
    m = re.search(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", low)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", low)
    if m:
        return _iso(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    mon_re = "(january|february|march|april|may|june|july|august|september|october|november|december)"
    m = re.search(r"\b(?:(early|beginning of|start of|mid|middle of|late|end of)[\s-]+)?" + mon_re +
                  r"(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?,?\s+(20\d\d)\b", low)
    if not m:
        return None
    when, mon, day, year = m.group(1), MONTHS[m.group(2)], m.group(3), int(m.group(4))
    last = calendar.monthrange(year, mon)[1]
    if day:
        d = int(day)
    elif when in ("early", "beginning of", "start of"):
        d = 5
    elif when in ("mid", "middle of"):
        d = 15
    else:
        d = last
    return _iso(year, mon, d)


def _iso(y: int, m: int, d: int) -> Optional[str]:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None

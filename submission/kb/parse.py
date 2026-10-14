"""Page-level parsing: classify pages, find dates, pull out tables and passages.

Everything here is pure functions over one page's markdown, so ingest can cache by content hash.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

# --------------------------------------------------------------------------- page kinds
# How much we trust a page for a *current* value. Rate sheets beat product pages, which beat
# promo landing pages, which beat calculators, guides, articles and old PDFs.
AUTHORITY = {
    "rates": 5,
    "product": 4,
    "contact": 4,
    "promo": 2,
    "guide": 2,
    "document": 2,
    "calculator": 1,
    "article": 1,
    "listing": 0,
}


def page_kind(url: str) -> str:
    path = urlparse(url).path.lower().rstrip("/") or "/"
    if path in ("/rates", "/business-rates"):
        return "rates"
    if path == "/contact":
        return "contact"
    if "/author/" in path or "/tag/" in path or path in ("/learn/blog", "/newsroom", "/business/resources"):
        return "listing"
    if path.startswith("/hubfs/"):
        return "document"
    if "calculator" in path or path.startswith("/calculators"):
        return "calculator"
    if path.startswith(("/learn/", "/newsroom/", "/dupacodaily/", "/business/resources/", "/resources/")) or re.match(
        r"^/20\d\d/", path
    ):
        if path.startswith("/learn/master-your-money"):
            return "guide"
        return "article"
    if path.endswith("-lp") or "promo" in path or "special-offer" in path or path in ("/switch-and-earn", "/earn-more", "/brighttrack", "/checking-offer-lp"):
        return "promo"
    if path in ("/homebuying-guidance",) or "guidance" in path:
        return "guide"
    return "product"


# --------------------------------------------------------------------------- dates
MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTH_RE = "(january|february|march|april|may|june|july|august|september|october|november|december)"
LONG_DATE_RE = re.compile(MONTH_RE + r"\.? (\d{1,2}),? (\d{4})", re.I)
NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
EFFECTIVE_RE = re.compile(r"effective(?: as of)?:?\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)


def _mkdate(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def effective_date(text: str) -> Optional[date]:
    m = EFFECTIVE_RE.search(text)
    return _mkdate(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def publish_date(url: str, md: str) -> Optional[date]:
    """Articles print their date in the header block ("July 21, 2026"); some URLs carry /YYYY/MM/DD/."""
    m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url)
    if m:
        return _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for line in md.splitlines()[:20]:
        m = re.fullmatch(r"\s*" + MONTH_RE + r" (\d{1,2}), (\d{4})\s*", line, re.I)
        if m:
            return _mkdate(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


# --------------------------------------------------------------------------- tables
@dataclass
class Table:
    context: str                 # headings / labels just above the table
    header: list[str]
    rows: list[list[str]]
    effective: Optional[date]    # the "Rates are effective as of" line that belongs to this table
    start: int                   # line numbers, for debugging
    end: int
    ends: Optional[date] = None  # "offer ended 2025-09-30": the promotion is over after this date
    prose: bool = False          # built from a "label: value" sentence rather than a markdown table


_FOOTNOTE_AFTER_PCT = re.compile(r"(\d%)\s*\d\b")
_FOOTNOTE_AFTER_APR = re.compile(r"\b(APR|APY)\d\b")


def clean_cell(c: str) -> str:
    c = c.strip().replace("\\|", "|")
    c = _FOOTNOTE_AFTER_PCT.sub(r"\1", c)       # "0.20%1" -> "0.20%"
    c = _FOOTNOTE_AFTER_APR.sub(r"\1", c)       # "5.39% APR1" -> "5.39% APR"
    c = c.replace("–", "-").replace("—", "-")
    c = re.sub(r"\*\*|__", "", c)
    return re.sub(r"\s+", " ", c).strip()


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [clean_cell(c) for c in re.split(r"(?<!\\)\|", s)]


def _is_sep(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells)


_LABEL_SKIP = re.compile(r"^(\[.*\]\(.*\)\s*)+$|^!\[|^- \[|^#{1,6}\s*$")


def _context_above(lines: list[str], start: int, stop: int) -> str:
    """Up to 3 short label-like lines above a table (product names, section headings)."""
    out = []
    i = start - 1
    while i >= stop and len(out) < 3 and start - i < 25:
        s = lines[i].strip()
        i -= 1
        tab = re.match(r"^- \[([^\]]{3,60})\]\([^)]*#\)$", s)   # old layout: "- [Money Market Account Rates](...rates#)"
        if tab:
            out.append(tab.group(1))
            continue
        if not s or _LABEL_SKIP.search(s) or s.startswith("|"):
            continue
        if EFFECTIVE_RE.search(s) or len(s) > 140:
            # a long paragraph or the previous table's effective line ends the context block
            if out:
                break
            continue
        out.append(s.lstrip("#").strip().strip("*").strip())
    return " / ".join(reversed(out))


def extract_tables(md: str) -> list[Table]:
    lines = md.splitlines()
    blocks = []  # (start, end) line ranges of table blocks
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append((i, j))
            i = j
        else:
            i += 1
    tables = []
    for k, (s, e) in enumerate(blocks):
        cells = [_split_row(l) for l in lines[s:e]]
        if len(cells) >= 2 and _is_sep(cells[1]):
            header, rows = cells[0], [c for c in cells[2:] if not _is_sep(c)]
        else:  # header-less fragment (some crawls drop the header row)
            header, rows = [], [c for c in cells if not _is_sep(c)]
        if not rows:
            continue
        prev_end = blocks[k - 1][1] if k else 0
        next_start = blocks[k + 1][0] if k + 1 < len(blocks) else len(lines)
        # The effective-date line follows its table; a line above belongs to the previous table.
        # Footnotes ("1Rates are effective as of ...") restate older dates, so skip lines starting with a digit.
        eff = None
        for l in lines[e:min(next_start, e + 12)]:
            if re.match(r"\s*\d", l):
                continue
            eff = effective_date(l)
            if eff:
                break
        if eff is None:
            for l in lines[e:min(next_start, e + 12)]:
                eff = effective_date(l)
                if eff:
                    break
        ctx = _context_above(lines, s, prev_end)
        # The footnote under a table names its product family ("1Money Market Rates: Dividends ...").
        for l in lines[e:min(next_start, e + 12)]:
            fm = re.match(r"^\s*\**\d([A-Z][A-Za-z' &/-]{3,50}?)(?: Rates?)?:", l)
            if fm and not EFFECTIVE_RE.search(fm.group(1)):
                ctx = f"{ctx} / {fm.group(1).strip()}"
                break
        if len(ctx) < 25:
            # Older layouts label tables only "Regular"/"Special"; the section's link ("Explore" ->
            # /personal/savings/certificates/) is the only hint of the product, so borrow its path words.
            for l in reversed(lines[max(0, s - 30):s]):
                m = re.search(r"\]\((https?://[^)\s]+)\)", l)
                if m and "/hubfs/" not in m.group(1):
                    path = urlparse(m.group(1)).path.strip("/").split("/")[-1]
                    ctx = f"{path.replace('-', ' ')} / {ctx}" if path else ctx
                    break
        ends = None
        for l in lines[e:min(next_start, e + 12)]:
            ends = ended_date(l) if re.search(r"offer|promotion|special", l, re.I) else None
            if ends:
                break
        tables.append(Table(ctx, header, rows, eff, s, e, ends=ends))
    return tables


ENDED_RE = re.compile(r"(?:offer|promotion|special|rate)?\s*(?:ended|expired|ends|expires|valid through|through)\s*(?:on)?\s*"
                      r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|" + MONTH_RE + r"\.? \d{1,2},? \d{4})", re.I)
PROSE_RATE_RE = re.compile(r"^(?P<label>[A-Za-z0-9][^|%]{3,100}?[A-Za-z)])\s*:?\s+(?P<val>\d{1,2}(?:\.\d{1,3})?%)(?P<rest>[^%]{0,80})$")


def _parse_any_date(s: str) -> Optional[date]:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s)
    except ValueError:
        return None
    m = NUM_DATE_RE.search(s)
    if m:
        return _mkdate(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = LONG_DATE_RE.search(s)
    if m:
        return _mkdate(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def ended_date(text: str) -> Optional[date]:
    m = ENDED_RE.search(text)
    return _parse_any_date(m.group(1)) if m else None


def prose_rate_tables(md: str) -> list[Table]:
    """Short 'label: 3.50%' lines (no markdown table) become one-row tables, e.g.
    '6-month certificate APY: 3.50%' or 'Summer special: 6-month certificate APY 2.95% (offer ended 2025-09-30)'."""
    out = []
    heading = ""
    page_eff = None
    for n, raw in enumerate(md.splitlines()):
        line = re.sub(r"\*\*|__", "", raw.strip().lstrip("-* ").strip())
        if raw.lstrip().startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        page_eff = effective_date(line) or page_eff
        if not line or line.startswith("|") or len(line) > 170:
            continue
        m = PROSE_RATE_RE.match(line)
        if not m:
            continue
        label = m.group("label").strip().rstrip(":")
        if not re.search(r"\b(apy|apr|rate|yield|dividend)\b", label + " " + m.group("rest"), re.I):
            continue
        metric = "APY" if re.search(r"\bapy\b|yield", line, re.I) else "APR" if re.search(r"\bapr\b", line, re.I) else "Rate"
        t = Table(heading, ["Item", metric], [[label, m.group("val")]], page_eff, n, n + 1,
                  ends=ended_date(line), prose=True)
        out.append(t)
    return out


# --------------------------------------------------------------------------- passages
@dataclass
class Passage:
    heading: str
    text: str
    start: int = 0
    tags: list[str] = field(default_factory=list)


_NOISE = re.compile(
    r"^(\[[^\]]*\]\([^)]*\)\s*)+$|^!\[|^(Third Party Plugins|Messenger( Launcher)?|Continue|Cancel)$", re.I)


_TEASER_RE = re.compile(r"^#{0,6}\s*(Handpicked for You|Related (Articles|Posts|Stories)|You (may|might) also like|More from)", re.I | re.M)


def strip_teasers(md: str) -> str:
    """Article pages end with teaser cards for other (older) articles; their dates and numbers mislead."""
    m = _TEASER_RE.search(md)
    return md[:m.start()] if m else md


# A single call-to-action link ("[Join Dupaco](https://join.dupaco.com/)") is content, not navigation.
_CTA = re.compile(r"^\[[^\]]{2,40}\]\(https?://[^)\s]+\)$")


def passages(md: str, title: str = "") -> list[Passage]:
    """Split a page into heading-scoped passages of a few paragraphs; tables are kept inline as text."""
    md = strip_teasers(md)
    out: list[Passage] = []
    heading = title
    buf: list[str] = []
    start = 0

    def flush():
        txt = "\n".join(buf).strip()
        if len(txt) > 25:
            out.append(Passage(heading, txt, start))
        buf.clear()

    for n, raw in enumerate(md.splitlines()):
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("#"):
            flush()
            heading = s.lstrip("#").strip() or heading
            start = n
            continue
        if not s:
            if sum(len(b) for b in buf) > 700:
                flush()
                start = n
            continue
        if _NOISE.search(s) and not _CTA.match(s):
            continue
        if not buf:
            start = n
        buf.append(s)
        if sum(len(b) for b in buf) > 1400:
            flush()
            start = n
    flush()
    return out

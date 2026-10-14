"""Question answering over the versioned store."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .qparse import Query, parse_question
from .rates import candidate_groups, min_columns, rate_columns
from .store import Store, Unit
from .text import TextAnswerer

# No digits: a refusal must not state any number (the phone number would count as one).
REFUSAL = ("I don't know: that isn't listed on the website. Please contact the credit union or visit a branch "
           "for that information.")


class Engine:
    def __init__(self, store: Store):
        self.s = store
        self.t = TextAnswerer(store)

    # ------------------------------------------------------------------ entry point
    def answer(self, question: str, as_of: Optional[str] = None) -> dict:
        q = parse_question(question, as_of)
        view = self.s.view_for(as_of or q.as_of_text)
        if q.link_intent:
            r = self._link(q, view)
            if r:
                return r
        availability = bool(re.search(r"\b(still|available|offer|discontinued|anymore|any more)\b", q.raw.lower()))
        min_q = _wants_minimum(q.raw)
        if q.rate_intent or (availability and q.term_months) or min_q:
            r = self._rates(q, view)
            if r:
                return r
            gone = self._discontinued(q, view)
            if gone:
                return gone
            if self._is_specific_rate_question(q):
                return self._refuse("rate not published")
        return self._text(q, view)

    def _discontinued(self, q: Query, view) -> Optional[dict]:
        """A product with rates in an earlier snapshot but none now was withdrawn (e.g. an ended special)."""
        for old in reversed([v for v in self.s.views if v.taken_on < view.taken_on]):
            groups = candidate_groups(self.s, old, q)
            if groups and groups[0].coverage >= 0.99 and _specific(q):
                g = groups[0]
                # Name the product but quote no rate: the old value is not a current answer.
                label = re.sub(r"\s+", " ", re.sub(r"[\d.,]+%|\$[\d,.]+", "", g.rows[0].label)).strip()
                text = (f"The {label} is no longer offered: it doesn't appear on the current rate sheet. "
                        f"Please contact the credit union for today's options.")
                return {"text": text, "sources": [g.winner], "confident": False,
                        "note": f"last listed in snapshot {old.snapshot_id} ({g.winner.url})"}
        return None

    # ------------------------------------------------------------------ rates
    def _is_specific_rate_question(self, q: Query) -> bool:
        """Questions shaped like a rate lookup. If no current rate table answers them, the honest answer is
        'not listed' (free text would only dump neighbouring tiers or other products)."""
        low = q.raw.lower()
        if re.search(r"\b(rate|rates|apr|apy|interest|dividend|yield)\b", low):
            return True
        return bool(re.search(r"\b(pay|pays|paying|earn|earns|earning)\b", low) and (
            _specific(q) or re.search(r"\b(certificate|cd|savings|special|account|ira|money market|checking)\b", low)))

    def _rates(self, q: Query, view) -> Optional[dict]:
        groups = candidate_groups(self.s, view, q)
        if not groups:
            return None
        best = groups[0]
        chosen = [best]
        if q.list_intent:
            chosen += [g for g in groups[1:6] if g.coverage >= best.coverage * 0.8]
        lines, sources, notes = [], [], []
        for g in chosen:
            rows = sorted(g.rows, key=lambda r: -r.score)
            top = rows[0].score
            rows = [r for r in rows if r.score >= top - 0.01] if not q.list_intent and _specific(q) else rows
            if q.amount is not None:
                rows = _tightest_tiers(rows)
            rows = sorted(rows, key=lambda r: g.winner.table.rows.index(r.row))[:8]
            product = _product_name(g.winner)
            parts = []
            want_min = _wants_minimum(q.raw)
            low = q.raw.lower()
            # "after the promotional period" asks for the standard rate; "intro/promo rate" for the introductory one
            intro = (False if re.search(r"\bafter (the )?(intro|promo)", low) else
                     True if ({"intro", "introductory"} & q.flags or re.search(r"\bpromo", low)) else None)
            for r in rows:
                cols = rate_columns(g.winner.table.header, r.row, q.metric, intro=intro)
                if want_min:
                    cols = min_columns(g.winner.table.header, r.row)
                    if not cols and not q.rate_intent:
                        continue       # a minimum the rate table doesn't state (e.g. credit score): not this table
                if not cols:
                    continue
                vals = ", ".join(f"{v} {n}".strip() if n and n.lower() not in v.lower() else v for n, v in cols)
                parts.append(f"{r.label}: {vals}")
            if not parts:
                continue
            body = "; ".join(parts)
            redundant = not product or all(p.lower().startswith(product.lower()) for p in parts)
            lines.append(body if redundant else f"{product}: {body}")
            sources.append(g.winner)
            sources.extend(g.alt_sources[:2])
            if g.losers:
                old = sorted({_fmt(lu.table.effective) for lu, _ in g.losers})
                notes.append(f"Other pages still show older rates (effective {', '.join(old)}); "
                             f"used {g.winner.url} effective {_fmt(g.winner.table.effective)}.")
        if not lines:
            return None
        text = ". ".join(lines) + "."
        if q.change_intent:
            prev = self._previous_values(q, view, chosen[0])
            if prev:
                text += " " + prev
        return self._ok(text, sources, " ".join(notes) or None)

    def _previous_values(self, q: Query, view, g) -> Optional[str]:
        """For 'what was it before?': the most recent earlier value of the same row on the same page."""
        current = {r.label.lower(): rate_columns(g.winner.table.header, r.row, q.metric) for r in g.rows}
        for old in reversed([v for v in self.s.views if v.taken_on < view.taken_on]):
            for og in candidate_groups(self.s, old, q):
                if og.winner.url != g.winner.url:
                    continue
                for r in og.rows:
                    cur = current.get(r.label.lower())
                    was = rate_columns(og.winner.table.header, r.row, q.metric)
                    if cur and was and [v for _, v in was] != [v for _, v in cur]:
                        vals = ", ".join(f"{v} {n}".strip() for n, v in was)
                        return f"Before that it was {vals} (download of {old.taken_on.isoformat()})."
        return None

    # ------------------------------------------------------------------ links
    def _link(self, q: Query, view) -> Optional[dict]:
        """Return the page that answers the question, plus any link written in its best sentence."""
        ranked = self.t.ranked_sentences(q, view)
        if not ranked:
            return None
        pages: dict[str, float] = {}
        for score, cov, joint, u, k, s in ranked[:40]:
            pages[u.url] = max(pages.get(u.url, 0), score + 0.05 * u.authority)
        urls = sorted(pages, key=lambda x: (-pages[x], x))[:2]
        best = ranked[0]
        inline = [m for m in re.findall(r"\((https?://[^)\s]+)\)", best[3].text) if "dupaco.com" in m][:1]
        urls = list(dict.fromkeys([urls[0]] + inline + urls[1:]))[:3]
        text = "You can find it here: " + ", ".join(urls)
        return self._ok(text, [u for u in (self._unit_for(view, x) for x in urls[:2]) if u], None)

    # ------------------------------------------------------------------ free text
    def _text(self, q: Query, view) -> dict:
        r = self.t.answer(q, view)
        if r is None:
            return self._refuse("no supporting text")
        if "refuse" in r:
            return self._refuse(r["refuse"])
        return self._ok(r["text"], r["units"], None)

    # ------------------------------------------------------------------ helpers
    def _unit_for(self, view, url: str) -> Optional[Unit]:
        for uid in sorted(view.units):
            u = self.s.units[uid]
            if u.url == url:
                return u
        return None

    def _ok(self, text: str, units: list[Unit], note: Optional[str]) -> dict:
        seen, srcs = set(), []
        for u in units:
            if u.url not in seen:
                seen.add(u.url)
                srcs.append(u)
        return {"text": text, "sources": srcs, "confident": True, "note": note}

    def _refuse(self, why: str) -> dict:
        return {"text": REFUSAL, "sources": [], "confident": False, "note": why}


def _wants_minimum(raw: str) -> bool:
    """'What's the minimum to open X?' asks for the minimum; 'the special ($1,000 minimum)' only names a tier."""
    low = raw.lower()
    low = re.sub(r"\$[\d,.]+\s*(minimum|min\.?)|(minimum|min\.?) (of )?\$[\d,.]+", " ", low)
    return bool(re.search(r"\bminimum\b|\bmin\.? to\b|\bhow much (do i need|to open)\b", low))


def _tightest_tiers(rows):
    """'$300,000 or less' and '$200,000 or less' both contain $150,000; the narrowest tier is the one that applies."""
    from .rates import _row_amount_range, _shape
    best: dict[str, tuple] = {}
    for r in rows:
        rng = _row_amount_range(r.label.lower())
        key = re.sub(r"\$[#,]+[^ ]*|or less|or more|\+", "", _shape(r.label))
        span = (rng.hi - rng.lo) if rng else float("inf")
        if key not in best or span < best[key][0]:
            best[key] = (span, [r])
        elif span == best[key][0]:
            best[key][1].append(r)
    keep = {id(r) for _, rs in best.values() for r in rs}
    return [r for r in rows if id(r) in keep]


def _specific(q: Query) -> bool:
    return any(x is not None for x in (q.term_months, q.model_year, q.amount))


def _product_name(u: Unit) -> str:
    h = u.heading.split(" / ")[-1].strip()
    return h if h and len(h) < 80 else ""


def _fmt(d: Optional[date]) -> str:
    return d.strftime("%m/%d/%Y") if d else "undated"

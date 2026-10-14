"""Question answering over the versioned store."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .qparse import Query, parse_question
from .rates import candidate_groups, rate_columns
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
        if q.rate_intent or (availability and q.term_months):
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
        return bool(re.search(r"\b(rate|apr|apy|interest)\b", q.raw.lower()))

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
            rows = sorted(rows, key=lambda r: g.winner.table.rows.index(r.row))[:8]
            product = _product_name(g.winner)
            parts = []
            for r in rows:
                cols = rate_columns(g.winner.table.header, r.row, q.metric)
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
        return self._ok(text, sources, " ".join(notes) or None)

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


def _specific(q: Query) -> bool:
    return any(x is not None for x in (q.term_months, q.model_year, q.amount))


def _product_name(u: Unit) -> str:
    h = u.heading.split(" / ")[-1].strip()
    return h if h and len(h) < 80 else ""


def _fmt(d: Optional[date]) -> str:
    return d.strftime("%m/%d/%Y") if d else "undated"

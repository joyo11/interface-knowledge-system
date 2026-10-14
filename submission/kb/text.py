"""Free-text answers: sentence-level retrieval plus a few handlers for common member questions
(hours, branch addresses, "how many" counts) and an honest refusal when the site doesn't say."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .qparse import Query
from .search import tokens
from .store import Store, Unit

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])|\n+")
DAY = r"(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)[a-z]*"
SCHED_LINE = re.compile(rf"^\s*({DAY}(?:\s*[-–]\s*{DAY})?)\s*:\s*(.+?)\s*$", re.I)
TIME_RE = re.compile(r"\b\d{1,2}(?::\d\d)?\s*[ap]\.?m\.?|\bnoon\b|\bclosed\b", re.I)
ADDRESS_RE = re.compile(r"\b\d{2,6} [A-Z0-9][\w .'-]*?(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Pkwy|Way|Ct|Hwy|Pl|Square)\b\.?,?[^\n]{0,60}?\b\d{5}\b")
GENERIC_Q = set(tokens("what when where how who which does do is are can tell me about the a an your dupaco "
                       "information info question know want find get give"))
KNOWN_CAPS = {"Dupaco", "I", "What", "When", "Where", "How", "Who", "Which", "Is", "Are", "Can", "Do", "Does",
              "Visa", "APR", "APY", "IRA", "CD", "ATM", "Roth", "My", "The", "Get", "Provide", "Tell", "Please"}


def _link(m: re.Match) -> str:
    text, url = m.group(1).strip(), m.group(2).strip()
    # Page links stay readable ("Join Dupaco (https://join.dupaco.com/)"); asset links (images, PDFs) and
    # links to the same text keep just their text, so stray file names and dates don't leak into answers.
    if re.search(r"\.(jpe?g|png|gif|svg|pdf|xlsx?)(\?|$)|/hubfs/|^tel:|^mailto:", url, re.I) or not url.startswith("http"):
        return text
    return f"{text} ({url})" if text else url


def clean(s: str) -> str:
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)", _link, s)
    s = re.sub(r"\*\*|__|^#+\s*|^[-*]\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def sentences(text: str) -> list[str]:
    return [c for c in (clean(x) for x in SENT_SPLIT.split(text)) if len(c) > 2]


def named_place(q: Query) -> Optional[str]:
    """A capitalised place name in the question, e.g. 'Mullan Avenue' or 'Galena'."""
    words = [re.sub(r"['’]s$", "", w) for w in re.findall(r"[A-Za-z$'’&-]+", q.raw)]
    phrase, best = [], None
    for i, w in enumerate(words):
        cap = w[:1].isupper() and w not in KNOWN_CAPS
        if cap:
            phrase.append(w)
        else:
            if phrase:
                best = best or " ".join(phrase)
            phrase = []
    if phrase:
        best = best or " ".join(phrase)
    if best:
        best = re.sub(r"\s+(Branch|Office|Location)$", "", best)
    return best


DATE_PAT = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b"
                      r"[^.]{0,12}\d{4}|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
ANSWER_TYPES = {
    "date": DATE_PAT,
    "number": re.compile(r"\b\d[\d,]*\b|\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b", re.I),
    "money": re.compile(r"\$\s?\d|\d%"),
    "phone": re.compile(r"\b\d{3}[-. )]\d{3}[-.]\d{4}\b"),
    "id": re.compile(r"\b\d{3}[-. )]?\d{3}[-.]?\d{3,4}\b|\b\d{9}\b"),
}


def answer_type(q: str) -> Optional[str]:
    low = q.lower()
    if re.search(r"\b(routing|account|nmls|phone|fax|tax id) number\b|\brouting\b", low):
        return "id"
    if re.search(r"^when\b|\bwhat (date|day|year)\b|\bstill open\b|\bdeadline\b|\bclosing date\b", low):
        return "date"
    if re.search(r"\bhow many\b|\bnumber of\b", low):
        return "number"
    if re.search(r"\bhow much\b|\bfee\b|\bcost\b|\bminimum\b|\bup to\b", low):
        return "money"
    if re.search(r"\bphone\b|\bcall\b|\bnumber to\b", low):
        return "phone"
    return None


ORDINALS = set("first second third fourth fifth sixth seventh eighth ninth tenth".split())
CTX_W, PATH_W = 0.5, 0.25


class TextAnswerer:
    def __init__(self, store: Store):
        self.s = store

    # ------------------------------------------------------------------ dispatch
    def answer(self, q: Query, view) -> Optional[dict]:
        low = q.raw.lower()
        hours_q = re.search(r"\bhours\b", low) or (
            re.search(r"\b(lobby|lobbies|drive-?up|drive-?thru|drive-?through|branch(es)?)\b", low)
            and re.search(r"\b(open|close|closes|closing time|what time|when)\b", low)
            and not re.search(r"\b(close (on|march|in)|closing date|closed for good|still open|permanently)\b", low)
            and not re.search(r"\bhy-?vee\b", low))
        if hours_q and not re.search(
                r"volunteer|hours of|work hours", low):
            return self.hours(q, view)
        if re.search(r"\baddress\b|\bwhere(?: is|'s|’s| can i find)\b|\blocated\b|\blocation of\b", low) and re.search(
                r"branch|office|location", low):
            r = self.address(q, view)
            if r:
                return r
        if re.search(r"\bhow many\b|\bnumber of\b", low):
            r = self.count(q, view)
            if r:
                return r
        return self.best_sentences(q, view)

    # ------------------------------------------------------------------ sentence retrieval
    def ranked_sentences(self, q: Query, view, top_units: int = 40, kinds=("passage",)):
        hits = self.s.index.search(q.toks, top=top_units, allow=view.units)
        if not hits:
            return []
        idf = self.s.index.idf
        qset = {t for t in q.toks if t not in GENERIC_Q} or set(q.toks)
        qmass = sum(idf.get(t, 1.0) for t in qset) or 1.0
        top = hits[0][0] or 1.0
        atype = answer_type(q.raw)
        # Anchor: the question's rarest word (usually a name: Home$tart, Hy-Vee, Galena, Forbes) must appear in
        # the sentence or its page title/heading, otherwise the sentence is about something else.
        rare = sorted(qset, key=lambda t: -idf.get(t, 0))
        anchor = rare[0] if rare and idf.get(rare[0], 0) >= 6.0 and not rare[0].isdigit() else None
        out = []
        for sc, uid in hits:
            u = self.s.units[uid]
            if u.kind not in kinds or u.page_kind == "listing":
                continue
            ctx = set(tokens(f"{u.title} {u.heading}"))
            path = set(tokens(urlparse(u.url).path.replace("-", " ").replace("/", " ")))
            ctx_cov = sum(idf.get(t, 1.0) for t in qset & ctx) / qmass
            path_cov = sum(idf.get(t, 1.0) for t in qset & path) / qmass
            rec = 0.0
            if u.published:
                rec = max(0.0, 1 - (view.taken_on - u.published).days / 3650)
            for k, s in enumerate(sentences(u.text)):
                st = set(tokens(s))
                cov = sum(idf.get(t, 1.0) for t in qset & st) / qmass
                if cov <= 0:
                    continue
                if atype and ANSWER_TYPES[atype].search(s):
                    cov_bonus = 0.3
                else:
                    cov_bonus = 0.0
                carried = 0.8 if uid in view.carried else 1.0
                if anchor and anchor not in st and anchor not in ctx:
                    carried *= 0.6
                score = carried * (cov + cov_bonus + CTX_W * ctx_cov + PATH_W * path_cov + 0.2 * sc / top + 0.04 * u.authority
                         + 0.03 * rec)
                out.append((score, cov, max(cov, min(1.0, cov + ctx_cov)), u, k, s))
        out.sort(key=lambda x: (-x[0], x[3].uid, x[4]))
        return out

    def best_sentences(self, q: Query, view) -> Optional[dict]:
        ranked = self.ranked_sentences(q, view)
        if not ranked:
            return None
        score, cov, joint, u, k, s = ranked[0]
        if joint < 0.3:
            return None
        page = [r for r in ranked if r[3].url == u.url]
        atype = answer_type(q.raw)
        if atype and not ANSWER_TYPES[atype].search(s):
            # The best page usually states the date/number in a sentence that doesn't repeat every keyword.
            typed = [r for r in page if ANSWER_TYPES[atype].search(r[5]) and r[0] >= 0.6 * score] or [
                r for r in ranked[:60] if ANSWER_TYPES[atype].search(r[5]) and r[0] >= 0.6 * score]
            if typed:
                score, cov, joint, u, k, s = typed[0]
        else:
            # Near-ties on one page: the earliest sentence is usually the lead that states the fact.
            close = [r for r in page if r[0] >= score - 0.08]
            score, cov, joint, u, k, s = min(close, key=lambda r: (r[3].uid, r[4]))
        sents = sentences(u.text)
        picked = [k]
        # Keep going while the answer is short or introduces a list ("...like:"), up to ~3 sentences.
        nxt = k + 1
        while nxt < len(sents) and len(picked) < 4 and (
                sents[picked[-1]].endswith(":") or len(" ".join(sents[i] for i in picked)) < 200):
            picked.append(nxt)
            nxt += 1
        text = " ".join(sents[i] for i in picked)
        head = clean(u.heading)
        if head and not re.search(r"\d", head) and head.lower() not in text.lower() and len(head) < 90:
            text = f"{head}: {text}"
        return {"text": text[:700], "units": [u], "joint": joint}

    # ------------------------------------------------------------------ hours
    def schedules(self, view) -> list[tuple[Unit, str, list[tuple[str, str]]]]:
        """Every Lobby / Drive-up schedule block on the site in this snapshot."""
        out = []
        for uid in sorted(view.units):
            u = self.s.units[uid]
            if u.kind != "passage" or not re.search(r"\b(lobby|drive-?up)\b", u.text, re.I):
                continue
            label, rows = None, []
            for raw in u.text.split("\n"):
                line = clean(raw)
                m_label = re.match(r"^(Lobby|Drive-?up|Drive-?thru)\b:?\s*(.*)$", line, re.I)
                if m_label:
                    if label and rows:
                        out.append((u, label, rows))
                    label, rows = ("Drive-up" if m_label.group(1).lower().startswith("drive") else "Lobby"), []
                    line = m_label.group(2)
                    if not line:
                        continue
                m = SCHED_LINE.match(line)
                if label and m and TIME_RE.search(m.group(2)):
                    rows.append((m.group(1).replace("–", "-"), m.group(2).replace("–", "-")))
                elif label and rows:
                    out.append((u, label, rows))
                    label, rows = None, []
            if label and rows:
                out.append((u, label, rows))
        return out

    def hours(self, q: Query, view) -> Optional[dict]:
        blocks = self.schedules(view)
        place = named_place(q)
        low = q.raw.lower()
        if place and place.lower() not in ("hy-vee",):
            mine = [b for b in blocks if place.lower() in (b[0].text + " " + b[0].title).lower()]
            if not mine:
                return {"refuse": f"no hours published for {place}"}
            blocks = mine
        if not blocks:
            return None
        want = "Drive-up" if re.search(r"drive", low) else "Lobby" if re.search(r"lobby", low) else None
        parts, units = [], []
        for label in (["Lobby", "Drive-up"] if want is None else [want]):
            scheds = Counter()
            first: dict[tuple, Unit] = {}
            for u, lab, rows in blocks:
                if lab != label:
                    continue
                key = tuple(rows)
                scheds[key] += 1
                first.setdefault(key, u)
            if not scheds:
                continue
            ordered = sorted(scheds, key=lambda k: (-scheds[k], first[k].uid))[:4]
            texts = ["; ".join(f"{d} {t}" for d, t in key) for key in ordered]
            units += [first[k] for k in ordered]
            if len(texts) == 1:
                parts.append(f"{label} hours: {texts[0]}.")
            else:
                parts.append(f"{label} hours vary by branch. Most branches: {texts[0]}. Other branches: "
                             + " | ".join(texts[1:]) + ".")
        if not parts:
            return None
        tail = "" if place else " Let me know which branch you mean for its exact hours."
        return {"text": " ".join(parts) + tail, "units": units[:3]}

    # ------------------------------------------------------------------ addresses
    def address(self, q: Query, view) -> Optional[dict]:
        place = named_place(q)
        if not place:
            return None
        pl = place.lower()
        order = sorted(view.units, key=lambda i: (self.s.units[i].page_kind != "contact", -self.s.units[i].authority, i))
        for uid in order:
            u = self.s.units[uid]
            if u.kind != "passage" or pl not in u.text.lower():
                continue
            for line in u.text.split("\n"):
                if pl in line.lower() and ADDRESS_RE.search(line):
                    return {"text": f"The {place} branch is at {clean(ADDRESS_RE.search(line).group(0))}.",
                            "units": [u]}
        return {"refuse": f"no address for {place}"}

    # ------------------------------------------------------------------ counts
    def count(self, q: Query, view) -> Optional[dict]:
        """'How many X': the same count is restated in many dated articles; the newest one wins."""
        ranked = self.ranked_sentences(q, view, top_units=200)
        nouns = [t for t in q.toks if t not in GENERIC_Q and not t.isdigit() and t not in ("many", "number")]
        cands = []
        for score, cov, joint, u, k, s in ranked:
            st = tokens(s)
            if any(t in nouns and i > 0 and (st[i - 1].isdigit() or st[i - 1] in ORDINALS) for i, t in enumerate(st)):
                cands.append((u.published or date.min, score, u, s))
        if not cands:
            return None
        top_score = max(c[1] for c in cands)
        cands = [c for c in cands if c[1] >= 0.6 * top_score]
        cands.sort(key=lambda c: (-c[0].toordinal(), -c[1], c[2].uid))
        d, sc, u, s = cands[0]
        return {"text": s, "units": [u]}

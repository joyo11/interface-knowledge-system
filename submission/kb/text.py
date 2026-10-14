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
LOOSE_ADDRESS_RE = re.compile(r"\b(located at|address is|at) \d{2,6} [A-Z0-9][\w .'-]{2,40}?\b(St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|"
                              r"Lane|Blvd|Boulevard|Highway|Hwy|Pkwy|Parkway|Way|Ct|Court|Pl|Place|Square)\b")
GENERIC_Q = set(tokens("what when where how who which does do is are can tell me about the a an your dupaco "
                       "information info question know want find get give"))
KNOWN_CAPS = {"Dupaco", "I", "What", "When", "Where", "How", "Who", "Which", "Is", "Are", "Can", "Do", "Does",
              "Visa", "APR", "APY", "IRA", "CD", "ATM", "Roth", "My", "The", "Get", "Provide", "Tell", "Please",
              "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Weekend", "Weekday",
              "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December", "Lobby", "Drive-up", "Drive", "Thru", "Hours", "Branch", "Dupaco's", "Credit",
              "Union", "Is", "Was", "Will", "Does", "Did", "Could", "Would", "Should", "Your", "Its", "A", "An"}


def _link(m: re.Match) -> str:
    text, url = m.group(1).strip(), m.group(2).strip()
    # Page links stay readable ("Join Dupaco (https://join.dupaco.com/)"); asset links (images, PDFs) and
    # links to the same text keep just their text, so stray file names and dates don't leak into answers.
    if re.search(r"\.(jpe?g|png|gif|svg|pdf|xlsx?)(\?|$)|/hubfs/|^tel:|^mailto:", url, re.I) or not url.startswith("http"):
        return text
    return f"{text} ({url})" if text else url


PHONE_RE = re.compile(r"(?<![\d.])(?:1[.\-\s])?\(?(\d{3})\)?[.\-\s](\d{3})[.\-](\d{4})(?![\d])")


def clean(s: str) -> str:
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = PHONE_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}", s)    # 1.800.383.1700 -> 800-383-1700
    s = re.sub(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)", _link, s)
    s = re.sub(r"\*\*|__|^#+\s*|^[-*]\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def sentences(text: str) -> list[str]:
    # Table rows are answered by the rate logic (with its currency rules), never quoted as free text.
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("|"))
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
    if re.search(r"\bphone\b|\bcall\b|\btelephone\b", low) and not re.search(r"routing|account number", low):
        return "phone"
    if re.search(r"\b(routing|account|nmls|fax|tax id) number\b|\brouting\b", low):
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
        if re.search(r"\baddress(es)?\b|\bwhere(?: is|'s|’s| are| can i find)\b|\blocated\b|\blocations? of\b", low) and re.search(
                r"branch|office|location", low):
            r = self.address(q, view)
            if r:
                return r
        r = self.contact_fact(q, view)
        if r:
            return r
        if re.search(r"\bfees?\b|\bcharges?\b|\bcost\b|\bhow much (is|does|do|for|to)\b|\bprice\b", low) and not re.search(
                r"\b(rate|apr|apy|interest|dividend|down payment|grant|scholarship|assistance)\b", low):
            r = self.fee(q, view)
            if r:
                return r
        if re.search(r"\bhow many\b|\bnumber of\b", low):
            r = self.count(q, view)
            if r:
                return r
        return self.best_sentences(q, view)

    # ------------------------------------------------------------------ sentence retrieval
    def ranked_sentences(self, q: Query, view, top_units: int = 40, kinds=("passage",)):
        if answer_type(q.raw) in ("phone", "id"):
            top_units = max(top_units, 150)     # numbers often sit in long footnotes that rank low as passages
        hits = self.s.index.search(q.toks, top=top_units, allow=view.units)
        if not hits:
            return []
        idf = self.s.index.idf
        qset = {t for t in q.toks if t not in GENERIC_Q} or set(q.toks)
        qmass = sum(idf.get(t, 1.0) for t in qset) or 1.0
        top = hits[0][0] or 1.0
        atype = answer_type(q.raw)
        if atype in ("phone", "id"):
            # "phone number", "call": the answer type itself (any sentence with a number satisfies it), not the topic
            qset = (qset - set(tokens("phone number call telephone reach contact dial ring"))) or qset
            qmass = sum(idf.get(t, 1.0) for t in qset) or 1.0
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
                    cov_bonus = 0.3 + (0.3 if atype in ("id", "phone") and u.page_kind == "contact" else 0.0)
                else:
                    cov_bonus = 0.0
                carried = 0.8 if uid in view.carried else 1.0
                if anchor and anchor not in st and anchor not in ctx:
                    carried *= 0.6
                score = carried * (cov + cov_bonus + CTX_W * ctx_cov + PATH_W * path_cov + 0.2 * sc / top + 0.04 * u.authority
                         + 0.03 * rec)
                grounded = not anchor or anchor in st or anchor in ctx
                out.append((score, cov, max(cov, min(1.0, cov + ctx_cov)) if grounded else 0.0, u, k, s))
        out.sort(key=lambda x: (-x[0], x[3].uid, x[4]))
        return out

    def best_sentences(self, q: Query, view) -> Optional[dict]:
        # A key word the site never uses at all ("SWIFT code") means the site can't answer it.
        idf = self.s.index.idf
        unknown = [t for t in q.toks if t not in GENERIC_Q and t.isalpha() and len(t) >= 4 and t not in idf]
        if unknown and len([t for t in q.toks if t not in GENERIC_Q]) <= 6:
            return {"refuse": f"site never mentions {unknown}"}
        # Sentences that don't mention the question's named entity can't answer it (joint == 0).
        ranked = [r for r in self.ranked_sentences(q, view) if r[2] > 0]
        if not ranked:
            return None
        score, cov, joint, u, k, s = ranked[0]
        if joint < 0.3:
            return None
        # What's true now: among near-equal candidates, prefer the freshest page (undated product pages are
        # current; an article counts from its publish date).
        def fresh(r):
            return r[3].published.toordinal() if r[3].published else view.taken_on.toordinal()
        near = [r for r in ranked if r[0] >= 0.85 * score and r[2] >= 0.3]
        if near:
            top = max(near, key=lambda r: (fresh(r), r[0]))
            if fresh(top) - fresh(ranked[0]) > 180:
                score, cov, joint, u, k, s = top
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

    # ------------------------------------------------------------------ contact facts
    CONTACT_WORDS = set(tokens("phone number call main general contact reach address mailing mail routing aba "
                               "what is the dupaco credit union s my for to i do can"))

    def contact_fact(self, q: Query, view) -> Optional[dict]:
        """General contact questions ('main phone number', 'mailing address', 'routing number') are answered from
        the contact page's labelled fields. Questions about a specific team or branch go to normal retrieval."""
        low = q.raw.lower()
        team = re.search(r"\b(mortgage|card|visa|insurance|business|loan|lending|team|department|investment|"
                         r"financial services|wealth|branch|office|fraud|lost|stolen|pin)\b", low)
        field = ("Routing Number" if re.search(r"routing|\baba\b", low) else
                 "Phone" if re.search(r"phone|\bcall\b|telephone", low) and not team else
                 "Address" if re.search(r"mailing|\bmail\b|address", low) and not re.search(r"branch|office|location|change|update", low) else None)
        if not field:
            return None
        if field == "Address" and set(q.toks) - self.CONTACT_WORDS:
            return None
        for uid in sorted(view.units):
            u = self.s.units[uid]
            if u.page_kind != "contact" or u.kind != "passage":
                continue
            lines = [clean(l) for l in u.text.split("\n") if l.strip()]
            for i, line in enumerate(lines):
                m = re.match(rf"^{field}:\s*(.+)$", line, re.I)
                if m:
                    val = m.group(1)
                    if field == "Address" and i + 1 < len(lines) and re.search(r"\b\d{5}\b", lines[i + 1]):
                        val = f"{val}, {lines[i + 1]}"
                    return {"text": f"{field}: {val}", "units": [u]}
        return None

    # ------------------------------------------------------------------ fees
    FEE_LINE = re.compile(r"^(?P<item>[A-Za-z(][^$\n]{2,110}?)\s*[-:–]?\s*(?P<amt>\$\s?\d[\d,]*(?:\.\d\d)?\*?[^\n]{0,60}|Free\b[^\n]{0,60}|"
                          r"\d+(?:\.\d+)?%\*?\s+of[^\n]{0,40}|Price varies)$")
    FEE_GENERIC = set(tokens("fee fees charge charges cost costs how much is does do for a an the to of what price "
                             "dupaco there each per item"))

    def fee_lines(self, view) -> list[tuple[Unit, str, str, str]]:
        cache = self.__dict__.setdefault("_fee_cache", {})
        if view.snapshot_id not in cache:
            out = []
            for uid in sorted(view.units):
                u = self.s.units[uid]
                if u.kind != "passage" or u.page_kind in ("article", "listing", "calculator"):
                    continue
                for line in u.text.split("\n"):
                    line = clean(line)
                    m = self.FEE_LINE.match(line) if len(line) < 180 else None
                    if m and not re.search(r"%\s*(apy|apr)|\bapy\b|\bapr\b", line, re.I):
                        out.append((u, line, m.group("item").strip(" -:"), m.group("amt").strip()))
            cache[view.snapshot_id] = out
        return cache[view.snapshot_id]

    def fee(self, q: Query, view) -> Optional[dict]:
        """Fee questions are answered from fee-schedule lines ('Stop payment (check, ACH, & bill pay) $30.00* per item'):
        the single best-matching line, never its neighbours (they are other fees)."""
        idf = self.s.index.idf
        want = [t for t in q.toks if t not in self.FEE_GENERIC]
        if not want:
            return None
        mass = sum(idf.get(t, 1.0) for t in want)
        best = None
        for u, line, item, amt in self.fee_lines(view):
            it = set(tokens(item))
            cov = sum(idf.get(t, 1.0) for t in want if t in it) / mass
            extra = len(it - set(want))          # prefer the most specific item name
            key = (cov, -extra, u.authority, -u.uid)
            if cov >= 0.6 and (best is None or key > best[0]):
                best = (key, u, item, amt)
        multi = re.search(r"\bfees\b|,|\band\b", q.raw.lower())
        if multi:
            # "wire fees (incoming, domestic and international)": every line naming the main thing asked about
            head = max(want, key=lambda t: sum(t in tokens(it) for _, _, it, _ in self.fee_lines(view)) * idf.get(t, 1.0))
            lines = []
            for u, line, item, amt in self.fee_lines(view):
                if head in tokens(item) and (best is None or u.url == best[1].url):
                    lines.append(f"{item}: {re.sub(r'[*]', '', amt).strip()}")
            lines = list(dict.fromkeys(lines))
            if 1 < len(lines) <= 6:
                src = best[1] if best else next(u for u, _, it, _ in self.fee_lines(view) if head in tokens(it))
                return {"text": "; ".join(lines) + ".", "units": [src]}
        if not best:
            return None
        _, u, item, amt = best
        amt = re.sub(r"\*", "", amt).strip()
        return {"text": f"{item}: {amt}.", "units": [u]}

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
        for cand in [place] + (place.split() if " " in place else []):
            r = self._address_for(cand, q, view)
            if r and "refuse" not in r:
                return r
        return {"refuse": f"no address for {place}"}

    def _address_for(self, place: str, q: Query, view) -> Optional[dict]:
        pl = place.lower()
        # 1. The branch locator: an address line under a "Dupaco Branch" entry (not an ATM / shared branch).
        #    Read the whole contact page in order, since an entry can straddle two passages.
        contact = [self.s.units[i] for i in sorted(view.units)
                   if self.s.units[i].kind == "passage" and self.s.units[i].page_kind == "contact"]
        lines = [clean(l) for u in contact for l in u.text.split("\n") if l.strip()]
        if re.search(r"\bbranches\b|\blocations\b", q.raw.lower()):
            # "branches in Dubuque": every branch entry whose address is in that town
            found = [ADDRESS_RE.search(l).group(0) for i, l in enumerate(lines)
                     if i and pl in l.lower() and ADDRESS_RE.search(l) and "branch" in lines[i - 1].lower()
                     and "atm" not in lines[i - 1].lower() and "shared" not in lines[i - 1].lower()]
            found = list(dict.fromkeys(found))
            if found:
                return {"text": f"Dupaco branches in {place}: " + "; ".join(found) + ".", "units": contact[:1]}
        for i, line in enumerate(lines):
            if pl in line.lower() and ADDRESS_RE.search(line):
                label = lines[i - 1].lower() if i else ""     # the entry type sits right above its address
                if "branch" in label and "atm" not in label and "shared" not in label:
                    return {"text": f"The {place} branch is at {ADDRESS_RE.search(line).group(0)}.", "units": contact[:1]}
        # 2. A sentence that names the place as a Dupaco branch and gives a street address.
        for uid in sorted(view.units):
            u = self.s.units[uid]
            if u.kind != "passage" or pl not in u.text.lower():
                continue
            for sent in sentences(u.text):
                low = sent.lower()
                if pl in low and "branch" in low and (ADDRESS_RE.search(sent) or LOOSE_ADDRESS_RE.search(sent)):
                    return {"text": sent, "units": [u]}
        return {"refuse": f"no address for {place}"}

    # ------------------------------------------------------------------ counts
    def count(self, q: Query, view) -> Optional[dict]:
        """'How many X': the same count is restated in many dated articles; the newest one wins."""
        ranked = self.ranked_sentences(q, view, top_units=200)
        m = re.search(r"\bhow many (\w+)|\bnumber of (\w+)", q.raw.lower())
        counted = tokens(m.group(1) or m.group(2)) if m else []
        nouns = counted or [t for t in q.toks if t not in GENERIC_Q and not t.isdigit()]
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

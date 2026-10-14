"""A small BM25 index (stdlib only) plus the tokenizer shared by the whole system."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

STOP = set("""
a an and are as at be by can do does for from has have how i in is it its me my of on or our please
the their there this to was what when where which who why will with you your yours we us dupaco
tell give get know want would like could should any much many current currently right now today
if over also just still
""".split())

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")

# Light normalisation so "certificates"/"certificate", "cd"/"certificate", "months"/"month" meet.
SYNONYMS = {
    "cd": "certificate", "cds": "certificate", "certificates": "certificate", "certificat": "certificate",
    "mo": "month", "months": "month", "mos": "month", "yr": "year", "years": "year",
    "mortgages": "mortgage", "loans": "loan", "rates": "rate", "branches": "branch", "accounts": "account",
    "cards": "card", "hrs": "hours", "visa": "visa", "apys": "apy", "aprs": "apr", "fees": "fee",
    "savings": "saving", "checking": "checking", "dividends": "dividend", "members": "member",
    "vehicles": "vehicle", "cars": "auto", "car": "auto", "automobile": "auto", "atms": "atm",
    "locations": "location", "employees": "employee", "students": "student", "wallets": "wallet",
}


def stem(t: str) -> str:
    """Tiny suffix stripper: close/closes/closed/closing/closure all become "clos"."""
    if not t.isalpha() or len(t) <= 3:
        return t
    for suf, minlen in (("izing", 5), ("ized", 5), ("izes", 5), ("ize", 5), ("ition", 5), ("ations", 5), ("ation", 5), ("ings", 5), ("ing", 5), ("ures", 5), ("ure", 5), ("ies", 4),
                        ("ed", 4), ("es", 5), ("s", 3)):
        if t.endswith(suf) and len(t) - len(suf) >= minlen - 1:
            if suf == "s" and t.endswith(("ss", "us", "is")):
                continue
            t = t[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    if len(t) > 3 and t.endswith("e"):
        t = t[:-1]
    return t


_TOKEN_CACHE: dict[str, str] = {}


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    text = re.sub(r"(?<=[a-z])\$(?=[a-z])", "s", (text or "").lower())    # Home$tart -> homestart
    for t in TOKEN_RE.findall(text.replace("’", "'")):
        t = t.strip(".'")
        if t.endswith("'s"):
            t = t[:-2]
        if not keep_stop and t in STOP:
            continue
        if "." in t and not t.replace(".", "").isdigit():
            # host names and file names: "join.dupaco.com" -> join, dupaco, com
            out.extend(tokens(t.replace(".", " "), keep_stop))
            continue
        st = _TOKEN_CACHE.get(t)
        if st is None:
            st = _TOKEN_CACHE[t] = stem(SYNONYMS.get(t, t))
        out.append(st)
    return out


class BM25:
    def __init__(self, k1: float = 1.2, b: float = 0.6):
        self.k1, self.b = k1, b
        self.docs: list[Counter] = []
        self.lens: list[int] = []
        self.post: dict[str, list[int]] = defaultdict(list)
        self.idf: dict[str, float] = {}
        self.avg = 1.0

    def add(self, toks: list[str]) -> int:
        c = Counter(toks)
        i = len(self.docs)
        self.docs.append(c)
        self.lens.append(len(toks))
        for t in c:
            self.post[t].append(i)
        return i

    def finalize(self) -> None:
        n = len(self.docs) or 1
        self.avg = (sum(self.lens) / n) or 1.0
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def search(self, q: list[str], top: int = 50, allow: set[int] | None = None) -> list[tuple[float, int]]:
        scores: dict[int, float] = defaultdict(float)
        for t in set(q):
            idf = self.idf.get(t)
            if not idf:
                continue
            for i in self.post[t]:
                if allow is not None and i not in allow:
                    continue
                tf = self.docs[i][t]
                scores[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
        return sorted(((s, i) for i, s in scores.items()), key=lambda x: (-x[0], x[1]))[:top]

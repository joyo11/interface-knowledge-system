"""Versioned store: every snapshot keeps its own view, identical content is parsed and indexed once."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .parse import AUTHORITY, Table, extract_tables, page_kind, passages, prose_rate_tables, publish_date
from .search import BM25, tokens


@dataclass
class Unit:
    """A searchable piece of one page version: a passage or a table."""
    uid: int
    url: str
    kind: str                       # "passage" | "table"
    page_kind: str
    title: str
    heading: str
    text: str
    published: Optional[date]
    table: Optional[Table] = None
    snapshots: set[str] = field(default_factory=set)

    @property
    def authority(self) -> int:
        return AUTHORITY.get(self.page_kind, 1)


@dataclass
class SnapshotView:
    snapshot_id: str
    taken_on: date
    pages: dict[str, str]                      # url -> page hash
    units: set[int] = field(default_factory=set)
    # Pages not in this download but seen before (crawls miss pages): kept as a low-priority fallback.
    carried: set[int] = field(default_factory=set)


def is_rate_table(u: "Unit") -> bool:
    """A table that publishes rates: some cell holds a percentage and the header/heading names a rate."""
    t = u.table
    if not any("%" in c for r in t.rows for c in r):
        return False
    names = " ".join(t.header) + " " + u.heading + (" " + t.rows[0][0] if t.prose else "")
    return re.search(r"rate|apr|apy|yield|dividend", names, re.I) is not None


def table_text(t: Table) -> str:
    head = " | ".join(t.header)
    rows = "\n".join(" | ".join(r) for r in t.rows)
    return f"{t.context}\n{head}\n{rows}"


class Store:
    def __init__(self):
        self.units: list[Unit] = []
        self._unit_by_key: dict[tuple, int] = {}
        self._page_units: dict[str, list[int]] = {}    # page hash -> unit ids
        self.page_meta: dict[str, dict] = {}            # page hash -> {url,title,kind,published}
        self.views: list[SnapshotView] = []
        self.index = BM25()
        self._dirty = False
        self.table_vocab: set[str] = set()

    # ------------------------------------------------------------------ ingest
    def add_snapshot(self, snapshot) -> SnapshotView:
        view = SnapshotView(snapshot.snapshot_id, date.fromisoformat(snapshot.taken_on[:10]), {})
        for p in snapshot.pages:
            h = hashlib.sha1((p.url + "\0" + p.markdown).encode("utf-8")).hexdigest()
            view.pages[p.url] = h
            if h not in self._page_units:
                self._page_units[h] = self._parse_page(p, h)
            for uid in self._page_units[h]:
                self.units[uid].snapshots.add(view.snapshot_id)
                view.units.add(uid)
        prev = self.views[-1] if self.views else None
        if prev:
            last = {**{url: h for url, h in getattr(prev, "_all_pages", prev.pages).items()}}
            for url, h in last.items():
                if url not in view.pages:
                    for uid in self._page_units[h]:
                        if uid not in view.units:
                            view.carried.add(uid)
            view._all_pages = {**last, **view.pages}
        else:
            view._all_pages = dict(view.pages)
        view.units |= view.carried
        self.views.append(view)
        self.__dict__.pop("_table_cache", None)
        self.views.sort(key=lambda v: v.taken_on)
        self._dirty = True
        return view

    def _parse_page(self, p, h: str) -> list[int]:
        kind = page_kind(p.url)
        pub = publish_date(p.url, p.markdown) if kind in ("article", "document", "guide") else None
        self.page_meta[h] = {"url": p.url, "title": p.title, "kind": kind, "published": pub}
        title = (p.title or "").replace(" | Dupaco", "").strip()
        ids = []
        for ps in passages(p.markdown, title):
            ids.append(self._unit(p.url, "passage", kind, title, ps.heading, ps.text, pub, None))
        tables = extract_tables(p.markdown)
        if kind not in ("article", "listing", "calculator"):
            # Rates stated in sentences count too, but only on product/rate/promo pages (never blog prose).
            tables += prose_rate_tables(p.markdown)
        for t in tables:
            ids.append(self._unit(p.url, "table", kind, title, t.context, table_text(t), pub, t))
        return ids

    def _unit(self, url, kind, page_kind_, title, heading, text, pub, table) -> int:
        eff = table.effective if table else None
        key = (url, kind, heading, text, eff, pub)
        uid = self._unit_by_key.get(key)
        if uid is not None:
            return uid
        uid = len(self.units)
        self.units.append(Unit(uid, url, kind, page_kind_, title, heading, text, pub, table))
        self._unit_by_key[key] = uid
        # Title and heading are indexed with the body so "Bright Savings" finds the Bright Savings table.
        self.index.add(tokens(f"{title} {heading} {heading} {text}"))
        return uid

    def finalize(self) -> None:
        if self._dirty:
            self.index.finalize()
            # Words that occur in some rate table: only these can tell rate tables apart.
            # Only rate tables, and only their names and row labels (not footnotes, forms or calculators).
            self.table_vocab = set()
            for u in self.units:
                if u.kind == "table" and is_rate_table(u):
                    labels = " ".join(c for r in u.table.rows for c in r if "%" not in c and not re.search(r"\d{3}", c))
                    self.table_vocab.update(tokens(f"{u.heading} {labels}"))
            self._dirty = False

    def table_units(self, view: SnapshotView) -> set[int]:
        key = view.snapshot_id
        cache = self.__dict__.setdefault("_table_cache", {})
        if key not in cache:
            cache[key] = {u for u in view.units if self.units[u].kind == "table" and is_rate_table(self.units[u])}
        return cache[key]

    # ------------------------------------------------------------------ views
    def view_for(self, as_of: Optional[str]) -> SnapshotView:
        """Latest snapshot taken on or before `as_of`; the newest one when no date is given."""
        if not as_of:
            return self.views[-1]
        try:
            d = date.fromisoformat(str(as_of).strip()[:10])
        except ValueError:
            return self.views[-1]
        eligible = [v for v in self.views if v.taken_on <= d]
        return eligible[-1] if eligible else self.views[0]

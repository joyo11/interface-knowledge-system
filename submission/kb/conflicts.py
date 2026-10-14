"""Find where the site contradicts itself, pick the winner, and say why.

Two kinds of contradiction are reported (a deliberately short, high-precision list):
  1. Rate tables: the same product/tier shows different values on different pages of one snapshot
     (rate sheet vs calculator / promo landing page / guide). Winner = newest "effective as of" date,
     then page authority. Different tiers, terms and rate-vs-APY columns are never compared.
  2. Article boilerplate: a count restated in many press releases ("23 branch office locations") with
     different values. Winner = the most recently published article.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .rates import _label_cells, _row_amount_range, _row_model_range, _row_term_months, _same_product, _shape, _shapes, rate_columns
from .store import Store, Unit

MAX_RATE_CONFLICTS = 40


def _families(store: Store, units: list[Unit]) -> list[list[Unit]]:
    fams: list[list[Unit]] = []
    for u in sorted(units, key=lambda x: (-x.authority, x.uid)):
        lab = [" ".join(r[:2]) for r in u.table.rows]
        strong = {x for x in (_shape(l) for l in lab) if re.search(r"[a-z]{3}", x)}
        weak = set().union(*(_shapes(l, u.heading) for l in lab)) if lab else set()
        for f in fams:
            h = f[0]
            hl = [" ".join(r[:2]) for r in h.table.rows]
            hs = {x for x in (_shape(l) for l in hl) if re.search(r"[a-z]{3}", x)}
            hw = set().union(*(_shapes(l, h.heading) for l in hl)) if hl else set()
            same_side = ("business" in u.url + u.heading.lower()) == ("business" in h.url + h.heading.lower())
            if any(x.url == u.url for x in f):
                continue    # two tables on one page are two products, not two copies
            if same_side and (len(strong & hs) >= 2 or ((strong & hs or weak & hw) and _same_product(u, h))):
                f.append(u)
                break
        else:
            fams.append([u])
    return [f for f in fams if len(f) > 1]


FLAG_WORDS = ("jumbo", "special", "refinanc", "purchase", "roth", "traditional", "intro", "business", "standard",
              "heloc", "loan", "rewards", "platinum")


def _rows(u: Unit) -> list[tuple[str, tuple, dict[str, str]]]:
    """Rows with an alignment key: (term, balance/loan tier, qualifier words, model-year slot or name)."""
    out = []
    head_words = set(re.findall(r"[a-z]+", u.heading.lower()))
    for r in u.table.rows:
        cols = rate_columns(u.table.header, r, None)
        if not cols:
            continue
        label = " ".join(_label_cells(u.table.header, r))
        low = label.lower().replace("–", "-")
        term = _row_term_months(low)
        amt = _row_amount_range(low)
        amt_key = (amt.lo, amt.hi) if amt else None
        flags = tuple(w for w in FLAG_WORDS if w in low)
        mr = _row_model_range(low)
        if mr:
            slot = ("my", mr.lo, mr.hi)
        elif term is None and amt_key is None:
            words = [w for w in re.findall(r"[a-z]+", low) if w not in head_words]
            slot = ("name", " ".join(words))
        else:
            slot = None
        out.append((label, (term, amt_key, flags, slot), {n or "rate": v for n, v in cols}))
    return out


def _topic(u: Unit, label: str, metric: str) -> str:
    product = u.heading.split(" / ")[0].strip()
    if not product or len(product) > 60 or set(re.findall(r"[a-z]+", product.lower())) <= set(re.findall(r"[a-z]+", label.lower())):
        return f"{label} {metric}".strip()
    return f"{label} {metric} ({product})".strip()


def _aligned(a: tuple, b: tuple) -> bool:
    """Same term, tier and qualifiers; model-year buckets may have rolled forward by a year."""
    if a[:3] != b[:3]:
        return False
    sa, sb = a[3], b[3]
    if sa and sb and sa[0] == sb[0] == "my":
        return abs(sa[1] - sb[1]) <= 1 and abs(sa[2] - sb[2]) <= 1
    return sa == sb


def _norm_val(v: str) -> str:
    m = re.findall(r"\d+(?:\.\d+)?", v)
    return "|".join(f"{float(x):.3f}" for x in m)


def rate_conflicts(store: Store) -> list[dict]:
    seen = set()
    out = []
    for view in reversed(store.views):          # newest snapshot first: those are the live contradictions
        tables = [store.units[i] for i in store.table_units(view)]
        tables = [u for u in tables if re.search(r"rate|apr|apy|yield|dividend", " ".join(u.table.header) + u.heading, re.I)]
        for fam in _families(store, tables):
            rank = lambda u: (u.table.effective or date.min, u.authority)
            win = max(fam, key=lambda u: (rank(u), -u.uid))
            wrows = _rows(win)
            for lu in fam:
                if lu is win:
                    continue
                for llabel, lkey, lvals in _rows(lu):
                    match = next((w for w in wrows if _aligned(w[1], lkey)), None)
                    if not match:
                        continue
                    wlabel, _, wvals = match
                    pick = next((m for m in ("APY", "APR") if m in lvals and m in wvals), None)
                    for metric, lv in ([(pick, lvals[pick])] if pick else lvals.items()):
                        wv = wvals.get(metric)
                        if not wv or _norm_val(wv) == _norm_val(lv):
                            continue
                        topic = _topic(win, wlabel, metric)
                        key = (lu.url, re.sub(r"\W+", " ", llabel.lower()), metric)
                        if key in seen:
                            continue
                        seen.add(key)
                        weff = win.table.effective.strftime("%m/%d/%Y") if win.table.effective else "undated"
                        leff = lu.table.effective.strftime("%m/%d/%Y") if lu.table.effective else "undated"
                        out.append({
                            "topic": topic,
                            "values": [lv, wv],
                            "sources": [(lu.url, view.snapshot_id), (win.url, view.snapshot_id)],
                            "resolution": wv,
                            "reason": (f"{win.url} ({win.page_kind}, rates effective {weff}) is newer/more authoritative "
                                       f"than {lu.url} ({lu.page_kind}, effective {leff}), which still shows the old value"),
                            "_prio": (view is store.views[-1], win.authority - lu.authority),
                        })
    out.sort(key=lambda c: (not c["_prio"][0], -c["_prio"][1]))
    return out[:MAX_RATE_CONFLICTS]


# Facts restated in press-release boilerplate ("ABOUT DUPACO"): the newest article carries the current figure.
COUNT_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})*)\s+(branch office locations)\b|membership (?:totals|of) more than (\d{1,3}(?:,\d{3})+)|"
                      r"assets (?:exceeding|of more than) \$(\d+(?:\.\d+)?) billion", re.I)
ORDINAL_RE = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth) time (Forbes) (?:has )?(?:recognized|named|ranked)", re.I)


def boilerplate_conflicts(store: Store) -> list[dict]:
    view = store.views[-1]
    facts: dict[str, list[tuple[date, str, str]]] = defaultdict(list)
    for uid in sorted(view.units):
        u = store.units[uid]
        if u.kind != "passage" or not u.published:
            continue
        for m in COUNT_RE.finditer(u.text):
            if m.group(1):
                facts["branch office locations"].append((u.published, m.group(1), u.url))
            elif m.group(3):
                facts["members (membership total)"].append((u.published, m.group(3), u.url))
            elif m.group(4):
                facts["billion in assets"].append((u.published, "$" + m.group(4), u.url))
        for m in ORDINAL_RE.finditer(u.text):
            facts[f"times recognized by {m.group(2)}"].append((u.published, m.group(1).lower(), u.url))
    out = []
    for noun, obs in facts.items():
        vals = {v for _, v, _ in obs}
        if len(vals) < 2:
            continue
        obs.sort(key=lambda x: (x[0], x[2]))
        newest = obs[-1]
        latest_by_val = {}
        for d, v, url in obs:
            latest_by_val[v] = (d, url)
        ordered = sorted(latest_by_val, key=lambda v: latest_by_val[v][0])
        values = [f"{v} {noun}" if not noun.startswith("times") else v for v in ordered]
        out.append({
            "topic": f"Dupaco {noun}" if not noun.startswith("times") else f"Number of {noun}",
            "values": values,
            "sources": [(latest_by_val[v][1], view.snapshot_id) for v in ordered],
            "resolution": values[-1],
            "reason": (f"the most recently published page ({newest[0].isoformat()}, {newest[2]}) states the "
                       f"current figure; older articles repeat outdated boilerplate"),
        })
    return out


MAX_TEMPORAL = 30


def temporal_conflicts(store: Store) -> list[dict]:
    """The official rate sheet changed a value between snapshots: older answers (and old copies) are wrong now.
    One entry per product row, headline metric only (APY for deposits, APR for loans)."""
    hist: dict[tuple, list] = {}
    for view in store.views:
        for uid in store.table_units(view):
            u = store.units[uid]
            if u.authority < 4 or "business" in u.url:
                continue
            product = (u.heading.split(" / ")[-1].strip() or u.title)
            for label, key, vals in _rows(u):
                metric = next((m for m in ("APY", "APR") if m in vals), None) or next(iter(vals))
                k = (u.url, u.heading.split(" / ")[-1].lower(), label.lower(), metric)
                hist.setdefault(k, []).append((view, vals[metric], product, label, u.url))
    out = []
    for (url, _, _, metric), obs in hist.items():
        distinct = []
        for view, v, product, label, u in obs:
            if not distinct or _norm_val(distinct[-1][1]) != _norm_val(v):
                distinct.append((view, v))
        if len(distinct) < 2:
            continue
        product, label = obs[-1][2], obs[-1][3]
        latest_view, latest_v = distinct[-1]
        hist_txt = ", ".join(f"{v} ({vw.taken_on.isoformat()})" for vw, v in distinct)
        out.append({
            "topic": f"{label} {metric} (changed over time)",
            "values": [v for _, v in distinct],
            "sources": [(url, vw.snapshot_id) for vw, _ in distinct],
            "resolution": latest_v,
            "reason": f"the rate sheet changed between downloads: {hist_txt}; the newest download is current "
                      f"(earlier values are only right for 'as of' questions)",
            "_prio": (latest_view is store.views[-1], len(distinct)),
            "_family": product,
        })
    out.sort(key=lambda c: (not c["_prio"][0], -c["_prio"][1], c["topic"]))
    # Round-robin across products so one busy table (mortgages) doesn't fill the whole list.
    by_fam: dict[str, list] = defaultdict(list)
    for c in out:
        by_fam[c["_family"]].append(c)
    picked = []
    while len(picked) < MAX_TEMPORAL and any(by_fam.values()):
        for fam in list(by_fam):
            if by_fam[fam]:
                picked.append(by_fam[fam].pop(0))
    for c in picked:
        c.pop("_family", None)
    return picked[:MAX_TEMPORAL]


def _merge(conflicts: list[dict]) -> list[dict]:
    """Same topic and same values reported from several stale pages -> one entry listing every source."""
    merged: dict[tuple, dict] = {}
    for c in conflicts:
        key = (re.sub(r"\W+", " ", c["topic"].lower()), tuple(_norm_val(v) for v in c["values"]))
        if key in merged:
            m = merged[key]
            m["sources"] += [s for s in c["sources"] if s not in m["sources"]]
        else:
            merged[key] = c
    return list(merged.values())


def find_conflicts(store: Store) -> list[dict]:
    out = _merge(rate_conflicts(store)) + boilerplate_conflicts(store) + _merge(temporal_conflicts(store))
    for c in out:
        c.pop("_prio", None)
    return out

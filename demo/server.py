"""Local walkthrough UI for the knowledge system. Standard library only.

    python3 demo/server.py        ->  http://localhost:8787

It loads the exact graded code in submission/ (unchanged) and adds read-only
"explain" views on top: which copy won, which stale copies lost, and why.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "submission"))

import ast  # noqa: E402
import re  # noqa: E402

from run_eval import load_snapshots, load_submission  # noqa: E402
from kb.engine import _wants_minimum  # noqa: E402
from kb.qparse import parse_question  # noqa: E402
from kb.rates import candidate_groups, rate_columns  # noqa: E402
from kb.search import tokens  # noqa: E402

SUB = ROOT / "submission"
# Files the module explorer may show (read-only). Anything else is refused.
SOURCE_FILES = {"solution.py": SUB / "solution.py",
                **{f"kb/{p.name}": p for p in (SUB / "kb").glob("*.py") if p.name != "__init__.py"}}

PORT = 8787
HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

print("loading snapshots ...", flush=True)
t0 = time.perf_counter()
SNAPS = load_snapshots(ROOT / "snapshots")
KS = load_submission(ROOT / "submission")
for s in SNAPS:
    KS.ingest(s)
INGEST_S = time.perf_counter() - t0
CONFLICTS = [asdict(c) for c in KS.conflicts()]
print(f"ingested {len(SNAPS)} snapshots in {INGEST_S:.2f}s, {len(CONFLICTS)} conflicts", flush=True)


def _eff(t):
    e = getattr(t, "effective", None)
    return e.isoformat() if e else None


def _unit_info(u, rows, metric):
    t = u.table
    out_rows = []
    for r in rows[:6]:
        cols = rate_columns(t.header, r.row, metric) if t else []
        out_rows.append({"label": r.label, "values": [f"{v} {n}".strip() if n and n.lower() not in v.lower() else v for n, v in cols]})
    return {"url": u.url, "page_kind": u.page_kind, "authority": u.authority,
            "effective": _eff(t) if t else None, "rows": out_rows}


def explain(question: str, as_of: str | None) -> dict:
    """Read-only trace of the rate decision: the winning table and the copies it beat."""
    q = parse_question(question, as_of)
    view = KS.store.view_for(as_of or q.as_of_text)
    parsed = {"term_months": q.term_months, "amount": q.amount, "metric": q.metric,
              "flags": sorted(q.flags), "rate_intent": bool(q.rate_intent),
              "as_of_text": q.as_of_text}
    out = {"snapshot": view.snapshot_id, "parsed": parsed, "winner": None, "losers": []}
    if not (q.rate_intent or q.term_months):
        return out
    groups = candidate_groups(KS.store, view, q)
    if not groups:
        return out
    g = groups[0]
    out["winner"] = _unit_info(g.winner, sorted(g.rows, key=lambda r: -r.score), q.metric)
    out["losers"] = [_unit_info(u, rr, q.metric) for u, rr in g.losers[:5]]
    return out


def ask(question: str, as_of: str | None) -> dict:
    t = time.perf_counter()
    a = KS.answer(question, as_of=as_of)
    ms = (time.perf_counter() - t) * 1000
    try:
        why = explain(question, as_of)
    except Exception as e:  # explain is a demo add-on; never break the answer
        why = {"error": str(e)}
    return {"question": question, "as_of": as_of, "ms": round(ms, 1),
            "answer": asdict(a), "why": why}


def timeline(question: str) -> list[dict]:
    out = []
    for s in SNAPS:
        a = KS.answer(question, as_of=s.taken_on)
        out.append({"snapshot": s.snapshot_id, "taken_on": s.taken_on, "text": a.text,
                    "confident": a.confident})
    return out


def source(file: str, names: str) -> dict:
    """Exact source of named top-level functions/classes/assignments (or Class.method) in a submission file."""
    path = SOURCE_FILES.get(file)
    if path is None:
        return {"error": "unknown file"}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    out = []
    for name in [n for n in names.split(",") if n]:
        cls, _, meth = name.partition(".")
        node = None
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == cls:
                node = n
            elif isinstance(n, ast.Assign) and any(getattr(t, "id", None) == cls for t in n.targets):
                node = n
        if node is not None and meth:
            node = next((m for m in getattr(node, "body", []) if isinstance(m, ast.FunctionDef) and m.name == meth), None)
        if node is None:
            continue
        start = (node.decorator_list[0].lineno if getattr(node, "decorator_list", None) else node.lineno)
        out.append({"name": name, "start": start, "code": "\n".join(lines[start - 1:node.end_lineno])})
    return {"file": file, "total_lines": len(lines), "blocks": out}


import threading  # noqa: E402

_TRACE_LOCK = threading.Lock()
TEXT_HANDLERS = ("hours", "address", "contact_fact", "fee", "count", "best_sentences")


def _which_text_handler(q, view) -> str | None:
    """Wrap TextAnswerer's handlers for one call and note the one whose result became the answer."""
    t = KS.engine.t
    last = {"name": None}
    with _TRACE_LOCK:
        originals = {n: getattr(t, n) for n in TEXT_HANDLERS}

        def wrap(name, fn):
            def inner(*a, **k):
                r = fn(*a, **k)
                if r:
                    last["name"] = name
                return r
            return inner
        try:
            for n, fn in originals.items():
                setattr(t, n, wrap(n, fn))
            t.answer(q, view)
        finally:
            for n in originals:
                delattr(t, n)          # drop the instance override, back to the class method
    return last["name"]


def trace(question: str, as_of: str | None) -> dict:
    """Follow one question through every module, mirroring Engine.answer's branch order exactly."""
    st, eng = KS.store, KS.engine
    q = parse_question(question, as_of)
    view = st.view_for(as_of or q.as_of_text)
    out: dict = {"question": question}

    # store: which download, how much is in it
    out["store"] = {"snapshot": view.snapshot_id, "taken_on": view.taken_on.isoformat(), "pages": len(view.pages),
                    "units": len(view.units), "carried": len(view.carried), "unique_units_all": len(st.units),
                    "downloads": len(st.views), "picked_by": "date in the question" if (not as_of and q.as_of_text)
                    else ("the date you chose" if as_of else "no date, so the newest download")}

    # qparse: the structured question
    amt = q.amount
    out["qparse"] = {"term_months": q.term_months, "terms": sorted(q.terms), "model_year": q.model_year,
                     "amount": [amt.lo, None if amt.hi == float("inf") else amt.hi] if amt else None, "metric": q.metric, "flags": sorted(q.flags),
                     "rate_intent": bool(q.rate_intent), "list_intent": bool(q.list_intent),
                     "link_intent": bool(q.link_intent), "as_of_text": q.as_of_text, "tokens": q.toks[:14]}

    # engine: the route, in the same order Engine.answer tries them
    route, why = None, ""
    if q.link_intent and eng._link(q, view):
        route, why = "link", "The question asks for a link, so it returns the page that answers it."
    if route is None:
        availability = bool(re.search(r"\b(still|available|offer|discontinued|anymore|any more)\b", q.raw.lower()))
        if q.rate_intent or (availability and q.term_months) or _wants_minimum(q.raw):
            if eng._rates(q, view):
                route, why = "rates", "It's a rate question and a current rate table answers it."
            elif eng._discontinued(q, view):
                route, why = "discontinued", "The product had rates in an earlier download but none now."
            elif eng._is_specific_rate_question(q):
                route, why = "refuse", "It asks for a rate, but no current rate table covers that product."
    if route is None:
        r = eng._text(q, view)
        route, why = ("text", "Not a rate lookup, so it's answered from the website's text.") if r["confident"] \
            else ("refuse", f"Nothing on the current site supports an answer ({r['note']}).")
    out["engine"] = {"route": route, "why": why}

    # search: what the keyword index ranks first
    allow = st.table_units(view) if route in ("rates", "discontinued") else view.units
    hits = st.index.search(q.toks, top=5, allow=allow)
    out["search"] = {"scope": "rate tables only" if allow is not view.units else "every passage in this download",
                     "hits": [{"score": round(s, 2), "url": st.units[i].url, "heading": st.units[i].heading[:90],
                               "kind": st.units[i].kind} for s, i in hits]}

    # rates + parse: winner/losers and the table the parser built
    why_r = explain(question, as_of)
    out["rates"] = {"winner": why_r.get("winner"), "losers": why_r.get("losers", [])}
    if route in ("rates",) and why_r.get("winner"):
        groups = candidate_groups(st, view, q)
        u = groups[0].winner
        t = u.table
        out["parse"] = {"url": u.url, "page_kind": u.page_kind, "authority": u.authority, "heading": u.heading[:120],
                        "header": t.header, "rows": [r for r in t.rows[:4]], "n_rows": len(t.rows),
                        "effective": t.effective.isoformat() if t.effective else None,
                        "ends": t.ends.isoformat() if t.ends else None}
    else:
        out["parse"] = None

    # text: record which TextAnswerer handler actually produced the answer (no re-implemented routing)
    if route == "text":
        handler = _which_text_handler(q, view)
        ranked = eng.t.ranked_sentences(q, view) if handler == "best_sentences" else []
        out["text"] = {"handler": handler,
                       "sentences": [{"score": round(r[0], 2), "url": r[3].url, "sentence": r[5][:260]} for r in ranked[:3]]}
    else:
        out["text"] = None

    # conflicts: the ones about the pages this answer touched
    a = KS.answer(question, as_of=as_of)
    urls = {s.url for s in a.sources} | ({why_r["winner"]["url"]} if why_r.get("winner") else set())
    words = set(tokens(question))
    rel = [c for c in CONFLICTS if any(s["url"] in urls for s in c["sources"])
           and (route == "rates" or words & set(tokens(c["topic"])))]
    rel.sort(key=lambda c: -len(words & set(tokens(c["topic"]))))
    out["conflicts"] = {"total": len(CONFLICTS), "related": rel[:3]}

    # solution: the contract-level answer the scorer sees
    out["solution"] = {"answer": asdict(a)}
    return out


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            body = (Path(__file__).parent / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/fonts/SchibstedGrotesk.woff2":  # bundled so the page works offline
            body = (Path(__file__).parent / "fonts" / "SchibstedGrotesk.woff2").read_bytes()
            self.send_response(200)
            self.send_header("content-type", "font/woff2")
            self.send_header("cache-control", "max-age=86400")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/meta":
            self._json({"snapshots": [{"id": s.snapshot_id, "taken_on": s.taken_on, "pages": len(s.pages)}
                                      for s in SNAPS],
                        "ingest_s": round(INGEST_S, 2), "conflicts": len(CONFLICTS)})
        elif u.path == "/api/ask":
            self._json(ask(p.get("q", ""), p.get("as_of") or None))
        elif u.path == "/api/timeline":
            self._json(timeline(p.get("q", "")))
        elif u.path == "/api/trace":
            self._json(trace(p.get("q", ""), p.get("as_of") or None))
        elif u.path == "/api/source":
            self._json(source(p.get("file", ""), p.get("fn", "")))
        elif u.path == "/api/conflicts":
            self._json(CONFLICTS)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"open http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

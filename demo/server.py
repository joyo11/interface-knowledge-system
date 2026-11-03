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

from run_eval import load_snapshots, load_submission  # noqa: E402
from kb.qparse import parse_question  # noqa: E402
from kb.rates import candidate_groups, rate_columns  # noqa: E402

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
        elif u.path == "/api/meta":
            self._json({"snapshots": [{"id": s.snapshot_id, "taken_on": s.taken_on, "pages": len(s.pages)}
                                      for s in SNAPS],
                        "ingest_s": round(INGEST_S, 2), "conflicts": len(CONFLICTS)})
        elif u.path == "/api/ask":
            self._json(ask(p.get("q", ""), p.get("as_of") or None))
        elif u.path == "/api/timeline":
            self._json(timeline(p.get("q", "")))
        elif u.path == "/api/conflicts":
            self._json(CONFLICTS)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"open http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

"""Dupaco knowledge system: versioned snapshots -> current, tier-correct, cited answers.

See NOTES.md for how "current" is decided. Standard library only.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for d in HERE.parents:
    if (d / "harness" / "contract.py").exists():
        sys.path.insert(0, str(d / "harness"))
        break
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

from kb.conflicts import find_conflicts  # noqa: E402
from kb.engine import Engine  # noqa: E402
from kb.store import Store  # noqa: E402


class KnowledgeSystem:
    def __init__(self):
        self.store = Store()
        self.engine = Engine(self.store)
        self._cache: dict = {}
        self._conflicts = None

    def ingest(self, snapshot: Snapshot) -> None:
        self.store.add_snapshot(snapshot)
        self.store.finalize()
        self._cache.clear()
        self._conflicts = None

    def answer(self, question: str, as_of=None) -> Answer:
        key = (" ".join(question.lower().split()), as_of)
        if key not in self._cache:
            r = self.engine.answer(question, as_of)
            self._cache[key] = r
        r = self._cache[key]
        view = self.store.view_for(as_of)
        return Answer(
            text=r["text"],
            sources=[Source(url=u.url, snapshot_id=view.snapshot_id) for u in r["sources"]],
            confident=r["confident"],
            conflict_note=r["note"],
        )

    def conflicts(self) -> list[Conflict]:
        if self._conflicts is None:
            self._conflicts = [
                Conflict(topic=c["topic"], values=c["values"],
                         sources=[Source(url=u, snapshot_id=sid) for u, sid in c["sources"]],
                         resolution=c["resolution"], reason=c["reason"])
                for c in find_conflicts(self.store)
            ]
        return self._conflicts


def build() -> KnowledgeSystem:
    return KnowledgeSystem()

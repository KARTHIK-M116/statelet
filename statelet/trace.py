"""Structured tracing: one agent run = one trace, writes as child spans.

Shaped to Lemma's trace contract so a run can be posted straight to
Lemma, written to disk, or printed.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    name: str
    started: float
    attrs: dict[str, Any] = field(default_factory=dict)
    ended: float | None = None
    status: str = "open"

    def end(self, *, status: str = "ok", **attrs: Any) -> None:
        self.ended, self.status = time.time(), status
        self.attrs.update(attrs)

    def to_dict(self) -> dict[str, Any]:
        stop = self.ended if self.ended is not None else time.time()
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": round((stop - self.started) * 1000, 2),
            **self.attrs,
        }


class Trace:
    def __init__(self, name: str) -> None:
        self.trace_id = str(uuid.uuid4())[:8]
        self.name = name
        self.started = time.time()
        self.spans: list[Span] = []
        self.events: list[dict[str, Any]] = []

    def span(self, name: str, **attrs: Any) -> Span:
        s = Span(name=name, started=time.time(), attrs=attrs)
        self.spans.append(s)
        return s

    def event(self, name: str, **attrs: Any) -> None:
        at = round((time.time() - self.started) * 1000, 2)
        self.events.append({"name": name, "at_ms": at, **attrs})

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "duration_ms": round((time.time() - self.started) * 1000, 2),
            "spans": [s.to_dict() for s in self.spans],
            "events": self.events,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

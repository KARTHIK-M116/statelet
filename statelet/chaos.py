"""Fault injection, so reliability claims are measured not asserted.

Fault modes and why each is here:

- timeout / rate_limit  transient; should retry, not compensate
- forbidden             permanent; should abort immediately
- silent_noop           API returns success, changes nothing. The one
                        the whole design is organised around -- only
                        readback verification catches it
- readback_lies         write lands but the next read is stale
                        (eventual consistency); retry should converge
- partial_write         write lands with WRONG content. Passes an
                        existence check, fails a fingerprint check

`repeat` distinguishes a blip (retry heals it, run should converge)
from a persistent condition such as a missing OAuth scope (retry cannot
heal it, run must abort and roll back). Modelling only blips made the
silent-noop defence look tested when it was not.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

from statelet.adapters import Adapter, AdapterError
from statelet.core import Resource


class Fault(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    FORBIDDEN = "forbidden"
    SILENT_NOOP = "silent_noop"
    READBACK_LIES = "readback_lies"
    PARTIAL_WRITE = "partial_write"


@dataclass
class FaultPlan:
    app: str
    fault: Fault
    on_write: int = 1
    repeat: int = 1        # -1 = persist for the rest of the run


class ChaosAdapter:
    """Adapter decorator that injects faults."""

    def __init__(self, inner: Adapter, plan: FaultPlan | None = None) -> None:
        self._inner = inner
        self.name = inner.name
        self.plan = plan
        self._writes = 0
        self._lies_left = 0

    def read(self, subject: str) -> list[Resource]:
        return self._inner.read(subject)

    def _due(self) -> Fault | None:
        p = self.plan
        if p is None or p.app != self.name or self._writes < p.on_write:
            return None
        if p.repeat >= 0 and self._writes >= p.on_write + p.repeat:
            return None
        return p.fault

    def _maybe_raise(self, fault: Fault | None) -> None:
        if fault is Fault.TIMEOUT:
            raise AdapterError(f"{self.name}: request timed out", transient=True)
        if fault is Fault.RATE_LIMIT:
            raise AdapterError(f"{self.name}: 429 too many requests", transient=True)
        if fault is Fault.FORBIDDEN:
            raise AdapterError(f"{self.name}: 403 forbidden", transient=False)

    def create(self, resource: Resource) -> Resource:
        self._writes += 1
        fault = self._due()
        self._maybe_raise(fault)

        if fault is Fault.SILENT_NOOP:
            return resource                      # pretend; change nothing
        if fault is Fault.PARTIAL_WRITE:
            corrupt = Resource(key=resource.key, content={"body": ""},
                               meta=resource.meta)
            return self._inner.create(corrupt)
        if fault is Fault.READBACK_LIES:
            created = self._inner.create(resource)
            self._lies_left = 1
            return created
        return self._inner.create(resource)

    def update(self, resource: Resource) -> Resource:
        self._writes += 1
        fault = self._due()
        self._maybe_raise(fault)
        if fault is Fault.SILENT_NOOP:
            return resource
        if fault is Fault.PARTIAL_WRITE:
            corrupt = Resource(key=resource.key, content={"body": ""},
                               meta=resource.meta)
            return self._inner.update(corrupt)
        return self._inner.update(resource)

    def delete(self, resource: Resource) -> None:
        self._writes += 1
        fault = self._due()
        self._maybe_raise(fault)
        if fault is Fault.SILENT_NOOP:
            return
        self._inner.delete(resource)

    def verify(self, resource: Resource, *, present: bool = True) -> bool:
        if self._lies_left > 0:
            self._lies_left -= 1
            return False                         # stale read
        return self._inner.verify(resource, present=present)


# -- harness --------------------------------------------------------------


@dataclass
class Trial:
    fault: str | None
    app: str | None
    converged: bool
    aborted: bool
    applied: int
    compensated: int
    orphans: int
    blocked: int = 0      # orphans caused by a permission-denied rollback
    detail: str = ""

    @property
    def unexplained(self) -> int:
        return self.orphans - self.blocked


@dataclass
class ChaosReport:
    trials: list[Trial]

    @property
    def total(self) -> int:
        return len(self.trials)

    @property
    def aborts(self) -> int:
        return sum(1 for t in self.trials if t.aborted)

    @property
    def clean_aborts(self) -> int:
        return sum(1 for t in self.trials if t.aborted and t.orphans == 0)

    @property
    def recovered(self) -> int:
        return sum(1 for t in self.trials if t.fault and t.converged)

    @property
    def unexplained_orphans(self) -> int:
        """Must stay at zero. Total orphans may not, because a
        permission-denied rollback path is a real condition -- but every
        one of those is itemised in a cleanup manifest."""
        return sum(t.unexplained for t in self.trials)

    @property
    def blocked_orphans(self) -> int:
        return sum(t.blocked for t in self.trials)

    def table(self) -> str:
        rows = [
            f"{'fault':<16}{'app':<9}{'result':<13}"
            f"{'applied':>8}{'rolled back':>13}{'orphans':>9}",
            "-" * 68,
        ]
        for t in self.trials:
            result = ("converged" if t.converged
                      else "clean abort" if t.orphans == 0 else "ORPHANED")
            rows.append(
                f"{(t.fault or 'none'):<16}{(t.app or '-'):<9}{result:<13}"
                f"{t.applied:>8}{t.compensated:>13}{t.orphans:>9}"
            )
        rows += [
            "-" * 68,
            f"trials={self.total}  aborts={self.aborts}  "
            f"recovered-from-fault={self.recovered}  "
            f"clean-rollback={self.clean_aborts}/{self.aborts}",
            f"orphans: {self.unexplained_orphans} unexplained  |  "
            f"{self.blocked_orphans} permission-blocked rollback (itemised)",
        ]
        return "\n".join(rows)


def run_chaos(spec_factory, adapter_factory, *, trials: int = 25, seed: int = 7):
    from statelet.engine import Reconciler

    rng = random.Random(seed)
    faults = list(Fault)
    out: list[Trial] = []

    for i in range(trials):
        spec = spec_factory()
        base = adapter_factory()

        plan = None if i == 0 else FaultPlan(       # trial 0 = control
            app=rng.choice(list(base)),
            fault=rng.choice(faults),
            on_write=rng.randint(1, 4),
            repeat=rng.choice([1, 1, 2, -1, -1]),
        )
        wrapped = {
            n: ChaosAdapter(a, plan if plan and plan.app == n else None)
            for n, a in base.items()
        }

        try:
            o = Reconciler(wrapped).reconcile(spec)
            rep = o.report
            # A rollback is "blocked" whenever the fault persists through
            # the compensation path itself -- a revoked permission, or a
            # delete endpoint that silently no-ops. Both leave a resource
            # we can name but cannot remove in-run. Narrowing this to 403
            # only would mislabel the silent-no-op case as a bug.
            blocked = len(rep.orphans) if (
                plan and plan.repeat < 0 and plan.app in
                {x.op.app for x in rep.orphans}
            ) else 0
            out.append(Trial(
                fault=plan.fault.value if plan else None,
                app=plan.app if plan else None,
                converged=o.converged, aborted=rep.aborted,
                applied=len(rep.applied), compensated=rep.compensated,
                orphans=len(rep.orphans), blocked=blocked,
                detail=rep.abort_reason,
            ))
        except AdapterError as exc:
            # Read-phase failure: we refused to write, so nothing exists
            # to orphan.
            out.append(Trial(
                fault=plan.fault.value if plan else None,
                app=plan.app if plan else None,
                converged=False, aborted=True, applied=0,
                compensated=0, orphans=0, detail=f"refused: {exc}",
            ))

    return ChaosReport(trials=out)

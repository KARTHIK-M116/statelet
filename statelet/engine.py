"""Execution and reconciliation.

    observe (parallel) -> diff -> apply (verified) -> report

Guarantees, in order of importance:

1. A write is not "applied" until a read confirms it.
2. Applied writes are journalled and reversed on failure (LIFO).
3. Transient faults retry; permanent faults abort immediately.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from statelet.adapters import Adapter, AdapterError
from statelet.core import (
    Operation, OpKind, OpResult, OpStatus, Plan, Resource, Spec, compute_plan,
)
from statelet.trace import Trace


@dataclass
class Report:
    results: list[OpResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    compensated: int = 0

    @property
    def applied(self) -> list[OpResult]:
        return [r for r in self.results if r.status is OpStatus.APPLIED]

    @property
    def orphans(self) -> list[OpResult]:
        """State we may have created and can no longer account for.

        The number that matters: an agent that fails loudly with zero
        orphans is safe to run again. One that half-succeeds is not.
        """
        return [r for r in self.results if r.is_orphan]

    @property
    def ok(self) -> bool:
        return not self.aborted and not self.orphans

    def cleanup_manifest(self) -> list[dict[str, str]]:
        """Orphans, itemised. Compensation cannot fix a rollback that is
        itself permission-denied; the least we can do is name it. Since
        reconciliation converges, a later run with the permission fixed
        repairs state -- this is a diagnostic, not a manual to-do list.
        """
        return [
            {
                "app": r.op.app,
                "resource": str(r.op.resource.key),
                "remote_id": r.op.resource.remote_id or "unknown",
                "status": r.status.value,
                "reason": r.detail,
            }
            for r in self.orphans
        ]

    def summary(self) -> str:
        if self.ok:
            return f"OK: {len(self.applied)} applied, 0 orphans"
        parts = []
        if self.aborted:
            parts.append("ABORTED")
        parts.append(f"{len(self.applied)} applied")
        if self.compensated:
            parts.append(f"{self.compensated} rolled back")
        parts.append(f"{len(self.orphans)} orphans")
        return " | ".join(parts)


class Executor:
    def __init__(
        self, adapters: dict[str, Adapter], *,
        max_attempts: int = 3, backoff: float = 0.5, trace: Trace | None = None,
    ) -> None:
        self.adapters = adapters
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.trace = trace or Trace("execute")

    def apply(self, plan: Plan, *, dry_run: bool = False) -> Report:
        report = Report()
        if dry_run:
            self.trace.event("dry_run", plan=plan.summary())
            return report

        journal: list[Operation] = []
        for op in plan.ops:
            result = self._apply_one(op)
            report.results.append(result)

            if result.status is OpStatus.APPLIED:
                journal.append(op)
                continue

            # Any non-applied op aborts. Pressing on and hoping is how
            # half-provisioned accounts happen.
            report.aborted = True
            report.abort_reason = f"{op}: {result.detail}"

            # An UNVERIFIED write is swept too: the app returned success,
            # so we cannot assume nothing landed. Deletes are idempotent,
            # so cleaning up something that never existed is harmless.
            sweep = list(journal)
            if result.status is OpStatus.UNVERIFIED:
                sweep.append(op)

            self.trace.event("abort", op=str(op), reason=result.detail,
                             to_compensate=len(sweep))
            report.compensated = self._compensate(sweep, report)
            return report

        self.trace.event("complete", applied=len(report.applied))
        return report

    def _adapter(self, op: Operation) -> Adapter:
        try:
            return self.adapters[op.app]
        except KeyError:
            raise AdapterError(f"no adapter registered for {op.app!r}")

    def _write(self, adapter: Adapter, op: Operation) -> None:
        if op.kind is OpKind.CREATE:
            adapter.create(op.resource)
        elif op.kind is OpKind.UPDATE:
            adapter.update(op.resource)
        else:
            adapter.delete(op.resource)

    def _attempt(
        self, op: Operation, *, retry_permanent: bool = False
    ) -> tuple[bool, str, int]:
        """Write, then prove it, with bounded retry.

        Shared by forward application and compensation. Compensation
        needs the same retry semantics: a 429 on the way back is just as
        transient as a 429 on the way in, and treating it as fatal
        turns a recoverable rollback into a permanent orphan.

        `retry_permanent` deliberately inverts the forward-path rule.
        Going forward, a 403 means stop -- retrying a deterministic
        failure burns clock and makes it look flaky. Rolling back, a 403
        means we are about to leave state behind, and an extra wasted
        call is far cheaper than an orphan someone has to hunt down.
        """
        adapter = self._adapter(op)
        want_present = op.kind is not OpKind.DELETE
        detail = ""

        for attempt in range(1, self.max_attempts + 1):
            span = self.trace.span(f"{op.kind.value}:{op.app}",
                                   op=str(op), attempt=attempt)
            try:
                self._write(adapter, op)
            except AdapterError as exc:
                detail = str(exc)
                span.end(status="error", error=detail, transient=exc.transient)
                give_up = not (exc.transient or retry_permanent)
                if give_up or attempt == self.max_attempts:
                    return False, detail, attempt
                time.sleep(self.backoff * attempt)
                continue

            if adapter.verify(op.resource, present=want_present):
                span.end(status="ok", verified=True)
                return True, "verified", attempt

            detail = (f"write returned success but readback disagreed "
                      f"(expected present={want_present})")
            span.end(status="unverified", error=detail)
            if attempt == self.max_attempts:
                return False, detail, attempt
            time.sleep(self.backoff * attempt)

        return False, detail, self.max_attempts

    def _apply_one(self, op: Operation) -> OpResult:
        ok, detail, attempts = self._attempt(op)
        if ok:
            return OpResult(op, OpStatus.APPLIED, detail, attempts)
        # "readback disagreed" means the app accepted the write, so we
        # cannot assume nothing landed -- that distinction drives whether
        # the op gets swept into compensation.
        status = (OpStatus.UNVERIFIED if "readback disagreed" in detail
                  else OpStatus.FAILED)
        return OpResult(op, status, detail, attempts)

    def _compensate(self, journal: list[Operation], report: Report) -> int:
        """Replay the journal backwards, applying each inverse.

        LIFO because later resources may depend on earlier ones. Each
        rollback is itself verified -- an unverified rollback is an
        orphan wearing a clean shirt.
        """
        rolled_back = 0
        for op in reversed(journal):
            ok, detail, _ = self._attempt(op.inverse(), retry_permanent=True)
            if ok:
                self._mark(report, op, OpStatus.COMPENSATED, "rolled back")
                rolled_back += 1
            else:
                self._mark(report, op, OpStatus.COMPENSATION_FAILED, detail)
        return rolled_back

    @staticmethod
    def _mark(report: Report, op: Operation, status: OpStatus, detail: str) -> None:
        for r in report.results:
            if r.op is op:
                r.status, r.detail = status, detail
                return


@dataclass
class Outcome:
    plan: Plan
    report: Report
    trace: Trace

    @property
    def converged(self) -> bool:
        return self.report.ok and not self.report.aborted


class Reconciler:
    def __init__(self, adapters: dict[str, Adapter]) -> None:
        self.adapters = adapters

    def observe(self, subject: str, trace: Trace) -> list[Resource]:
        """Read every app in parallel.

        A read failure is fatal by design: reconciling against a partial
        view would 'create' resources that already exist in the app we
        failed to read, which is the duplicate-provisioning bug this
        architecture exists to prevent.
        """
        def one(item):
            name, adapter = item
            span = trace.span(f"read:{name}", subject=subject)
            try:
                found = adapter.read(subject)
            except AdapterError as exc:
                span.end(status="error", error=str(exc))
                raise AdapterError(
                    f"cannot reconcile: read from {name!r} failed ({exc}). "
                    "Refusing to act on a partial view of state."
                ) from exc
            span.end(status="ok", found=len(found))
            return found

        if len(self.adapters) == 1:
            return one(next(iter(self.adapters.items())))

        with ThreadPoolExecutor(max_workers=len(self.adapters)) as pool:
            results = list(pool.map(one, self.adapters.items()))
        return [r for batch in results for r in batch]

    def reconcile(
        self, spec: Spec, *, dry_run: bool = False, trace: Trace | None = None
    ) -> Outcome:
        trace = trace or Trace("reconcile")
        trace.event("start", subject=spec.subject.email,
                    role=spec.subject.role, prune=spec.prune)

        observed = self.observe(spec.subject.email, trace)
        plan = compute_plan(spec.resources(), observed, prune=spec.prune)
        trace.event("planned", creates=len(plan.creates), updates=len(plan.updates),
                    deletes=len(plan.deletes), unchanged=len(plan.unchanged))

        report = Executor(self.adapters, trace=trace).apply(plan, dry_run=dry_run)
        return Outcome(plan=plan, report=report, trace=trace)

"""One test per claim in docs/RELIABILITY.md.

If a claim is not covered here, it does not go in the brief.
"""

import pytest

from statelet.adapters import AdapterError, FakeAdapter
from statelet.chaos import ChaosAdapter, Fault, FaultPlan, run_chaos
from statelet.core import Spec, Subject
from statelet.engine import Reconciler

APPS = ("slack", "notion", "linear", "gcal")
EXPECTED = 10  # 4 slack + 2 notion + 2 linear + 2 gcal
CHECKLIST = "notion:page:priya@acme.com:onboarding-checklist"


def adapters():
    return {n: FakeAdapter(n) for n in APPS}


def spec(prune=False):
    return Spec.from_role(
        Subject(email="priya@acme.com", name="Priya R",
                role="backend-engineer", manager="amit@acme.com",
                start_date="2026-09-21"),
        prune=prune,
    )


def chaos(app, fault, on_write=1, repeat=1):
    base = adapters()
    wrapped = {
        n: ChaosAdapter(a, FaultPlan(app, fault, on_write, repeat)
                        if n == app else None)
        for n, a in base.items()
    }
    return base, wrapped


# -- core behaviour -------------------------------------------------------


def test_provisions_from_empty():
    ad = adapters()
    o = Reconciler(ad).reconcile(spec())
    assert o.converged
    assert len(o.plan.creates) == EXPECTED
    assert sum(a.count() for a in ad.values()) == EXPECTED


def test_idempotent_rerun_issues_zero_writes():
    ad = adapters()
    r = Reconciler(ad)
    r.reconcile(spec())
    writes = {n: a.write_calls for n, a in ad.items()}

    second = r.reconcile(spec())
    assert second.plan.is_noop
    assert {n: a.write_calls for n, a in ad.items()} == writes


def test_dry_run_writes_nothing():
    ad = adapters()
    o = Reconciler(ad).reconcile(spec(), dry_run=True)
    assert len(o.plan.creates) == EXPECTED
    assert sum(a.write_calls for a in ad.values()) == 0


def test_offboarding_uses_the_same_engine():
    ad = adapters()
    r = Reconciler(ad)
    r.reconcile(spec())
    o = r.reconcile(spec().offboard())
    assert len(o.plan.deletes) == EXPECTED
    assert o.converged
    assert sum(a.count() for a in ad.values()) == 0


# -- drift ----------------------------------------------------------------


def test_repairs_only_identity_drift():
    ad = adapters()
    r = Reconciler(ad)
    r.reconcile(spec())
    assert ad["slack"].tamper_delete(
        "slack:channel_membership:priya@acme.com:backend")
    assert ad["notion"].tamper_delete(CHECKLIST)

    o = r.reconcile(spec())
    assert len(o.plan.creates) == 2
    assert not o.plan.updates and not o.plan.deletes
    assert o.converged
    assert sum(a.count() for a in ad.values()) == EXPECTED


def test_detects_content_drift_not_just_missing_resources():
    """A page that exists with the wrong body is NOT in sync."""
    ad = adapters()
    r = Reconciler(ad)
    r.reconcile(spec())

    assert ad["notion"].tamper_content(CHECKLIST, {"body": ""})
    o = r.reconcile(spec())

    assert len(o.plan.updates) == 1, "emptied body must register as drift"
    assert not o.plan.creates, "the resource still exists; do not recreate it"
    assert o.converged
    # Content is actually restored, not merely reported as fixed.
    restored = {str(x.key): x for x in ad["notion"].read("priya@acme.com")}
    assert restored[CHECKLIST].content["body"] != ""


def test_content_drift_converges_on_rerun():
    ad = adapters()
    r = Reconciler(ad)
    r.reconcile(spec())
    ad["notion"].tamper_content(CHECKLIST, {"body": "wrong"})
    r.reconcile(spec())
    assert r.reconcile(spec()).plan.is_noop


def test_update_rollback_restores_prior_content():
    """Compensating an UPDATE writes the old content back, not a delete."""
    ad = adapters()
    Reconciler(ad).reconcile(spec())
    ad["notion"].tamper_content(CHECKLIST, {"body": "drifted"})

    # gcal fails persistently, so the notion update must be rolled back.
    wrapped = dict(ad)
    wrapped["gcal"] = ChaosAdapter(
        ad["gcal"], FaultPlan("gcal", Fault.FORBIDDEN, 1, repeat=-1))
    # Force an ordering where notion updates before gcal writes by
    # dropping gcal state so it has creates pending.
    ad["gcal"].tamper_delete("gcal:event:priya@acme.com:week1-1on1")

    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    body = {str(x.key): x for x in ad["notion"].read("priya@acme.com")}
    assert body[CHECKLIST].content["body"] == "drifted", (
        "rollback must restore the observed prior content"
    )


# -- failure handling -----------------------------------------------------


def test_persistent_silent_noop_is_caught_not_trusted():
    """A 2xx that changed nothing is not success (missing OAuth scope)."""
    base, wrapped = chaos("slack", Fault.SILENT_NOOP, repeat=-1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    assert "readback disagreed" in o.report.abort_reason
    assert not o.report.orphans
    assert sum(a.count() for a in base.values()) == 0


def test_single_silent_noop_blip_self_heals():
    """Over-reacting to a blip throws away completed work."""
    base, wrapped = chaos("slack", Fault.SILENT_NOOP, repeat=1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.converged
    assert sum(a.count() for a in base.values()) == EXPECTED


def test_partial_write_fails_verification():
    """A write that lands with the WRONG content must not pass.

    This is the fault an existence check cannot catch -- only a content
    fingerprint comparison does.
    """
    base, wrapped = chaos("notion", Fault.PARTIAL_WRITE, repeat=-1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    assert "readback disagreed" in o.report.abort_reason


def test_rollback_leaves_zero_orphans():
    base, wrapped = chaos("notion", Fault.FORBIDDEN, repeat=1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    assert not o.report.orphans
    assert sum(a.count() for a in base.values()) == 0


def test_transient_fault_is_retried_not_compensated():
    base, wrapped = chaos("linear", Fault.TIMEOUT, repeat=1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.converged
    assert sum(a.count() for a in base.values()) == EXPECTED


def test_read_failure_refuses_to_act():
    """Never reconcile against a partial view of the world."""

    class Blind(FakeAdapter):
        def read(self, subject):
            raise AdapterError("500 from upstream")

    ad = adapters()
    ad["notion"] = Blind("notion")
    with pytest.raises(AdapterError, match="Refusing to act"):
        Reconciler(ad).reconcile(spec())
    assert sum(a.count() for a in ad.values()) == 0


def test_parallel_reads_hit_every_app():
    """Reads run concurrently; all four apps must still be observed."""
    ad = adapters()
    Reconciler(ad).reconcile(spec())
    o = Reconciler(ad).reconcile(spec())
    apps_seen = {r.key.app for r in o.plan.unchanged}
    assert apps_seen == set(APPS)


# -- orphan accounting ----------------------------------------------------


def test_every_orphan_is_itemised_in_the_manifest():
    base, wrapped = chaos("notion", Fault.FORBIDDEN, on_write=2, repeat=-1)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    orphans = o.report.orphans
    assert orphans, "this scenario should block a rollback"

    manifest = o.report.cleanup_manifest()
    assert len(manifest) == len(orphans)
    for e in manifest:
        assert e["app"] == "notion"
        assert e["reason"], "an orphan with no stated reason is useless"


def test_rerun_converges_after_blocked_rollback():
    """Convergence is what makes a cleanup manifest cheap."""
    base, wrapped = chaos("notion", Fault.FORBIDDEN, on_write=2, repeat=-1)
    first = Reconciler(wrapped).reconcile(spec())
    assert first.report.aborted

    second = Reconciler(base).reconcile(spec())   # permission restored
    assert second.converged
    assert sum(a.count() for a in base.values()) == EXPECTED


def test_chaos_suite_no_unexplained_orphans():
    """Headline reliability claim, swept across seeds.

    A single seed is not evidence: an earlier version of this test
    passed on seed 7 while 63 orphans were hiding on other seeds. The
    sweep is the test.

    Orphans are permitted only where the injected fault persists through
    the compensation path itself -- a revoked permission or a delete
    endpoint that silently no-ops leaves a resource we can name but
    cannot remove in-run. Those are itemised in a cleanup manifest.
    """
    unexplained = 0
    total = 0
    for seed in range(1, 11):
        rep = run_chaos(spec, adapters, trials=20, seed=seed)
        total += rep.total
        unexplained += rep.unexplained_orphans
        assert rep.unexplained_orphans == 0, f"seed {seed}:\n{rep.table()}"
    assert total == 200
    assert unexplained == 0


def test_compensation_retries_transient_faults():
    """A 429 on the way back is as transient as one on the way in.

    Treating it as fatal turns a recoverable rollback into a permanent
    orphan -- this was a real bug the seed sweep exposed.
    """
    base, wrapped = chaos("slack", Fault.RATE_LIMIT, on_write=3, repeat=2)
    o = Reconciler(wrapped).reconcile(spec())
    if o.report.aborted:
        assert not o.report.orphans, (
            "transient fault during rollback must be retried, not orphaned"
        )


def test_compensation_retries_permanent_faults_too():
    """Rolling back, a 403 means we are about to leave state behind.

    The forward path gives up on a 403 because retrying a deterministic
    failure is pointless. The rollback path does not, because a wasted
    call is cheaper than an orphan.
    """
    base, wrapped = chaos("notion", Fault.FORBIDDEN, on_write=1, repeat=2)
    o = Reconciler(wrapped).reconcile(spec())
    assert o.report.aborted
    assert not o.report.orphans
    assert sum(a.count() for a in base.values()) == 0

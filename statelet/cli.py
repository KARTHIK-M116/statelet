"""CLI, and the demo surface.

    statelet doctor                       # check live credentials
    statelet plan "onboard ..."
    statelet apply    --spec specs/new-hire.yaml [--dry-run] [--live]
    statelet drift    --spec ... --break slack:backend --empty notion:runbook
    statelet offboard --spec ...
    statelet chaos    --trials 25

Default backend is in-memory, so everything runs with no credentials.
"""

from __future__ import annotations

import argparse
import json
import sys

from statelet.adapters import AdapterError, FakeAdapter
from statelet.chaos import run_chaos
from statelet.core import OpStatus, Spec, Subject
from statelet.engine import Outcome, Reconciler
from statelet.trace import Trace

APPS = ("slack", "notion", "linear", "gcal")
KINDS = {"slack": "channel_membership", "notion": "page",
         "linear": "issue", "gcal": "event"}

DIM, BOLD, RST = "\033[2m", "\033[1m", "\033[0m"
GRN, RED, YEL, CYN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def c(t: str, col: str) -> str:
    return f"{col}{t}{RST}" if sys.stdout.isatty() else t


_FAKES: dict[str, FakeAdapter] = {}


def get_adapters(args) -> dict:
    if getattr(args, "live", False):
        from statelet.live import build_live_adapters
        return build_live_adapters()
    global _FAKES
    if not _FAKES:
        _FAKES = {n: FakeAdapter(n) for n in APPS}
    return _FAKES


# -- rendering ------------------------------------------------------------


def render_plan(o: Outcome) -> None:
    print(f"\n{BOLD}Plan{RST}  {o.plan.summary()}")
    for op in o.plan.creates:
        print(f"  {c('+', GRN)} {op.resource.key}")
    for op in o.plan.updates:
        print(f"  {c('~', YEL)} {op.resource.key} {c('(content drift)', DIM)}")
    for op in o.plan.deletes:
        print(f"  {c('-', RED)} {op.resource.key}")
    if o.plan.unchanged and not o.plan.is_noop:
        print(c(f"  = {len(o.plan.unchanged)} already in sync", DIM))


def render_report(o: Outcome) -> None:
    rep = o.report
    print(f"\n{BOLD}Execution{RST}")
    icon = {
        OpStatus.APPLIED: c("ok", GRN),
        OpStatus.FAILED: c("fail", RED),
        OpStatus.UNVERIFIED: c("unverified", YEL),
        OpStatus.COMPENSATED: c("rolled back", CYN),
        OpStatus.COMPENSATION_FAILED: c("ORPHAN", RED),
    }
    for r in rep.results:
        retry = f" {DIM}(attempt {r.attempts}){RST}" if r.attempts > 1 else ""
        print(f"  [{icon.get(r.status, r.status.value)}] {r.op.resource.key}{retry}")
        if r.status is not OpStatus.APPLIED and r.detail:
            print(c(f"        {r.detail}", DIM))

    print()
    if rep.ok and not rep.aborted:
        print(c(f"CONVERGED  {rep.summary()}", GRN))
    elif rep.orphans:
        print(c(f"ABORTED WITH ORPHANS  {rep.summary()}", RED))
        print(c(f"  cause: {rep.abort_reason}", RED))
        print(f"\n{BOLD}Cleanup manifest{RST}")
        print(json.dumps(rep.cleanup_manifest(), indent=2))
    else:
        print(c(f"CLEAN ABORT  {rep.summary()}", YEL))
        print(c(f"  cause: {rep.abort_reason}", YEL))
        print(c("  state left exactly as it was found", DIM))


# -- commands -------------------------------------------------------------


def cmd_doctor(args) -> int:
    """Preflight every live credential. The live adapters are the
    unverified path, so this is the check to run before trusting them."""
    from statelet.live import build_gcal, missing_env
    from statelet.live import LinearAdapter, NotionAdapter, SlackAdapter
    import os

    print(f"{BOLD}Checking live credentials{RST}\n")
    missing = missing_env()
    builders = {
        "slack": lambda: SlackAdapter(os.environ["SLACK_BOT_TOKEN"]),
        "notion": lambda: NotionAdapter(os.environ["NOTION_TOKEN"],
                                        os.environ["NOTION_PARENT_PAGE_ID"]),
        "linear": lambda: LinearAdapter(os.environ["LINEAR_API_KEY"],
                                        os.environ["LINEAR_TEAM_ID"]),
        "gcal": build_gcal,
    }

    failures = 0
    for app in APPS:
        if app in missing:
            print(f"  {c('SKIP', YEL)}  {app:<8} missing env: "
                  f"{', '.join(missing[app])}")
            failures += 1
            continue
        try:
            detail = builders[app]().preflight()
            print(f"  {c('PASS', GRN)}  {app:<8} {detail}")
        except AdapterError as exc:
            print(f"  {c('FAIL', RED)}  {app:<8} {exc}")
            failures += 1

    print()
    if failures:
        print(c(f"{failures}/{len(APPS)} apps not ready. "
                "See docs/AUTH.md for setup.", YEL))
        return 1
    print(c("all 4 apps reachable -- safe to try --live --dry-run", GRN))
    return 0


def cmd_plan(args) -> int:
    from statelet.planner import PlannerError, plan_spec

    trace = Trace("plan")
    try:
        spec = plan_spec(args.request, trace=trace)
    except PlannerError as exc:
        print(c(f"planner error: {exc}", RED), file=sys.stderr)
        return 2

    print(json.dumps(spec.to_dict(), indent=2))
    if args.out:
        import yaml
        with open(args.out, "w") as fh:
            yaml.safe_dump(spec.to_dict(), fh, sort_keys=False)
        print(c(f"\nwrote {args.out}", DIM))
    if args.trace:
        print(f"\n{BOLD}Trace{RST}\n{trace.to_json()}")
    return 0


def _run(spec: Spec, args, label: str) -> int:
    adapters = get_adapters(args)
    trace = Trace(label)
    o = Reconciler(adapters).reconcile(spec, dry_run=args.dry_run, trace=trace)

    render_plan(o)
    if args.dry_run:
        print(c("\nDRY RUN -- no writes issued", YEL))
        return 0
    render_report(o)
    if args.trace:
        print(f"\n{BOLD}Trace{RST}\n{trace.to_json()}")
    return 0 if o.report.ok else 1


def cmd_apply(args) -> int:
    return _run(Spec.load(args.spec), args, "onboard")


def cmd_offboard(args) -> int:
    return _run(Spec.load(args.spec).offboard(), args, "offboard")


def cmd_drift(args) -> int:
    """Break state out-of-band, then reconcile.

    --break deletes a resource (identity drift).
    --empty blanks its content (content drift: still there, now wrong).
    """
    spec = Spec.load(args.spec)
    adapters = get_adapters(args)
    Reconciler(adapters).reconcile(spec)          # start converged

    print(f"{BOLD}Breaking state out-of-band{RST}")
    broken = 0

    def key_for(token: str) -> tuple[str, str] | None:
        if ":" not in token:
            return None
        app, name = token.split(":", 1)
        if app not in KINDS:
            return None
        return app, f"{app}:{KINDS[app]}:{spec.subject.email}:{name}"

    for token in (args.break_ or "").split(","):
        token = token.strip()
        parsed = key_for(token) if token else None
        if not parsed:
            continue
        app, key = parsed
        if adapters[app].tamper_delete(key):
            print(f"  {c('deleted', RED)} {key}")
            broken += 1
        else:
            print(c(f"  not found: {key}", YEL))

    for token in (args.empty or "").split(","):
        token = token.strip()
        parsed = key_for(token) if token else None
        if not parsed:
            continue
        app, key = parsed
        if adapters[app].tamper_content(key, {"body": ""}):
            print(f"  {c('emptied', YEL)} {key} {c('(identity intact)', DIM)}")
            broken += 1
        else:
            print(c(f"  not found: {key}", YEL))

    if not broken:
        print(c("nothing broken; pass --break and/or --empty", YEL))
        return 2

    print(f"\n{BOLD}Reconciling{RST} {DIM}(expect exactly {broken} repairs){RST}")
    args.dry_run = False
    o = Reconciler(adapters).reconcile(spec)
    render_plan(o)
    render_report(o)

    repairs = len(o.plan.creates) + len(o.plan.updates)
    if repairs == broken and not o.plan.deletes:
        print(c(f"\nrepaired exactly {repairs}, touched nothing else", GRN))
        return 0
    print(c(f"\nexpected {broken} repairs, planned {repairs}", RED))
    return 1


def cmd_chaos(args) -> int:
    def spec_factory() -> Spec:
        return Spec.from_role(Subject(
            email="priya@acme.com", name="Priya R", role="backend-engineer",
            manager="amit@acme.com", start_date="2026-09-21",
        ))

    rep = run_chaos(spec_factory,
                    lambda: {n: FakeAdapter(n) for n in APPS},
                    trials=args.trials, seed=args.seed)
    print(rep.table())
    return 0 if rep.unexplained_orphans == 0 else 1


# -- argparse -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="statelet",
        description="Declarative, verified reconciliation across SaaS apps.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--live", action="store_true", help="use real app APIs")
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--trace", action="store_true")

    sp = sub.add_parser("doctor", help="check live credentials")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("plan", help="natural language -> spec")
    sp.add_argument("request")
    sp.add_argument("--out", help="write spec to YAML")
    sp.add_argument("--trace", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("apply", help="reconcile to desired state")
    sp.add_argument("--spec", required=True)
    common(sp)
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("offboard", help="reconcile to empty state")
    sp.add_argument("--spec", required=True)
    common(sp)
    sp.set_defaults(func=cmd_offboard)

    sp = sub.add_parser("drift", help="break state, then repair it")
    sp.add_argument("--spec", required=True)
    sp.add_argument("--break", dest="break_", default="",
                    help="delete resources, e.g. slack:backend,notion:runbook")
    sp.add_argument("--empty", default="",
                    help="blank content, e.g. notion:onboarding-checklist")
    common(sp)
    sp.set_defaults(func=cmd_drift)

    sp = sub.add_parser("chaos", help="fault injection suite")
    sp.add_argument("--trials", type=int, default=25)
    sp.add_argument("--seed", type=int, default=7)
    sp.set_defaults(func=cmd_chaos)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AdapterError as exc:
        # Credential and read failures are operating conditions, not
        # crashes; a traceback would bury the actionable part.
        print(c(str(exc), RED), file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(c(f"file not found: {exc.filename}", RED), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

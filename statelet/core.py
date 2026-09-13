"""Resource model, desired-state spec, and the diff.

Two ideas carry the whole system:

1. A resource has a stable *identity* (key) and a tracked *content*
   fingerprint. Identity drift means it is missing; content drift means
   it is wrong. Both are detected.
2. The diff is set arithmetic, not model output. That is what makes
   idempotency provable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ResourceKey:
    """Identity. Excludes anything mutable, so the same logical resource
    read at two different times compares equal."""

    app: str       # slack | notion | linear | gcal
    kind: str      # channel_membership | page | issue | event
    subject: str   # the person this is about
    name: str      # channel name, page title, issue title

    def __str__(self) -> str:
        return f"{self.app}:{self.kind}:{self.subject}:{self.name}"


@dataclass
class Resource:
    key: ResourceKey
    content: dict[str, Any] = field(default_factory=dict)  # diffed
    meta: dict[str, Any] = field(default_factory=dict)     # not diffed
    remote_id: str | None = None

    @property
    def fingerprint(self) -> str:
        """Hash of tracked content. Empty content -> no content tracking,
        so such resources are compared on identity alone."""
        if not self.content:
            return ""
        blob = json.dumps(self.content, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Resource) and self.key == other.key


class OpKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Operation:
    """A unit of change with a known inverse.

    UPDATE carries `prior` -- the observed state before the change --
    because undoing an update means writing the old content back.
    """

    kind: OpKind
    resource: Resource
    prior: Resource | None = None

    @property
    def app(self) -> str:
        return self.resource.key.app

    def inverse(self) -> Operation:
        if self.kind is OpKind.CREATE:
            return Operation(OpKind.DELETE, self.resource)
        if self.kind is OpKind.DELETE:
            return Operation(OpKind.CREATE, self.resource)
        if self.prior is None:
            raise ValueError(f"cannot invert UPDATE without prior: {self}")
        return Operation(OpKind.UPDATE, self.prior, prior=self.resource)

    def __str__(self) -> str:
        return f"{self.kind.value.upper()} {self.resource.key}"


class OpStatus(str, Enum):
    APPLIED = "applied"
    FAILED = "failed"
    UNVERIFIED = "unverified"       # write said OK, readback disagreed
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass
class OpResult:
    op: Operation
    status: OpStatus
    detail: str = ""
    attempts: int = 1

    @property
    def is_orphan(self) -> bool:
        return self.status is OpStatus.COMPENSATION_FAILED


# -- spec -----------------------------------------------------------------


@dataclass
class Subject:
    email: str
    name: str = ""
    role: str = ""
    manager: str = ""
    start_date: str = ""


ROLES: dict[str, dict[str, list[str]]] = {
    "backend-engineer": {
        "slack_channels": ["engineering", "backend", "deploys"],
        "notion_pages": ["onboarding-checklist", "backend-runbook"],
        "linear_issues": ["setup-dev-environment", "ship-first-pr"],
        "calendar_events": ["week1-1on1", "week1-standup"],
    },
    "data-scientist": {
        "slack_channels": ["data", "ml-research"],
        "notion_pages": ["onboarding-checklist", "data-access-guide"],
        "linear_issues": ["setup-notebook-env", "reproduce-baseline"],
        "calendar_events": ["week1-1on1", "data-walkthrough"],
    },
    "designer": {
        "slack_channels": ["design", "product"],
        "notion_pages": ["onboarding-checklist", "design-system"],
        "linear_issues": ["audit-current-flows", "figma-access-check"],
        "calendar_events": ["week1-1on1", "design-crit"],
    },
}
MANAGED: dict[str, set[str]] = {
    "slack": {c for t in ROLES.values() for c in t["slack_channels"]},
    "notion": {p for t in ROLES.values() for p in t["notion_pages"]},
    "linear": {i for t in ROLES.values() for i in t["linear_issues"]},
    "gcal": {e for t in ROLES.values() for e in t["calendar_events"]},
}
# Body text the reconciler keeps in sync. Emptying a page in Notion
# changes the fingerprint, which is how content drift is caught.
PAGE_BODIES = {
    "onboarding-checklist": "Day 1 setup, accounts, buddy intro.",
    "backend-runbook": "Deploys, on-call rotation, incident process.",
    "data-access-guide": "Warehouse access, PII policy, query patterns.",
    "design-system": "Tokens, components, contribution guide.",
}


@dataclass
class Spec:
    subject: Subject
    slack_channels: list[str] = field(default_factory=list)
    notion_pages: list[str] = field(default_factory=list)
    linear_issues: list[str] = field(default_factory=list)
    calendar_events: list[str] = field(default_factory=list)
    prune: bool = False

    @classmethod
    def from_role(cls, subject: Subject, *, prune: bool = False) -> Spec:
        tpl = ROLES.get(subject.role)
        if tpl is None:
            raise ValueError(
                f"unknown role {subject.role!r}; known: {', '.join(sorted(ROLES))}"
            )
        return cls(subject=subject, prune=prune, **{k: list(v) for k, v in tpl.items()})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Spec:
        s = data.get("subject") or {}
        if not s.get("email"):
            raise ValueError("spec.subject.email is required")
        d = data.get("desired") or {}
        return cls(
            subject=Subject(
                email=s["email"], name=s.get("name", ""), role=s.get("role", ""),
                manager=s.get("manager", ""), start_date=str(s.get("start_date", "")),
            ),
            slack_channels=list(d.get("slack_channels", [])),
            notion_pages=list(d.get("notion_pages", [])),
            linear_issues=list(d.get("linear_issues", [])),
            calendar_events=list(d.get("calendar_events", [])),
            prune=bool(data.get("prune", False)),
        )

    @classmethod
    def load(cls, path: str | Path) -> Spec:
        with open(path) as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": {
                "email": self.subject.email, "name": self.subject.name,
                "role": self.subject.role, "manager": self.subject.manager,
                "start_date": self.subject.start_date,
            },
            "desired": {
                "slack_channels": self.slack_channels,
                "notion_pages": self.notion_pages,
                "linear_issues": self.linear_issues,
                "calendar_events": self.calendar_events,
            },
            "prune": self.prune,
        }

    def offboard(self) -> Spec:
        """Offboarding is reconciling to empty state with pruning on --
        the same engine, run backwards."""
        return Spec(subject=self.subject, prune=True)

    def resources(self) -> list[Resource]:
        subj = self.subject.email
        out: list[Resource] = []

        def add(app, kind, names, content=None, **meta):
            for n in names:
                out.append(Resource(
                    key=ResourceKey(app, kind, subj, n),
                    content=(content(n) if content else {}),
                    meta=meta,
                ))

        add("slack", "channel_membership", self.slack_channels)
        add("notion", "page", self.notion_pages,
            content=lambda n: {"body": PAGE_BODIES.get(n, f"Onboarding: {n}")})
        add("linear", "issue", self.linear_issues,
            content=lambda n: {"assignee": subj},
            manager=self.subject.manager)
        add("gcal", "event", self.calendar_events,
            content=lambda n: {"start": self.subject.start_date},
            attendees=[subj, self.subject.manager])
        return out


# -- diff -----------------------------------------------------------------


@dataclass
class Plan:
    creates: list[Operation] = field(default_factory=list)
    updates: list[Operation] = field(default_factory=list)
    deletes: list[Operation] = field(default_factory=list)
    unchanged: list[Resource] = field(default_factory=list)

    @property
    def ops(self) -> list[Operation]:
        # Deletes first: pruning before adding avoids transient states
        # that exceed a seat or quota limit mid-run.
        return self.deletes + self.updates + self.creates

    @property
    def is_noop(self) -> bool:
        return not (self.creates or self.updates or self.deletes)

    def summary(self) -> str:
        if self.is_noop:
            return f"in sync ({len(self.unchanged)} resources, no changes)"
        bits = []
        for sym, seq in (("+", self.creates), ("~", self.updates), ("-", self.deletes)):
            if seq:
                bits.append(f"{sym}{len(seq)}")
        return f"{' '.join(bits)} ({len(self.unchanged)} already in sync)"


def compute_plan(
    desired: list[Resource], observed: list[Resource], *, prune: bool = False
) -> Plan:
    """desired - observed, plus content comparison on the overlap.

    Identity drift (missing/extra) and content drift (present but wrong)
    are different problems and produce different operations.
    """
    d = {r.key: r for r in desired}
    o = {r.key: r for r in observed}

    missing = sorted(d.keys() - o.keys(), key=str)
    extra = sorted(
        (k for k in o.keys() - d.keys() if k.name in MANAGED.get(k.app, set())),
        key=str,
    )
    common = sorted(d.keys() & o.keys(), key=str)

    updates, unchanged = [], []
    for k in common:
        want, have = d[k], o[k]
        # Only compare when the desired side declares tracked content;
        # otherwise identity alone is the contract.
        if want.content and want.fingerprint != have.fingerprint:
            want.remote_id = have.remote_id
            updates.append(Operation(OpKind.UPDATE, want, prior=have))
        else:
            unchanged.append(have)

    return Plan(
        creates=[Operation(OpKind.CREATE, d[k]) for k in missing],
        updates=updates,
        deletes=[Operation(OpKind.DELETE, o[k]) for k in extra] if prune else [],
        unchanged=unchanged,
    )

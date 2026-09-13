"""Natural language -> desired-state spec.

This is the only place a model is allowed to make a decision, and the
boundary is deliberate:

    The model decides WHAT THE GOAL IS.
    The diff engine decides WHAT TO CHANGE.

Why it matters. If an LLM emits actions directly, the same request can
produce different writes on different runs. Idempotency becomes
unprovable, rollback becomes untrustworthy, and "it worked when I
demoed it" is the strongest claim available. By confining the model to
producing a declarative spec -- which is then validated, diffed against
observed state, and executed deterministically -- a model mistake shows
up as a *wrong goal* that a human can read and reject in a dry run,
never as a surprise mutation in someone's Slack workspace.

The model's output is also structurally constrained: it may only select
from known role templates and add channels/pages from an allowlist. A
hallucinated channel name cannot become a write.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from statelet.core import ROLES, Spec, Subject
from statelet.trace import Trace

MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = """You convert an onboarding request into a JSON desired-state spec.

Return ONLY a JSON object, no prose, no markdown fences.

Schema:
{
  "email": "<work email>",
  "name": "<full name>",
  "role": "<one of: %s>",
  "manager": "<manager email, or empty string>",
  "start_date": "<YYYY-MM-DD, or empty string>",
  "extra_slack_channels": ["<channel>", ...]
}

Rules:
- "role" MUST be exactly one of the listed roles. Pick the closest match.
- Do not invent an email. If none is given, use "".
- extra_slack_channels only for channels explicitly requested. Otherwise [].
""" % ", ".join(sorted(ROLES))


class PlannerError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in fences despite instructions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise PlannerError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _call_model(request: str, *, trace: Trace) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise PlannerError("ANTHROPIC_API_KEY not set")

    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 1000,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": request}],
        }
    ).encode()

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )

    span = trace.span("llm:plan", model=MODEL, chars=len(request))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError) as exc:
        span.end(status="error", error=str(exc))
        raise PlannerError(f"model call failed: {exc}") from exc

    text = "".join(
        b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
    )
    usage = payload.get("usage", {})
    span.end(
        status="ok",
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )
    return _extract_json(text)


# -- deterministic fallback ----------------------------------------------

_ROLE_HINTS = {
    "backend-engineer": ("backend", "back end", "server", "api", "platform"),
    "data-scientist": ("data scien", "ml ", "machine learning", "analyst", "research"),
    "designer": ("design", "ux", "ui ", "product design"),
}


def _fallback_parse(request: str) -> dict:
    """Rule-based extraction, used when no API key is available.

    Not a replacement for the model -- it exists so the demo and the
    test suite never depend on a network call, and so a key outage
    degrades the product to "type a bit more structure" rather than
    "nothing works".
    """
    low = request.lower()

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", request)
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", request)

    role = "backend-engineer"
    for candidate, hints in _ROLE_HINTS.items():
        if any(h in low for h in hints):
            role = candidate
            break

    name_match = re.search(
        r"\b(?:onboard|add|provision|set up)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        request,
    )
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", request)

    return {
        "email": email_match.group(0) if email_match else "",
        "name": name_match.group(1) if name_match else "",
        "role": role,
        "manager": emails[1] if len(emails) > 1 else "",
        "start_date": date_match.group(0) if date_match else "",
        "extra_slack_channels": [],
    }


# -- public ---------------------------------------------------------------

SLACK_ALLOWLIST = {
    "general", "engineering", "backend", "frontend", "deploys", "data",
    "ml-research", "design", "product", "incidents", "random",
}


def plan_spec(
    request: str, *, trace: Trace | None = None, allow_fallback: bool = True
) -> Spec:
    """Turn a sentence into a validated Spec.

    Raises PlannerError if the result cannot be validated. Nothing
    unvalidated ever reaches the executor.
    """
    trace = trace or Trace("plan")

    try:
        raw = _call_model(request, trace=trace)
        source = "model"
    except PlannerError:
        if not allow_fallback:
            raise
        raw = _fallback_parse(request)
        source = "fallback"
        trace.event("planner_fallback", reason="model unavailable")

    email = (raw.get("email") or "").strip()
    if not email or "@" not in email:
        raise PlannerError(
            "could not determine a work email from the request; "
            "add one explicitly (e.g. 'priya@acme.com')"
        )

    role = (raw.get("role") or "").strip()
    if role not in ROLES:
        raise PlannerError(
            f"model proposed unknown role {role!r}; "
            f"allowed: {', '.join(sorted(ROLES))}"
        )

    spec = Spec.from_role(
        Subject(
            email=email,
            name=(raw.get("name") or "").strip(),
            role=role,
            manager=(raw.get("manager") or "").strip(),
            start_date=(raw.get("start_date") or "").strip(),
        )
    )

    # Allowlist extra channels. A hallucinated channel is dropped, not
    # written -- and the drop is recorded in the trace so it is visible
    # rather than silent.
    extra = raw.get("extra_slack_channels") or []
    accepted = [c for c in extra if c in SLACK_ALLOWLIST]
    rejected = [c for c in extra if c not in SLACK_ALLOWLIST]
    for c in accepted:
        if c not in spec.slack_channels:
            spec.slack_channels.append(c)
    if rejected:
        trace.event("planner_rejected_channels", channels=rejected)

    trace.event(
        "planned_spec",
        source=source,
        subject=email,
        role=role,
        resources=len(spec.resources()),
    )
    return spec

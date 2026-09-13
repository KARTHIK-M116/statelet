# statelet

**Declarative, verified reconciliation across SaaS apps.**

Describe what should be true about a person. The agent makes it true
across Slack, Notion, Linear and Google Calendar — and can prove it did.

Demo Link: https://drive.google.com/file/d/1EdgDg7pEpK4FZjBS6RUmQJ9LtezXofJR/view?usp=sharing
```
$ statelet apply --spec specs/new-hire.yaml
Plan  +10
CONVERGED  OK: 10 applied, 0 orphans

$ statelet apply --spec specs/new-hire.yaml        # again
Plan  in sync (10 resources, no changes)
CONVERGED  OK: 0 applied, 0 orphans
```

## The idea

Most multi-app agents are a sequence of tool calls. Sequences break
badly: a 2xx that silently did nothing is reported as success, a failure
at step four leaves steps one to three stranded, and re-running
duplicates everything.

`statelet` is a reconciler instead. It reads actual state, diffs against
desired state, applies only the difference — every write confirmed by a
readback, every applied write reversible.

Terraform for your people-ops stack, with an LLM translating intent.

## Setup

See [`docs/SETUP.md`](docs/SETUP.md) for VS Code and PyCharm, step by
step. Short version:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q          # 20 passed
```

## Try it in 90 seconds

No credentials required — the default backend is a tested in-memory
implementation of the same adapter contract the real apps use.

```bash
# 1. It provisions.
python -m statelet.cli apply --spec specs/new-hire.yaml

# 2. It is idempotent — no-op, zero writes issued.
python -m statelet.cli apply --spec specs/new-hire.yaml

# 3. Break state by hand: delete one thing, corrupt another.
#    It repairs exactly those two and nothing else.
python -m statelet.cli drift --spec specs/new-hire.yaml \
    --break slack:backend --empty notion:onboarding-checklist

# 4. Offboarding is the same engine, run backwards.
python -m statelet.cli offboard --spec specs/new-hire.yaml

# 5. Fault injection: 0 unexplained orphans across 1,000 trials.
python -m statelet.cli chaos --trials 25

# 6. Natural language in.
python -m statelet.cli plan "onboard Arjun as a data scientist, \
    arjun@acme.com, manager amit@acme.com, starting 2026-10-05"
```

## What makes it reliable

**Verified writes.** A write is not "applied" until a read confirms both
that the resource exists and that its content fingerprint matches. A 2xx
alone is not evidence — APIs accept writes and do nothing (missing
scope, permissions that fail open, eventual consistency, Slack's
`200 {"ok": false}`).

**Two kinds of drift.** A missing resource produces a CREATE; a resource
that exists but is *wrong* produces an UPDATE. A Notion page with the
right title and an emptied body is not in sync.

**Compensation.** Every operation has a known inverse by construction.
Applied writes are journalled; on failure the journal replays in reverse
and each rollback is itself verified. Rolling back retries even
permanent failures — a wasted call is cheaper than an orphan.

**A deterministic diff.** The LLM decides *what the goal is*. Set
arithmetic decides *what to change*. That is what makes idempotency
provable rather than hoped for.

**Refusal on partial reads.** If any app fails to read, nothing is
written. Acting on a partial view is how agents create duplicates.

Measurements, the four bugs the harness caught, and known gaps:
[`docs/RELIABILITY.md`](docs/RELIABILITY.md).

## Layout

```
statelet/
  core.py       Resource identity + content fingerprints, spec, diff
  engine.py     Verified writes, LIFO compensation, parallel reads
  adapters.py   The 5-method contract + tested in-memory app
  live.py       Slack, Notion, Linear, Google Calendar (+ OAuth refresh)
  planner.py    LLM: request -> validated spec (the only model call)
  chaos.py      Fault injection harness
  trace.py      Structured spans (one run = one trace)
  cli.py        Commands, and the demo surface
```

## Going live

```bash
export SLACK_BOT_TOKEN=xoxb-...
export NOTION_TOKEN=secret_...  NOTION_PARENT_PAGE_ID=...
export LINEAR_API_KEY=lin_api_...  LINEAR_TEAM_ID=...
export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...  GOOGLE_REFRESH_TOKEN=...

python -m statelet.cli doctor        # preflight all four
python -m statelet.cli apply --spec specs/new-hire.yaml --live --dry-run
```

`doctor` first, `--dry-run` second, live writes last. See
[`docs/AUTH.md`](docs/AUTH.md).



# statelet

**One AI agent that onboards a new hire across four apps — and proves
every write actually landed.**

Describe a new hire in one sentence. `statelet` makes it true across
Slack, Notion, Linear and Google Calendar, verifies every write by
reading it back, and rolls back cleanly if anything fails.

```
$ statelet plan "onboard Priya as a backend engineer, priya@acme.com,
                 manager amit@acme.com, starting 2026-09-21"

$ statelet apply --spec specs/new-hire.yaml --live
Plan  +9
CONVERGED  OK: 9 applied, 0 orphans

$ statelet apply --spec specs/new-hire.yaml --live      # run it again
Plan  in sync (9 resources, no changes)
CONVERGED  OK: 0 applied, 0 orphans
```

**[Watch the 2-minute demo](https://drive.google.com/file/d/1EdgDg7pEpK4FZjBS6RUmQJ9LtezXofJR/view?usp=sharing)**

Built in one day for the Multi-App AI Agent Hackathon. Verified against
four live APIs, not mocks.

## The idea

Most multi-app agents are a sequence of tool calls. Sequences break
three ways that only show up in production:

1. **A 2xx is treated as proof.** APIs accept writes and silently do
   nothing — a missing OAuth scope, permissions that fail open, eventual
   consistency, Slack returning `200 {"ok": false}`. The agent reports
   success and the new hire has no Notion access.
2. **Partial failure leaves orphans.** Step 4 fails; steps 1–3 already
   happened and nobody knows what to clean up.
3. **Re-running duplicates.** The retry that should be free creates a
   second set of every page, issue and event.

`statelet` is a reconciler instead. It reads actual state across all
four apps, diffs it against desired state, and applies only the
difference — every write confirmed by a readback, every applied write
reversible.

Terraform for your people-ops stack, with an LLM translating intent.

Offboarding is the same engine run backwards: reconcile to empty state.

## How it works

```
  request (natural language)
        │
        ▼
  planner ─── LLM decides WHAT THE GOAL IS ──► spec (validated, allowlisted)
        │
        ▼
  observe ─── parallel reads, all 4 apps ────► observed state
        │
        ▼
  diff ────── set arithmetic + fingerprints ─► plan
        │     (deterministic — no model here)
        ▼
  execute ─── per op: write → READ BACK → verify ──► journal
        │
        ├─ all verified ─────────────────► converged
        └─ any failure ──► compensate (LIFO inverse) ──► clean abort
```

**The model decides the goal. Set arithmetic decides the actions.** If
an LLM emitted writes directly, the same input could produce different
writes on different runs — idempotency would be unprovable and rollback
untrustworthy. Confining the model to a declarative spec means a model
mistake surfaces as a *wrong goal*, visible in `--dry-run` and
rejectable by a human, never as a surprise mutation in a live workspace.
Roles must match a known template and channels come from an allowlist,
so a hallucinated channel name cannot become a write.

## Setup

Python 3.10+. See [`docs/SETUP.md`](docs/SETUP.md) for VS Code and
PyCharm, step by step.

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest tests/ -q                           # 20 passed
```

## Try it in 90 seconds

No credentials needed. The default backend is a tested in-memory
implementation of the same adapter contract the real apps use — which is
why the fault harness can run a thousand deterministic trials.

```bash
# 1. One sentence in, a declarative spec out.
python -m statelet.cli plan "onboard Priya as a backend engineer, \
    priya@acme.com, manager amit@acme.com, starting 2026-09-21" \
    --out specs/demo.yaml

# 2. It provisions.
python -m statelet.cli apply --spec specs/demo.yaml --reset

# 3. It is idempotent — in sync, zero writes issued.
python -m statelet.cli apply --spec specs/demo.yaml

# 4. Break state by hand: delete one thing, corrupt another.
#    It repairs exactly those two and nothing else.
python -m statelet.cli drift --spec specs/demo.yaml \
    --break slack:backend --empty notion:onboarding-checklist

# 5. Offboarding is the same engine, run backwards.
python -m statelet.cli offboard --spec specs/demo.yaml

# 6. Fault injection: 0 unexplained orphans across 1,000 trials.
python -m statelet.cli chaos --trials 25
```

## What makes it reliable

**Verified writes.** A write is not "applied" until a read confirms both
that the resource exists and that its content fingerprint matches. A 2xx
alone is not evidence.

**Two kinds of drift.** A missing resource produces a CREATE; a resource
that exists but is *wrong* produces an UPDATE. A Notion page with the
right title and an emptied body is not in sync.

**Compensation.** Every operation has a known inverse by construction
(`CREATE` ⇄ `DELETE`, `UPDATE` ⇄ `UPDATE` to prior). Applied writes are
journalled; on failure the journal replays in reverse and each rollback
is itself verified. The rollback path retries even *permanent* failures,
because a wasted call is cheaper than an orphan.

**Never prune what you can't prove you created.** Slack channel
membership has no field to embed a marker in, so pruning would treat
every channel a human joined as an extra to delete. A managed namespace
restricts deletion to names a role template could have produced.

**Refusal on partial reads.** If any app fails to read, nothing is
written. Acting on a partial view is how agents create duplicates.

Full measurements, the nine bugs this found, and known gaps:
[`docs/RELIABILITY.md`](docs/RELIABILITY.md).

## Evidence

```
20 tests passing
1,000 fault-injection trials across 40 seeds
  aborts=318   clean-rollback=226/318   recovered-from-fault=642
  orphans: 0 unexplained | 149 persistent-blocked (itemised in a manifest)
4 live APIs — Slack, Notion, Linear, Google Calendar
```

Six fault modes are injected: `timeout`, `rate_limit`, `forbidden`,
`silent_noop`, `readback_lies`, `partial_write` — each as a one-write
blip *and* as a persistent condition, because a missing OAuth scope does
not fix itself on retry.

**Nine real bugs found before any of this was demoed.** Four by the
harness, five more by running against live APIs — including Notion's
eventually-consistent search index reporting a rollback as verified when
the page simply hadn't been indexed yet, and a prune that would have
deleted Slack channels a human joined. One hid behind a single test seed:
the suite passed on seed 7 while 63 orphans sat on other seeds, which is
why the test is now a sweep.

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
export NOTION_TOKEN=ntn_...  NOTION_PARENT_PAGE_ID=...
export LINEAR_API_KEY=lin_api_...  LINEAR_TEAM_ID=...
export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...  GOOGLE_REFRESH_TOKEN=...

python -m statelet.cli doctor                                    # preflight all four
python -m statelet.cli apply --spec specs/new-hire.yaml --live --dry-run
python -m statelet.cli apply --spec specs/new-hire.yaml --live
```

`doctor` first, `--dry-run` second, live writes last. Full credential
walkthrough — including the two Notion and Slack steps that fail
silently if skipped — in [`docs/AUTH.md`](docs/AUTH.md).

Set `ANTHROPIC_API_KEY` to use the model planner; without it, `plan`
falls back to deterministic parsing so the demo never depends on a
network call.

## Product surface

The CLI is the engine, not the product. The intended interfaces are a
Slack slash command (`/onboard priya@acme.com backend-engineer`) for
whoever knows a hire is starting, and a scheduled reconcile that repairs
drift before anyone notices. Both are thin wrappers over
`Reconciler.reconcile()` — the spec is already declarative and the
planner already accepts plain language, so neither needs engine changes.


## License

MIT.

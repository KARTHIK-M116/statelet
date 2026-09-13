# System and reliability brief

## What it does

Takes a plain-language request ("onboard Priya as a backend engineer,
priya@acme.com, manager amit@acme.com, starting 2026-09-21") and makes
it true across Slack, Notion, Linear and Google Calendar — 10 resources
across 4 apps in one run.

Not a provisioning script. A reconciler: it reads actual state, diffs
against desired state, applies only the difference. Run it twice and the
second run does nothing. Break something by hand and the next run
repairs exactly that.

## Why this shape

Multi-app agents are usually sequences of tool calls. Sequences have
three failure modes that only appear in production:

1. **A 2xx is treated as proof.** APIs accept writes and do nothing —
   missing OAuth scope, permissions that fail open, eventual
   consistency, Slack's `200 {"ok": false}`. The agent reports success
   and the new hire has no Notion access.
2. **Partial failure leaves orphans.** Step 4 fails; steps 1–3 already
   happened and nobody knows what to clean up.
3. **Re-running duplicates.** The retry that should be free creates a
   second set of everything.

Each is addressed structurally, not by prompt.

## Design

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
        │     (deterministic, no model here)
        ▼
  execute ─── per op: write → READ BACK → verify ──► journal
        │
        ├─ all verified ─────────────────► converged
        └─ any failure ──► compensate (LIFO inverse) ──► clean abort
                                 │
                                 └─ rollback blocked ──► cleanup manifest
```

### The boundary that matters

The model decides the goal. The diff engine decides the actions.

If an LLM emits writes directly, the same input can produce different
writes on different runs — idempotency becomes unprovable and rollback
untrustworthy. Confining the model to a declarative spec means a model
mistake surfaces as a *wrong goal*, visible in `--dry-run` and
rejectable by a human, never as a surprise mutation in a live workspace.

The spec is structurally constrained too: roles must match a known
template, Slack channels come from an allowlist. A hallucinated channel
is dropped and the drop is recorded in the trace.

### Identity drift vs content drift

A resource has a stable identity (app, kind, subject, name) and a
fingerprint over its tracked content. These catch different problems:

- **Identity drift** — the resource is gone. Produces a `CREATE`.
- **Content drift** — it exists but is wrong (a page whose body was
  emptied, an issue reassigned). Produces an `UPDATE`.

Verification checks both. A write that lands with the wrong content
fails `verify` even though the resource exists — the `partial_write`
fault mode in the harness exists specifically to test this.

`UPDATE` carries the observed prior state, so its inverse writes the old
content back rather than deleting the resource.

### Verified writes

A write is not `APPLIED` until a subsequent read confirms both presence
and fingerprint. A write returning success whose readback disagrees is
`UNVERIFIED`, which aborts the run.

### Compensation

Every operation has a known inverse by construction (`CREATE` ⇄
`DELETE`, `UPDATE` ⇄ `UPDATE` to prior). Applied writes are journalled;
on failure the journal replays in reverse (LIFO — later resources may
depend on earlier ones) and each rollback is itself verified.

`UNVERIFIED` writes are swept into compensation: the app returned
success, so we cannot assume nothing landed, and deletes are idempotent.

### Retry, and a deliberate asymmetry

Forward: transient faults (timeout, 429, 5xx) retry with backoff;
permanent faults (403, 404) abort immediately, because retrying a
deterministic failure burns clock and makes it look flaky.

Rolling back: **both** are retried. A 403 on the rollback path means we
are about to leave state behind, and a wasted call is far cheaper than
an orphan someone has to hunt down. This asymmetry removed the last 5
unexplained orphans in the sweep below.

### Refusing to act on partial state

If any adapter's read fails, the run aborts before any write. Acting on
a partial view would "create" resources that already exist in the app we
failed to read — the duplicate-provisioning bug this design prevents.

## How we know it works

### Test suite — 20 tests, one per claim

```
$ python -m pytest tests/ -q
20 passed
```

| Claim | Test |
|---|---|
| Provisions 10 resources across 4 apps | `test_provisions_from_empty` |
| Second run is a no-op, **zero writes** | `test_idempotent_rerun_issues_zero_writes` |
| Dry run writes nothing | `test_dry_run_writes_nothing` |
| Offboarding reuses the same engine | `test_offboarding_uses_the_same_engine` |
| Repairs identity drift only | `test_repairs_only_identity_drift` |
| Detects content drift, not just missing resources | `test_detects_content_drift_not_just_missing_resources` |
| Content drift converges on rerun | `test_content_drift_converges_on_rerun` |
| Rolling back an UPDATE restores prior content | `test_update_rollback_restores_prior_content` |
| Persistent silent no-op aborts, not "success" | `test_persistent_silent_noop_is_caught_not_trusted` |
| A single no-op blip self-heals | `test_single_silent_noop_blip_self_heals` |
| A write with wrong content fails verification | `test_partial_write_fails_verification` |
| Aborted run leaves zero orphans | `test_rollback_leaves_zero_orphans` |
| Transient fault retried, not compensated | `test_transient_fault_is_retried_not_compensated` |
| Compensation retries transient faults | `test_compensation_retries_transient_faults` |
| Compensation retries permanent faults too | `test_compensation_retries_permanent_faults_too` |
| Read failure writes nothing | `test_read_failure_refuses_to_act` |
| Parallel reads observe every app | `test_parallel_reads_hit_every_app` |
| Every orphan is itemised | `test_every_orphan_is_itemised_in_the_manifest` |
| Rerun converges after blocked rollback | `test_rerun_converges_after_blocked_rollback` |
| No unexplained orphans across a seed sweep | `test_chaos_suite_no_unexplained_orphans` |

### Chaos harness — 1,000 trials, 6 fault modes

`statelet chaos --trials 25 --seed N` injects a fault into a random app
at a random write. Modes: `timeout`, `rate_limit`, `forbidden`,
`silent_noop`, `readback_lies`, `partial_write`. Each is injected either
as a blip or as a **persistent** condition, because a missing OAuth
scope does not fix itself on retry.

Swept over 40 seeds:

```
1000 trials: aborts=318  clean-rollback=226/318  recovered-from-fault=642
orphans: 0 unexplained  |  149 persistent-blocked rollback (itemised)
```

- **642 runs hit a fault and still converged.** Transient faults are
  retried through rather than escalated.
- **318 aborted; 226 left zero state behind.**
- **0 unexplained orphans across 1,000 trials.** The suite asserts this
  over a seed sweep, not one seed — see below for why that matters.
- **149 persistent-blocked.** Honest, and explained next.

## The limit we did not engineer away

One failure class compensation cannot fix: when the injected fault
persists through the rollback path itself. A revoked permission, or a
delete endpoint that silently no-ops, leaves a resource that exists and
cannot be removed in-run.

We could have hidden this by not modelling persistent faults. Instead
the run emits a **cleanup manifest** — app, resource key, remote id, and
why rollback failed. And because reconciliation converges, re-running
once the permission is restored repairs state with no manual
bookkeeping; the manifest is a diagnostic, not a to-do list.
`test_rerun_converges_after_blocked_rollback` asserts this.

## What the harness found

Four real bugs, all in the mechanism that is the point of the project.
All found before any demo existed.

1. **A single silent no-op self-healed and never surfaced.** The first
   harness modelled only one-write blips, so retry masked the fault.
   The silent-no-op defence looked tested but was not. Fix: fault plans
   gained `repeat`; the persistent case got its own test.

2. **`UNVERIFIED` writes were never rolled back.** An unconfirmed write
   was excluded from the journal, so if it *had* landed it became a
   permanent orphan. 5 orphans per 25 trials. Fix: sweep unverified ops
   into compensation.

3. **Observed state aliased stored state.** The in-memory adapter
   returned live references, so an `UPDATE` mutated the very `prior`
   snapshot needed to undo it — rollback silently restored the wrong
   content. Fix: reads return copies. Caught by
   `test_update_rollback_restores_prior_content`.

4. **Compensation did not retry.** Forward writes retried transient
   faults; the rollback path attempted once. A persistent 429 during
   rollback became a permanent orphan. **This one hid behind a single
   test seed:** the suite passed on seed 7 while 63 orphans sat on other
   seeds. Fix: share retry logic between both paths, retry permanent
   failures on the rollback path only, and make the test a seed sweep.
   63 → 0.

Bug 4 is the argument for the whole approach. A one-seed test and a
clean demo would have shipped it.

## Known gaps

- **Live adapters are unverified against real APIs.** The HTTP shapes in
  `live.py` come from public docs; they have not been run against live
  credentials. The tested path is the in-memory backend. Run
  `statelet doctor` to preflight all four before trusting `--live`.
- **Attribute tracking is partial.** Notion tracks page body, Linear
  tracks assignee, Calendar tracks start date. Other fields (Slack
  channel topic, issue labels, event attendees) are not fingerprinted,
  so drift in those is invisible.
- **No concurrency control.** Two simultaneous reconciles for the same
  subject could both plan the same create. Idempotent creates limit the
  damage; there is no lock.
- **Compensation is not atomic.** A crash mid-rollback leaves a partial
  rollback. The next run converges, but the window exists.
- **Marker-based read scoping** means a human who edits the marker out
  of a resource's description makes it invisible to the reconciler.
- **Notion content updates delete and recreate blocks**, so page history
  and comments on those blocks are lost.

## Running it

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
python -m statelet.cli chaos --trials 25
python -m statelet.cli apply --spec specs/new-hire.yaml
python -m statelet.cli apply --spec specs/new-hire.yaml    # no-op
python -m statelet.cli drift --spec specs/new-hire.yaml \
    --break slack:backend --empty notion:onboarding-checklist
```

No credentials needed for any of the above.

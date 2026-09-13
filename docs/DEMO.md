# Two-minute demo script

Two minutes is ~300 spoken words: **one idea, three proofs**. Resist a
fourth.

The idea: *most agents can't tell you whether they actually worked. This
one can.*

Terminal at ~16pt, dark background, commands pre-loaded in shell history
so you never type during the take. Full rehearsal twice before
recording.

---

## 0:00–0:20 — The problem

Talk over a static terminal.

> "Multi-app agents are sequences of tool calls. That breaks three ways
> nobody sees in a demo: an API returns 200 and silently does nothing, a
> failure at step four leaves steps one to three stranded, and
> re-running duplicates everything.
>
> So I didn't build a sequence. I built a reconciler."

## 0:20–0:45 — Proof 1: provisions, then no-op

```bash
python -m statelet.cli plan "onboard Priya as a backend engineer, \
  priya@acme.com, manager amit@acme.com, starting 2026-09-21" \
  --out specs/demo.yaml
```

> "The model's only job is turning that sentence into a declarative
> spec. It never decides what to change."

```bash
python -m statelet.cli apply --spec specs/demo.yaml
python -m statelet.cli apply --spec specs/demo.yaml
```

> "Ten resources across four apps, each confirmed by reading it back —
> not by trusting the 200. Run it again: in sync, zero writes.
> Idempotent, because the diff is set arithmetic, not model output."

## 0:45–1:15 — Proof 2: drift repair (the one they remember)

```bash
python -m statelet.cli drift --spec specs/demo.yaml \
  --break slack:backend --empty notion:onboarding-checklist
```

> "Now I break state by hand. I pull her out of a Slack channel — and I
> empty her onboarding page, which is nastier: the page still exists,
> so an existence check says everything's fine.
>
> Reconcile. One create, one update. It caught the deletion *and* the
> corruption, and touched nothing else. That's the difference between a
> script and infrastructure — a script run twice does the work twice, a
> spec applied twice converges."

## 1:15–1:45 — Proof 3: it fails safely

```bash
python -m statelet.cli chaos --trials 25
```

> "Twenty-five runs with faults injected at random points — timeouts,
> 429s, 403s, writes that return success and change nothing, and writes
> that land with the wrong content.
>
> Across a thousand trials: six hundred and forty-two hit a fault and
> still converged. Three hundred aborted, and two hundred twenty-six of
> those left zero state behind. Zero unexplained orphans.
>
> The orphans that remain are honest — when the fault persists through
> the rollback path itself, the resource exists and I can't remove it.
> So it emits a cleanup manifest naming exactly what to look at, and
> because reconciliation converges, re-running once permissions are
> fixed repairs it.
>
> The harness found four real bugs before I ever demoed this. One hid
> behind a single test seed — passed on seed seven while sixty-three
> orphans sat on other seeds. That's why the test is a sweep."

## 1:45–2:00 — Close

> "Same engine does offboarding: reconcile to empty state. Twenty tests,
> one per claim in the brief.
>
> The thesis: an agent that can't verify its own writes isn't
> automation, it's a liability. This one verifies every write and can
> undo every one it made."

---

## Rules for the take

- **Never say "as you can see."** Say what it proves.
- **Don't explain the architecture.** The brief does that; the demo
  shows behaviour.
- **Don't apologise for the in-memory backend.** Frame it right: it's a
  tested implementation of the same adapter contract, which is *why*
  the chaos suite can run a thousand deterministic trials. Real
  adapters are in `live.py`.
- **Lead with drift repair if you get one shot.** Most visceral, hardest
  to fake.
- Record the in-memory version first as a safety net. Never let a live
  API be your only take.
- **Mention bug 4 if you have five seconds spare.** "A one-seed test and
  a clean demo would have shipped it" is the most credible sentence in
  the whole script, especially to judges who build agent observability.

## If live APIs break during the event

Cut live entirely, one sentence: "Live adapters are in the repo, written
against each app's public API; the reliability numbers come from the
in-memory backend so they're reproducible." Judges building
observability tooling will take a reproducible harness over a one-shot
live run that could have been lucky.

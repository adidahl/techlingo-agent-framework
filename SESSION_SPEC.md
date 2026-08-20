# TechLingo Session Spec — v2 (2026-08-20)

How the app composes a PRACTICE session from the imported exercise bank and
the learner's mastery state (MASTERY_SPEC.md). This is the Duolingo loop in
one algorithm: a confidence opener, mostly new material, the things fading
from memory, and your own past mistakes. The selected buckets retain their
meaning while one shared deterministic experience scheduler creates the exact
learner-facing order.

Scope note (bridge era): compiled LEVEL units (Lesson · Level 1/2/3), module
checkpoints, final review, and runtime **Practice / Review** all use the same
experience policy and scheduler. The compiler schedules selected bank items;
the runtime first selects its mastery buckets and then schedules that exact
selection with the warm-up pinned.

Reference implementation: `compose_session()` in
`src/techlingo_workflow/learning_specs.py`; vectors in
`tests/test_learning_specs.py`. Deterministic by design: a configured seed and
stable SHA-256 tie ranks replace process-global randomness, so implementations
are vector-testable.

## 1. Inputs

- **Pool**: candidate exercises — for course practice, every ACTIVE imported
  question of the course in curriculum order. Each carries (from `options`):
  `item_key`, `concept_id`, `rung` (1–5), `variant`, original learner
  mechanic, T/F answer where applicable, correct option indexes, Bloom level,
  module/lesson ownership, plus a per-user `seen` flag. Older imported items
  without the additive experience fields remain valid and use `unknown`.
- **User state** per concept: mastery row (MASTERY_SPEC) + `proven_rung` =
  highest rung ever answered correctly (0 = none).
- **Mistake queue**: item_keys answered wrong, oldest first; an item leaves
  the queue after **2** correct answers (`redeemed_count`).
- `session_size` (default **12**), `now`.

## 2. Composition (normative)

```
budget: 1 warm-up + body (session_size - 1)
  mistakes = min(queue length, floor(body * 0.15))
  review   = min(due concepts,  floor(body * 0.25))
  new      = body - mistakes - review          # unused capacity flows to NEW

1. WARM-UP — one easy win: among concepts with effective ≥ 0.55 pick the
   strongest (tie: concept_id); take its lowest-rung takeable item (tie:
   item_key). No strong concept ⇒ no warm-up (session is body-only).
2. MISTAKES — queue order (oldest first), skipping items no longer in the pool.
3. REVIEW — due concepts, weakest effective first (tie: concept_id), ONE item
   each: rung closest to the user's proven_rung (never above 5, floor 1),
   preferring UNSEEN variants of that cell, then lower rung, then item_key.
4. NEW — pool (curriculum) order, unseen items only, rung gate
   `rung ≤ proven_rung + 1` (never-seen concept ⇒ gate 1). If the gate leaves
   slots unfilled (small pools), a second pass relaxes the gate and takes any
   unseen item — acceptable: ordering still plays its lower rungs first.
5. SELECTION INVARIANTS — across the WHOLE session: max 2 selected items per
   concept; no item twice; bucket membership is frozen before ordering.
6. ORDER — warm-up is pinned first. Seeded bounded search schedules the body
   while preserving every selected item and its payload. Defaults:
   - no adjacent same-concept questions when feasible;
   - at most 2 consecutive questions of one original learner mechanic;
   - at most 2 consecutive questions of one rendered UI family (notably,
     `single_choice` and `multi_choice` both render as `multiple_choice`);
   - at most 2 consecutive identical T/F answers;
   - at least 3 mechanics in every full 6-question window when the selected
     pool exposes at least 3 known mechanics.
   Candidate scoring keeps a broadly easy-to-hard direction, retains
   mistake/review/new semantics, distributes recognition/production/scenario
   work, and discourages repeated T/F alternation and correct-option patterns.
7. RELAXATION — correctness, identity, schema, bucket membership, and selected
   coverage are never relaxed. Experience constraints relax cumulatively in
   this order: rolling-window diversity, T/F-answer streak, rendered UI-family
   streak, original-mechanic streak, concept adjacency. Every cumulative
   relaxation decision is scheduler-attested with its policy, scope, seed,
   predecessor profile, affected item keys, configured/observed value, and
   proof. Only an independently reproduced, mathematically unavoidable actual
   violation can waive a hard sequence issue. Exhausting the bounded search is
   a fatal diagnostic, never evidence that a constraint is infeasible.
```

## 3. In-session behavior (normative)

- Grade with GRADING_SPEC v1; update mastery with MASTERY_SPEC after EVERY
  answer (typo = correct). Wrong answers enqueue the item (or reset its
  `redeemed_count`).
- **De-escalation**: after 2 consecutive wrong answers, if a not-yet-played
  lower-rung item of the SAME concept as the last wrong answer exists in the
  session, move it to play next (one de-escalation per concept per session).
- Session end: no additional pass/fail — mastery already absorbed every
  answer. Show per-concept strength deltas if the UI wants a summary.

## 4. Target difficulty (informative)

The bucket shares + rung gates approximate Duolingo's ~80%-success sweet spot
without a learner model. Phase 4 telemetry (per-item p_correct) replaces the
rung-distance heuristic with measured difficulty; the composition algorithm
itself does not change.

## 5. Test vector (must pass exactly)

Pool (curriculum order; `seen` marked):

```
l1/alpha/r1/v1 seen   l1/alpha/r2/v1 seen   l1/alpha/r3/v1
l1/beta/r1/v1  seen   l1/beta/r1/v2         l1/beta/r2/v1
l2/gamma/r1/v1        l2/gamma/r2/v1        l2/gamma/r4/v1
```

User at day 10: alpha strong (strength 0.81436, half-life 10.4976, reviewed
day 10, proven R2) · beta due (0.32, 1.8, reviewed day 0, proven R1) · gamma
never seen. Mistake queue: `[l1/alpha/r2/v1]`. `session_size = 6`.

Expected: warm-up `l1/alpha/r1/v1`; mistakes `[]` (floor(5×0.15)=0); review
`[l1/beta/r1/v2]` (unseen variant of the proven cell); new
`[l1/alpha/r3/v1, l1/beta/r2/v1, l2/gamma/r1/v1, l2/gamma/r2/v1]` (last one
via the relaxed pass); final order:

```
l1/alpha/r1/v1 · l1/beta/r1/v2 · l2/gamma/r1/v1 · l1/beta/r2/v1 · l1/alpha/r3/v1 · l2/gamma/r2/v1
```

With `session_size = 12` and queue `[l2/gamma/r2/v1]`: mistakes takes it
(floor(11×0.15)=1). Caps: never more than 2 items of one concept.

## 6. Versioning

v2 is an additive metadata migration plus an intentional ordering change. The
mastery bucket shares, rung gates, concept cap, and mistake semantics from v1
are unchanged. Old callers may omit every new `PoolItem` field and retain a
valid deterministic session; known metadata activates the stronger experience
constraints. Shares, caps, thresholds, ordering, or tie-break changes bump the
major version, together with MASTERY_SPEC when coupled. Same-commit rule as
always.

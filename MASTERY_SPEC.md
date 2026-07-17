# TechLingo Mastery Spec — v1 (2026-07-17)

How the app tracks WHAT a learner knows, per concept, over time — the engine
behind reviews, practice sessions, and (later) crown/legendary gating.
Companion of SESSION_SPEC.md (which consumes these numbers) and GRADING_SPEC.md
(which decides correct/typo/wrong per answer).

Reference implementation: `src/techlingo_workflow/learning_specs.py` (Python)
— normative, with test vectors in `tests/test_learning_specs.py`. The app's
TypeScript port must pass the identical vectors
(TECHLINGO_APP_LEARNING_ENGINE_TASK.md).

Design principle: **mastery is per concept, not per question.** Every imported
question carries `options.concept_id` and `options.rung` (1–5); questions are
interchangeable probes of a concept at a difficulty rung. This is what lets a
republished course (new questions, same concept ids) keep every learner's
progress — concept ids are immutable once published (ARCHITECTURE.md D10).

## 1. State

One row per `(user, course, concept)`:

| field | type | initial |
|---|---|---|
| `strength` | float 0..1 | 0.0 |
| `half_life_days` | float | 0.0 (never answered correctly yet) |
| `last_review_at` | timestamp | null |

## 2. Constants (v1 — change ⇒ version bump)

```
gain per rung:   R1 0.15 · R2 0.20 · R3 0.30 · R4 0.35 · R5 0.40
correct:         half_life ×= 1.8   (first-ever correct sets it to 1.0 day; cap 90 days)
wrong:           strength ×= 0.5;  half_life ×= 0.5  (floor 0.5 days)
due threshold:   effective < 0.55
mastered:        effective ≥ 0.80   (informative; used by post-bridge level gating)
rounding:        6 decimals after every update/decay computation
```

Production evidence moves strength more than recognition: typing "LLM" (R3)
proves more than picking it from options (R1).

## 3. Rules (normative)

**Decay.** Effective strength at time `now`:

```
effective = strength * 2^(-(now - last_review_at) / half_life_days)
```

(0.0 when the concept was never answered correctly, i.e. `half_life_days = 0`.)

**Update on a graded answer** (a `typo` grade counts as correct — GRADING_SPEC v1):

```
current = effective(now)                       # update what they REMEMBER, not what they once had
correct:  strength' = current + (1 - current) * gain(rung)
          half_life' = 1.0 if first-ever correct else min(half_life * 1.8, 90)
wrong:    strength' = current * 0.5
          half_life' = max(half_life * 0.5, 0.5)
last_review_at' = now
```

**Due.** A concept is due for review iff it has been seen
(`last_review_at != null`) and `effective < 0.55`. Never-seen concepts are
"new", not "due" — SESSION_SPEC treats them separately.

## 4. Test vectors (must pass exactly)

**V1 — ladder climb** (all correct, same day, rungs 1→5):

| answer | strength | half_life |
|---|---|---|
| R1 ✓ | 0.15 | 1.0 |
| R2 ✓ | 0.32 | 1.8 |
| R3 ✓ | 0.524 | 3.24 |
| R4 ✓ | 0.6906 | 5.832 |
| R5 ✓ | 0.81436 | 10.4976 |

A concept climbed through all five rungs lands mastered (≥ 0.80) — by design.

**V2 — decay of the V1 end state** (answered day 0):

| day | effective | due? |
|---|---|---|
| 0 | 0.81436 | no |
| 3 | 0.668018 | no |
| 7 | 0.51296 | **yes** |
| 10.4976 | 0.40718 (exactly half) | yes |
| 21 | 0.203525 | yes |
| 60 | 0.015497 | yes |

**V3 — wrong then recover:** V1 state; wrong R2 at day 7 → `(0.25648, 5.2488)`;
correct R3 at day 8 → `(0.457326, 9.44784)`.

**V4 — first-ever answer wrong:** `(0.0, 0.5)`; seen + effectively zero ⇒ due
immediately.

**V5 — long gap:** V1 state, correct R1 at day 30. Effective pre-answer is
0.11234 — the update builds on THAT: → `(0.245489, 18.89568)`.

**V6 — caps:** growth capped at 90 days; shrink floored at 0.5 days.

## 5. Versioning

Constants, formulas, or rounding changes bump the major version. The spec, the
Python reference, and the vectors live in the same repo and must change in the
same commit; the app tracks the spec version it implements.

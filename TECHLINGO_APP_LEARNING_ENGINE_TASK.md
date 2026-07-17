# Task: Implement the v1 Learning Engine (Mastery + Practice Sessions) in the TechLingo App

**Audience:** the agent working in the TechLingo application codebase (mobile +
web). Self-contained — reference implementation and test vectors included. No
access to the course-generator repo is required. Pattern and ground rules are
the same as the grading task you already shipped
(TECHLINGO_APP_GRADING_TASK.md): port faithfully, pass the vectors bit-for-bit.

## Why

The generator now ships courses whose every question carries a stable
**concept identity and difficulty rung**. That unlocks the Duolingo learning
loop — knowing what a learner is forgetting and practicing exactly that. Two
features to build:

1. **Mastery tracking** — per-concept strength that decays over time
   (MASTERY_SPEC v1).
2. **Practice sessions** — "Practice" composes a ~12-item session: one easy
   warm-up, new material, fading concepts, the learner's past mistakes
   (SESSION_SPEC v1).

## Data contract (already live in imported courses)

Every imported question's `options` jsonb carries:

```jsonc
{
  "concept_id": "image-understanding-tasks",   // stable across republishes — mastery keys on THIS
  "item_key": "how-ai-works-with-images/image-understanding-tasks/r4/v1", // stable item identity
  "rung": 4                                     // difficulty rung 1-5
}
```

The bundle's `concepts.json` lists every concept (id, label, summary, lessons,
available rungs) — import it so practice can show concept labels. Treat
missing fields as absent-feature (old courses): questions without
`concept_id`/`rung` don't update mastery and only appear in the NEW bucket.

**Never** key mastery or analytics on the positional question `import_key`
(`...-q7`) — those are import-slot identifiers and shift on reimport.

## Storage (suggested DDL — adapt names to house style)

```sql
create table user_concept_strength (
  user_id uuid not null,
  course_key text not null,
  concept_id text not null,
  strength double precision not null default 0,
  half_life_days double precision not null default 0,
  last_review_at timestamptz,
  proven_rung int not null default 0,          -- highest rung answered correctly
  updated_at timestamptz not null default now(),
  primary key (user_id, course_key, concept_id)
);

create table mistake_queue (
  user_id uuid not null,
  course_key text not null,
  item_key text not null,
  concept_id text,
  times_missed int not null default 1,
  redeemed_count int not null default 0,        -- leaves the queue at 2
  last_missed_at timestamptz not null default now(),
  primary key (user_id, course_key, item_key)
);

-- per-user seen flag can live wherever answers are already recorded;
-- the composer only needs a boolean per (user, item_key).
```

## Rules (normative — full prose in MASTERY_SPEC.md / SESSION_SPEC.md)

Port this TypeScript faithfully. It is a 1:1 port of the Python reference the
vectors were generated from. All intermediate values round to 6 decimals
(`round6`); the vectors contain no rounding ties, so `Math.round` semantics
reproduce them exactly.

```typescript
// ===== MASTERY v1 =====
export interface ConceptMastery {
  strength: number;        // 0..1
  halfLifeDays: number;    // 0 = never answered correctly yet
  lastReviewAt: number | null; // epoch DAYS (not ms)
}

const GAIN_BY_RUNG: Record<number, number> = { 1: 0.15, 2: 0.20, 3: 0.30, 4: 0.35, 5: 0.40 };
const WRONG_STRENGTH_FACTOR = 0.5;
const HALF_LIFE_GROWTH = 1.8;
const HALF_LIFE_SHRINK = 0.5;
const INITIAL_HALF_LIFE_DAYS = 1.0;
const MIN_HALF_LIFE_DAYS = 0.5;
const MAX_HALF_LIFE_DAYS = 90.0;
export const DUE_THRESHOLD = 0.55;
export const MASTERED_THRESHOLD = 0.80;

const round6 = (x: number): number => Math.round(x * 1e6) / 1e6;

export function effectiveStrength(m: ConceptMastery, nowDays: number): number {
  if (m.lastReviewAt === null || m.halfLifeDays <= 0) return 0.0;
  const elapsed = Math.max(0, nowDays - m.lastReviewAt);
  return round6(m.strength * Math.pow(2, -elapsed / m.halfLifeDays));
}

/** `typo` grades count as CORRECT (grading spec v1). */
export function applyAnswer(
  m: ConceptMastery, rung: number, correct: boolean, nowDays: number,
): ConceptMastery {
  const gain = GAIN_BY_RUNG[rung];
  if (gain === undefined) throw new Error(`unknown rung ${rung}`);
  const current = effectiveStrength(m, nowDays);
  let strength: number, halfLife: number;
  if (correct) {
    strength = current + (1 - current) * gain;
    halfLife = m.halfLifeDays <= 0
      ? INITIAL_HALF_LIFE_DAYS
      : Math.min(m.halfLifeDays * HALF_LIFE_GROWTH, MAX_HALF_LIFE_DAYS);
  } else {
    strength = current * WRONG_STRENGTH_FACTOR;
    halfLife = Math.max(m.halfLifeDays * HALF_LIFE_SHRINK, MIN_HALF_LIFE_DAYS);
  }
  return { strength: round6(strength), halfLifeDays: round6(halfLife), lastReviewAt: nowDays };
}

export function isDue(m: ConceptMastery, nowDays: number): boolean {
  if (m.lastReviewAt === null) return false; // never-seen = NEW, not due
  return effectiveStrength(m, nowDays) < DUE_THRESHOLD;
}

// ===== SESSION v1 =====
export interface PoolItem {
  itemKey: string; conceptId: string; rung: number; variant?: number; seen: boolean;
}
export interface UserConceptState {
  conceptId: string; mastery: ConceptMastery; provenRung: number; // 0 = none
}
export interface SessionPlan {
  warmup: string[]; new: string[]; review: string[]; mistakes: string[]; finalOrder: string[];
}

const MISTAKES_SHARE = 0.15;
const REVIEW_SHARE = 0.25;
const MAX_ITEMS_PER_CONCEPT = 2;
export const MISTAKE_REDEEMED_AFTER = 2;

export function composeSession(
  pool: PoolItem[],                       // curriculum order
  userState: Map<string, UserConceptState>,
  mistakeQueue: string[],                 // item_keys, oldest first
  nowDays: number,
  sessionSize = 12,
): SessionPlan {
  const byKey = new Map(pool.map(p => [p.itemKey, p]));
  const conceptCount = new Map<string, number>();
  const chosen = new Set<string>();
  const plan: SessionPlan = { warmup: [], new: [], review: [], mistakes: [], finalOrder: [] };

  const canTake = (p: PoolItem) =>
    !chosen.has(p.itemKey) && (conceptCount.get(p.conceptId) ?? 0) < MAX_ITEMS_PER_CONCEPT;
  const take = (p: PoolItem, bucket: string[]) => {
    bucket.push(p.itemKey); chosen.add(p.itemKey);
    conceptCount.set(p.conceptId, (conceptCount.get(p.conceptId) ?? 0) + 1);
  };

  // 1. warm-up: strongest non-due concept, its lowest-rung takeable item.
  const strong = [...userState.values()]
    .filter(s => s.mastery.lastReviewAt !== null && effectiveStrength(s.mastery, nowDays) >= DUE_THRESHOLD)
    .map(s => [s, effectiveStrength(s.mastery, nowDays)] as const)
    .sort((a, b) => b[1] - a[1] || a[0].conceptId.localeCompare(b[0].conceptId));
  for (const [state] of strong) {
    const cands = pool
      .filter(p => p.conceptId === state.conceptId && canTake(p))
      .sort((a, b) => a.rung - b.rung || a.itemKey.localeCompare(b.itemKey));
    if (cands.length) { take(cands[0], plan.warmup); break; }
  }

  const mistakesAvail = mistakeQueue.filter(k => byKey.has(k) && !chosen.has(k));
  const dueStates = [...userState.values()]
    .filter(s => isDue(s.mastery, nowDays))
    .map(s => [s, effectiveStrength(s.mastery, nowDays)] as const)
    .sort((a, b) => a[1] - b[1] || a[0].conceptId.localeCompare(b[0].conceptId));

  const body = Math.max(0, sessionSize - 1);
  const nMistakes = Math.min(mistakesAvail.length, Math.floor(body * MISTAKES_SHARE));
  const nReview = Math.min(dueStates.length, Math.floor(body * REVIEW_SHARE));
  const nNew = body - nMistakes - nReview;

  // 2. mistakes — oldest first.
  for (const key of mistakesAvail) {
    if (plan.mistakes.length >= nMistakes) break;
    const item = byKey.get(key)!;
    if (canTake(item)) take(item, plan.mistakes);
  }

  // 3. review — weakest due concepts, one item each, near the proven rung,
  //    unseen variants first.
  for (const [state] of dueStates) {
    if (plan.review.length >= nReview) break;
    const target = state.provenRung ? Math.max(1, Math.min(state.provenRung, 5)) : 1;
    const cands = pool
      .filter(p => p.conceptId === state.conceptId && canTake(p))
      .sort((a, b) =>
        Math.abs(a.rung - target) - Math.abs(b.rung - target)
        || Number(a.seen) - Number(b.seen)
        || a.rung - b.rung
        || a.itemKey.localeCompare(b.itemKey));
    if (cands.length) take(cands[0], plan.review);
  }

  // 4. new — curriculum order, unseen, rung gate provenRung+1; relaxed second
  //    pass fills any shortfall.
  for (const item of pool) {
    if (plan.new.length >= nNew) break;
    if (!canTake(item) || item.seen) continue;
    const nextRung = (userState.get(item.conceptId)?.provenRung ?? 0) + 1;
    if (item.rung > nextRung) continue;
    take(item, plan.new);
  }
  if (plan.new.length < nNew) {
    for (const item of pool) {
      if (plan.new.length >= nNew) break;
      if (canTake(item) && !item.seen) take(item, plan.new);
    }
  }

  // 5-6. order: warm-up pinned; body rung-ascending, ties bucket then key;
  //       then break same-concept adjacency by swapping ahead.
  const bucketRank = new Map<string, number>();
  [plan.mistakes, plan.review, plan.new].forEach((b, r) => b.forEach(k => bucketRank.set(k, r)));
  const bodyKeys = [...bucketRank.keys()].sort((a, b) =>
    byKey.get(a)!.rung - byKey.get(b)!.rung
    || bucketRank.get(a)! - bucketRank.get(b)!
    || a.localeCompare(b));
  const ordered = [...plan.warmup, ...bodyKeys];
  for (let i = 1; i < ordered.length - 1; i++) {
    if (byKey.get(ordered[i])!.conceptId === byKey.get(ordered[i - 1])!.conceptId) {
      for (let j = i + 1; j < ordered.length; j++) {
        if (byKey.get(ordered[j])!.conceptId !== byKey.get(ordered[i - 1])!.conceptId) {
          [ordered[i], ordered[j]] = [ordered[j], ordered[i]];
          break;
        }
      }
    }
  }
  plan.finalOrder = ordered;
  return plan;
}
```

## Wiring requirements

1. **After every graded answer** (lesson, level, checkpoint, practice — every
   surface): if the question has `concept_id` + `rung`, run `applyAnswer`
   (typo ⇒ correct) and update `proven_rung = max(proven_rung, rung)` on
   correct. Wrong ⇒ upsert into `mistake_queue` (reset `redeemed_count`);
   correct on a queued item ⇒ `redeemed_count += 1`, delete at 2.
2. **Practice surface**: build the pool from the course's active questions in
   curriculum order, compose with `composeSession`, play with existing
   renderers + GRADING_SPEC grading.
3. **De-escalation** (in-session): after 2 consecutive wrong answers, if the
   session still holds an unplayed lower-rung item of the same concept, play
   it next (max once per concept per session).
4. **Republish safety**: a course reimport must not touch
   `user_concept_strength` (concept ids are stable). Items that disappear
   simply stop being served; stale `mistake_queue` rows are skipped by the
   composer (items not in the pool) and may be garbage-collected.

## Test vectors (must all pass — port as unit tests)

Mastery (all sequences start from `{strength: 0, halfLifeDays: 0, lastReviewAt: null}`):

| # | sequence | expected (strength, halfLife) |
|---|---|---|
| V1 | R1✓,R2✓,R3✓,R4✓,R5✓ all at day 0 | 0.15/1.0 → 0.32/1.8 → 0.524/3.24 → 0.6906/5.832 → 0.81436/10.4976 |
| V2 | V1 state, effective at day 3 / 7 / 10.4976 / 21 / 60 | 0.668018 (not due) / 0.51296 (due) / 0.40718 / 0.203525 / 0.015497 |
| V3 | V1 state → R2✗ at day 7 → R3✓ at day 8 | 0.25648/5.2488 → 0.457326/9.44784 |
| V4 | R1✗ at day 0 (first ever) | 0.0/0.5; effective 0.0; due=true |
| V5 | V1 state → R1✓ at day 30 | effective pre-answer 0.11234 → 0.245489/18.89568 |
| V6 | (0.9, 80, day 0) → R5✓ day 1; (0.4, 0.8, day 0) → R1✗ day 0 | halfLife caps at 90.0; floors at 0.5 |

Session (exact scenario + expected buckets and final order): see
SESSION_SPEC.md §5 — port it verbatim, including the `sessionSize: 12`
mistakes-bucket case and the 2-per-concept cap property.

## Acceptance criteria

1. All mastery + session vectors pass as unit tests (bit-for-bit).
2. Answering questions in ANY surface updates mastery; a Practice session
   composed for a mid-course learner contains warm-up + new + due + mistakes
   per the shares, never two same-concept items adjacent, never >2 per concept.
3. A course reimport (same concept ids, changed questions) leaves every
   learner's strength rows untouched and Practice keeps working.
4. Old courses without `concept_id`/`rung` degrade gracefully: no mastery
   writes, Practice falls back to NEW-bucket-only composition.
5. Do not log more than the app already logs at answer time.

## Versioning

This implements **mastery-v1** and **session-v1** (generator repo:
MASTERY_SPEC.md, SESSION_SPEC.md, reference `learning_specs.py`, vectors
`tests/test_learning_specs.py`). Deviations require a coordinated spec bump —
never diverge silently.

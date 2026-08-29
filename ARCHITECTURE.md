# TechLingo Platform Architecture — Long-Term Plan

> Status: **approved direction** (2026-07-16). This document is the single source
> of truth for the target architecture and the phased path to it. It supersedes
> the run-centric view of the pipeline; existing docs (RESILIENCE_PLAN.md,
> GRADING_SPEC.md, TECHLINGO_OUTPUT_PLAN.md) remain valid for the layers they
> describe and are referenced where relevant.
>
> Agreed with the product owner:
> 1. Phase 3 (learning runtime) WILL be implemented in the TechLingo app
>    following specs shipped from this repo (same pattern as the grading spec).
> 2. In Phase 2, a lesson level is emitted as a **separate unit** in the app DB
>    (bridge solution, zero app changes); a first-class `level` model arrives
>    with Phase 3.

---

## 0. Goals & non-goals

**Goals**

- G1. Any course from any folder: N markdown source files → complete course,
  no per-course code.
- G2. Content quality ≥ 9/10, protected by automated evaluation (not vibes).
- G3. Learning experience ≥ 9/10 measured against the Duolingo loop:
  levels, recycling, spaced repetition, practice, mistakes replay, variety.
- G4. A course is a **living entity**: regenerable incrementally, human-editable
  forever, publishable repeatedly without destroying learner progress.
- G5. Manual (human-authored) materials — video, audio, text — are first-class
  citizens next to generated content and survive every regeneration.
- G6. $0 marginal generation cost stays viable (subscription CLI backends).

**Non-goals**

- No graph/vector database, no microservices, no queue infrastructure. The
  scale (thousands of concepts, not billions) never justifies them.
- No LLM calls anywhere after the Content Factory. Compiler, grader, session
  composer, mastery model: deterministic code only.
- No ML personalization (Duolingo "birdbrain") before real telemetry exists.
  SM-2-lite + an 80%-success session target gives ~90% of the effect.
- No standalone CMS product. The existing web app evolves into the workspace UI.

---

## 1. Core principles

1. **The concept is the atom, not the question.** Questions are interchangeable
   probes of a concept at a given difficulty rung. Mastery, recycling, reviews,
   and telemetry all key on `concept_id`.
2. **Three planes, three lifecycles.** LLM Content Factory (slow, batch, $0) →
   deterministic Curriculum Compiler (instant, pure) → Learning Runtime in the
   app (spec-driven). Never mix responsibilities across planes.
3. **Files + git are the authoring database.** Canonical content lives as
   structured files in a course workspace; SQLite is a rebuildable index, never
   a source of truth. The app's Postgres remains the runtime store.
4. **Everything after the factory is deterministic and seeded.** Same workspace
   + same seed → byte-identical bundle. This is what makes the compiler
   testable and re-runnable for free.
5. **Specs govern the app boundary.** Runtime behavior we don't own is fixed by
   a spec + reference implementation + test vectors (proven with
   GRADING_SPEC.md / TECHLINGO_APP_GRADING_TASK.md).
6. **Stable identity everywhere.** `concept_id` and `item_key` are immutable
   once published; positional `import_key`s remain an app-import detail, never
   a telemetry or mastery key.

---

## 2. System overview

```
   SOURCES                 CONTENT FACTORY              CURRICULUM COMPILER            LEARNING RUNTIME
┌─────────────┐      ┌─────────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐
│ courses/<id>│      │ A0 per file → concept   │      │ path topology       │      │ TechLingo app       │
│  /sources/  │ ───▶ │ graph merge → cell-     │ ───▶ │ levels (units) +    │ ───▶ │ mastery (SM-2-lite) │
│  N × .md    │      │ addressed generation    │      │ recycling +         │      │ session composer    │
│ + authored  │      │ (A2–A4) → quality gates │      │ checkpoints →       │      │ mistakes replay     │
│ video/audio │      │ (A5) — LLM, hours, $0   │      │ bundle — pure fn,   │      │ telemetry events    │
│ /text units │      │ INCREMENTAL by hash     │      │ seconds, seeded     │      │ (per MASTERY_SPEC / │
└─────────────┘      └─────────────────────────┘      └─────────────────────┘      │  SESSION_SPEC)      │
                                                                                   └──────────┬──────────┘
       ▲                                                                                      │
       │                        nightly telemetry sync: p(correct) per item                   │
       └──────────────────── flags too-easy / too-hard items for regeneration ◀───────────────┘

     workspace on disk: git-versioned files (canonical) + .index.sqlite (derived, disposable)
```

---

## 3. Data model

### 3.1 Concept (the atom)

```yaml
# courses/<course-id>/graph/concepts.yaml
schema_version: concept-graph-v1
concepts:
  - id: llm-vs-slm                # slug; IMMUTABLE once published (mastery keys on it)
    label: "LLM vs SLM"
    summary: "LLMs are large general models; SLMs are smaller, focused, cheaper to run."
    depth: mechanism              # fact | mechanism | decision  (drives required rungs, §3.2)
    confusable_with: [model-training, fine-tuning]   # concept ids — distractor & rejected_answers source
    source: { file: "1. Introduction to AI concepts.md", heading: "How does generative AI work?" }
    prerequisites: []             # light, optional; the path stays LINEAR (Duolingo lesson)
    provenance: generated         # generated | human
    pinned: false                 # pinned ⇒ factory never touches it
```

`depth` encodes how far a concept can be meaningfully drilled:

| depth | meaning | required rungs |
|---|---|---|
| `fact` | a definition/term ("NFKC", "token") | R1–R3 |
| `mechanism` | how something works (LLM vs SLM trade-off) | R1–R4 |
| `decision` | when to choose it (service/approach selection) | R1–R5 |

### 3.2 The rung ladder (difficulty per concept)

The ladder composes the two existing axes — Bloom level (cognitive) and
mechanic (recognition → production) — into one ordered scale. It reuses the
existing Bloom/type coupling rules (RESILIENCE_PLAN §10) unchanged.

| rung | name | mechanic | internal types | Bloom |
|---|---|---|---|---|
| R1 | Recognize | pick the right option | single_choice / multi_choice | Remembering |
| R2 | Judge | verify/complete a statement | true_false (+ easy fill) | Understanding |
| R3 | Produce | type or construct the answer | fill_gaps, rearrange | Remembering/Understanding |
| R4 | Apply | scenario with a decision point | scenario single/multi choice | Applying |
| R5 | Analyze | discriminate between plausible approaches | scenario choice/multi | Analyzing/Evaluating |

This is exactly Duolingo's crown mechanic (same content, escalating production
demand) plus the exam-readiness top (AI-900/901 exams are scenario choice — R4/R5
is what certification actually tests).

### 3.3 Exercise bank (oversampled, cell-addressed)

Every item is addressed by **cell** `(concept_id, rung)` and a **variant** index.
Variants let higher levels and reviews repeat a *concept* without repeating the
*question* — the difference between a boring quiz and the Duolingo feel.

```jsonc
// courses/<course-id>/bank/<lesson-key>.json
{
  "schema_version": "exercise-bank-v1",
  "lesson": "what-is-generative-ai",
  "items": [
    {
      "item_key": "what-is-generative-ai/llm-vs-slm/r2/v1",   // stable identity
      "concept_id": "llm-vs-slm",
      "rung": 2,
      "variant": 1,
      "payload": { /* internal Exercise model from models.py — UNCHANGED */ },
      "provenance": "generated",       // generated | human-edited | human-authored
      "pinned": false,
      "status": "active",              // active | retired | flagged
      "source_hash": "sha256:…",       // source content hash at generation time
      "telemetry": { "p_correct": null, "n": 0, "typo_rate": null }   // Phase 4 sync
    }
  ]
}
```

**Generation quota per concept** (tunable defaults):

| depth | R1 | R2 | R3 | R4 | R5 | items/concept |
|---|---|---|---|---|---|---|
| fact | 2 | 2 | 1 | – | – | 5 |
| mechanism | 2 | 2 | 2 | 1 | – | 7 |
| decision | 1 | 2 | 2 | 2 | 2 | 9 |

With 6–8 concepts per lesson the full envelope yields a bank of ~40–60
items/lesson — roughly 3–4× oversampled vs. any single session (12–15 items).
A course may instead declare `workflow.worksheet_items_per_lesson`. The
budgeted worksheet keeps one mandatory variant for every depth-required rung,
alternates the remaining rows across recyclable R1/R2 and R3/R4 capacity,
uses R5 optional rows only after those bands fill, then balances each band
across concepts with stable tie ranks. The budget must lie between complete
ladder coverage and the full envelope; infeasible A1 maps are rejected before
generation. Nothing generated is post-hoc discarded. A tight feasible budget
can still make an exact cross-level repeat unavoidable when it leaves too few
optional rows; within-unit identity uniqueness remains absolute. AI-901 fixes
this budget at 30 active items per lesson. Cost is wall time only on
subscription backends; incremental builds (§4.3) keep iteration practical.

### 3.4 Course workspace layout

```
courses/<course-id>/
  course.yaml            # id, title, difficulty, locale, backend defaults
  compile.yaml           # compiler config: levels, recycle ratios, seed (§5)
  sources/               # the input .md files (copied in; hashes tracked)
  graph/concepts.yaml    # concept graph (canonical, provenance-marked)
  curriculum.yaml        # modules → lessons → ordered concept ids, titles, SLOs
  bank/<lesson-key>.json # exercise bank per lesson (canonical)
  authored/              # human-authored units/pages (video, audio, text) — never touched by the factory
  build_state.json       # last-built source hashes (committed → incremental builds work anywhere)
  dist/                  # emitted bundles (derived; git-ignored or LFS)
  .index.sqlite          # derived query index (git-ignored, rebuildable)
```

`curriculum.yaml` (teaching order) is separate from `concepts.yaml` (knowledge)
on purpose: reordering lessons or renaming titles must never look like a
knowledge change to the incremental builder.

```yaml
# curriculum.yaml
schema_version: curriculum-v1
modules:
  - key: intro-to-ai              # 1 source file = 1 module by default (overridable)
    title: "Introduction to AI concepts"
    lessons:
      - key: what-is-generative-ai
        title: "What Is Generative AI?"
        slo: "Explain what generative AI produces and how prompts drive it."
        concepts: [generative-ai-definition, llm-vs-slm, prompts-and-responses]
      - key: agents-basics
        title: "AI Agents: Model, Instructions, Tools"
        concepts: [agent-definition, agent-elements, knowledge-vs-action-tools]
  - key: authored-deep-dive       # human module: lessons reference authored/ pages
    title: "Hands-on with Azure AI (video)"
    authored: true
```

### 3.5 Provenance & pinning (the regeneration contract)

Every concept and item carries `provenance` ∈ {`generated`, `human-edited`,
`human-authored`} and `pinned: bool`. The factory obeys one rule:

> **Regeneration may only replace `generated`, unpinned items whose
> `source_hash` is stale.** `human-edited` and `human-authored` content, and
> anything pinned, is read-only to the factory. The web editor sets
> `provenance: human-edited` automatically on save.

This is what makes "the user adds video/audio/text manually" and "the system
regenerates freely" coexist without fear.

### 3.6 SQLite index (derived)

`index.py` rebuilds `.index.sqlite` from workspace files (on demand + file
watch in the server). Tables: `concepts`, `items`, `coverage` (concept × rung
matrix), `duplicates` (pairwise similarity above thresholds), `flags`. Serves
the CMS dashboards and validators with fast queries. Deleting the file is
always safe.

---

## 4. Content Factory (pipeline evolution)

The A0–A5 stages, chunked per-lesson generation, retry/repair loops, and all
deterministic gates from RESILIENCE_PLAN.md carry over. What changes is
**addressing** (cells instead of lesson quotas) and **scope** (course = N files).

### 4.1 Multi-document ingestion & graph merge

1. **A0 per source file** (unchanged prompt) → per-file inventories.
2. **Graph merge** (new): merge per-file inventories into one course-wide
   concept graph. Cross-file near-duplicates (e.g. "generative AI" introduced
   in files 1 and 2) are merged via label/summary similarity (token Jaccard
   now; embedding assist later — stored as SQLite blobs, still no vector DB).
   An LLM tie-break pass resolves ambiguous merges only.
3. **Concept ID stability** (critical): when a graph already exists, the merge
   MATCHES new extractions onto existing concepts (by id, label, then
   similarity) instead of minting new ids. Published concept ids are immutable
   (G4 — mastery survives). Unmatched old concepts are marked `retired`, never
   deleted.
4. **A1 evolves into the curriculum planner**: consumes the merged graph, emits
   `curriculum.yaml`. Default topology: **1 source file = 1 module**; lesson
   count proportional to concept count (~6–8 concepts/lesson), NOT a fixed
   global `modules_count`. `_validate_a1_map` (structural check + retries)
   carries over with per-file expectations.

### 4.2 Cell-addressed generation (A2–A4)

- A2 receives, per lesson: full source text (unchanged), the lesson's concept
  pack (unchanged), and a **cell worksheet** — the exact list of
  `(concept, rung, variant)` items to produce, derived deterministically from
  the quota table and any explicit per-course worksheet budget (§3.3). This
  replaces the per-lesson Bloom/type plan; `_bloom_type_plan` logic becomes the
  worksheet builder. A1 validates budget feasibility before A2 is called.
- Variant instructions: same concept+rung, different surface (different
  scenario/context, different distractor subset from confusables, different
  gap). Variants may test the same fact — the near-duplicate gate gets a
  variant-aware tier (§4.4).
- A3 (scenario rewrite) applies to R4/R5 items; A4 (feedback) unchanged; both
  stay per-lesson chunked with the existing concurrency and dirty-lesson
  partial retries.
- The item `payload` remains the existing internal `Exercise` model — prompts
  and validators keep working with minimal churn.

### 4.3 Incremental builds

- `build_state.json` (`build-state-v2`, with in-place v1 loading) separates the
  latest attempt from `last_known_good` and records source, workflow-config,
  validation-report, bank, and compiled-artifact hashes.
- Dirty set = changed files → concepts sourced from them → lessons containing
  those concepts (via curriculum.yaml) → only those lessons re-enter A2–A5.
- Unchanged lessons' bank files are byte-untouched (their `source_hash` still
  matches). This extends the existing `dirty_lessons` mechanism from run-scope
  to workspace-scope.
- Hard validation failure never enters the canonical workspace. Validated
  graph/curriculum/bank changes and their state checkpoint promote together
  with rollback on interruption; versioned bundles use a staging directory and
  a final atomic rename. The compiler refuses unresolved/unknown validation,
  source or configuration drift, bank drift, and invalid emitted artifacts.

### 4.4 Quality gates (delta on top of existing)

Existing gates stay (dedup, TF balance dictation, Bloom/type coupling,
tautology, rearrange mechanics, coverage floor/over-drill, fact-check with the
calibrated judge). New:

- **Ladder completeness**: every active concept has its required rungs (per
  `depth`) covered by ≥1 active item. Full-envelope worksheets enforce every
  configured optional variant; budgeted worksheets enforce their exact selected
  rows. Missing a required rung or a selected variant is an error that blocks
  publish.
- **Variant-aware duplicates**: items in the SAME cell may share the fact but
  must differ in surface (Jaccard on prompt text < 0.7); items in DIFFERENT
  cells/concepts keep today's stricter thresholds.
- **Cross-file coverage**: A0 inventory entries from every source file must map
  to ≥1 concept (nothing silently dropped in the merge). Warning with a listing.
- **Confusable reciprocity**: `confusable_with` pairs feed distractors and
  `rejected_answers` (GRADING_SPEC) — gate warns when a decision-depth concept
  has zero confusables (weak distractor pool).
- **Worksheet authority**: dictated rows are validated in exact order before
  fold-in. Concept, rung, question type, Bloom level, and dictated T/F answer
  must match the row which assigns the persisted variant; aggregate balance is
  not a substitute for row identity.
- **Final-sequence gate**: the emitted learner artifact is independently
  re-parsed and checked after selection, ordering, and option presentation.
  The machine-readable `sequence-quality-v1` report names exact unit/item
  paths, distributions, streaks, rolling windows, concept collisions,
  correct-option positions, rung rhythm, prompt stems, and every configured
  relaxation. Unexplained experience violations block publication.

---

## 5. Curriculum Compiler (deterministic, no LLM)

Pure function: `compile(workspace, compile.yaml) → CompiledCourse → bundle`.
Seeded; identical inputs produce an identical learner artifact and quality
report. Bundle version/timestamp provenance intentionally changes per publish.

```yaml
# compile.yaml (defaults shown)
schema_version: compile-v1
levels: 3
recycle: { l2: 0.40, l3: 0.30 }     # share of prior-level concepts recycled via UNSEEN variants
session_size_hint: 12
checkpoints: per_module              # per_module | none
final_review: true
seed: 901
experience:
  max_same_mechanic_streak: 2
  max_same_ui_family_streak: 2
  max_same_true_false_answer_streak: 2
  mechanics_window_size: 6
  min_mechanics_per_window: 3
  avoid_adjacent_same_concept: true
  max_search_states: 100000
  relaxation_order: [mechanics_window, true_false_answer_streak, ui_family_streak, mechanic_streak, concept_adjacency]
sequence_quality:
  max_same_correct_position_streak: 2
  max_downward_rung_jump: 1
  block_on_errors: true
  permute_choice_options: true
gauntlet:
  critic_backend: null             # claude-code | codex when enabled
  critic_model: null               # backend-qualified model label
  qualitative_required_for_publication: false
  max_rounds: 4
  plateau_rounds: 2
  repeated_loss_rounds: 2
  minimum_improvement_margin: 0.02
  confidence_threshold: 0.65
  human_review_threshold: 0.40
```

### 5.1 Levels (bridge: level = unit) — AGREED

Each lesson compiles into `levels` units. Unit `import_key` =
`<lesson-key>-l<N>`; title = `"<Lesson title> · Level N"` (localizable).

- **Level 1 — Foundations**: per concept: one R1 + one R2 candidate. Flashcards
  attach here. Among unseen variants, selection jointly balances learner
  mechanic, T/F answer, and correct-option position before stable variant/seed
  tie-breaks.
- **Level 2 — Apply**: per concept: R3; mechanism/decision concepts add R4.
  Plus recycling: `recycle.l2` share of the lesson's concepts get one
  **unseen** R1/R2 variant. Concept priority for recycling: most
  confusables first, then seeded round-robin (deterministic proxy for
  "hardest" until telemetry exists).
- **Level 3 — Master**: decision concepts: R5 variants; all: unseen R4/R3
  variants; plus `recycle.l3` share recycled via remaining unseen variants.

No item appears twice inside one learner unit. Recycling spends unseen variants
first; any bank whose selected worksheet has too few optional candidates may
reuse an exact item across different levels, which is explicit fallback behavior
rather than silent loss. A depth-classified bank avoids that fallback only when
its selected worksheet retains sufficient optional capacity in both recycled
rung bands. Even a full envelope can lack that capacity for some concept mixes;
a tight owner budget can make the same cross-level repeat mathematically
unavoidable after mandatory rung coverage.

### 5.2 Checkpoints & final review

- **Module checkpoint** unit (`<module-key>-checkpoint`): samples 1–2 items per
  core concept of the module inside a high-rung band, jointly preferring unseen
  content and underrepresented mechanics/answers/positions; falls back to seen
  items when the bank is exhausted (checkpoints legitimately re-ask). This is
  the Duolingo unit-review equivalent and the natural future placement-test
  ("jump ahead") instrument.
- **Course final review**: same sampler over decision/mechanism concepts across
  all modules.

### 5.3 Runtime relationship

In the bridge era, compiled level units ARE the sessions (today's app plays a
unit as a quiz). When Phase 3 lands, the same bank ships in the bundle and the
app composes *practice* sessions dynamically; the compiled path (levels,
checkpoints) remains the progression skeleton. No recompilation needed for the
transition — the bundle carries both.

### 5.4 Shared experience scheduler

Selection and order are separate deterministic stages. `experience.py`
projects bank or runtime items into one immutable metadata view, selects
unseen variants with experience-aware tie-breaks, and schedules the exact
selection with seeded bounded search. Identity, payload hashes, selected
coverage, answer data, and schema are never relaxable. Feasible pools enforce
both the authored mechanic and the rendered UI family (`single_choice` and
`multi_choice` share `multiple_choice`), plus local T/F, rolling-window, and
concept-adjacency constraints. An impossible pool follows the configured
cumulative relaxation order, still minimizes the unavoidable violation, and
records a scheduler attestation bound to the policy, scope, seed, predecessor
profile, and item hashes. The final validator independently reproduces the
infeasibility proof; a free-form reason cannot waive a violation. Bounded-search
exhaustion fails the build instead of authorizing relaxation.

Choice options are deterministically permuted only at presentation emission.
The multiset of complete option objects and every correctness flag is asserted
unchanged, then `correct_answer` is re-derived and the final TechLingo artifact
is validated again. This fixes answer-position patterns without rewriting bank
content.

---

## 6. Bundle & import contract

### 6.1 Layout

```
dist/<course-id>-v<N>/
  manifest.json           # schema_version bundle-v1, course meta, entity list + content hashes,
                          # source/config/bank/artifact provenance, generator, seed, created_at
  course.json             # course + module/unit TREE only (keys, titles, order, level markers)
  units/<unit-key>.json   # per-unit: meta + questions + flashcards — TLQuestion encoding UNCHANGED
  concepts.json           # runtime concept registry: id, label, summary, confusables,
                          # lesson, available rungs, depth  (mastery + practice need this)
  bank/<lesson-key>.json  # full exercise bank (Phase 3 runtime composition; ignored by older importers)
  telemetry.schema.json   # event contract (Phase 4; versioned independently)
```

Question `options` jsonb gains `item_key` alongside the existing `concept_id`
(both stable). **The TLQuestion field encoding from TECHLINGO_OUTPUT_PLAN.md is
unchanged** — correct_answer encodings, parts, word_bank, accepted_orders,
rejected_answers all stay exactly as shipped. Additive only.

### 6.2 Import semantics

- Importer reads `manifest.json`, diffs per-unit `content_hash` against an
  import ledger (`imported_units(unit_import_key, content_hash, imported_at)` —
  small app-side addition), and upserts only changed units. Positional question
  `import_key`s within a unit stay as today (unit content is replaced
  wholesale on change).
- Republish safety: mastery keys on `concept_id`, telemetry on `item_key` —
  both survive any reimport. Retired concepts keep their mastery rows
  (harmless), retired items stop being served.

### 6.3 Compatibility

`compile --flat` emits today's single `course.json` (one unit per lesson, no
levels) for the current importer, until the bundle importer exists. Phase 1
ships both; flat mode retires when the app reads bundles.

### 6.4 Qualitative Gauntlet and references

Deterministic gates remain the publication authority. The optional Gauntlet
runs afterward over the exact ordered `ArtifactSnapshot` shown to learners:

1. an isolated critic receives only the goal, complete 12-dimension rubric,
   relevant source excerpts, explicitly approved references, and the final
   artifact;
2. a separate editor applies the critic's single largest actionable gap within
   structurally enforced item/session/course scope;
3. the challenger must pass the full hard gate; content-changing repairs also
   receive a fresh source-fidelity critique before comparison;
4. a blind comparator strips identifying metadata and judges deterministic
   inverse A/B orders; unstable, low-confidence, or protected-dimension
   regression keeps the champion or requires human review;
5. bounded round/time/token/cost, plateau, repeated-loss, success, no-gap, and
   human decisions stop the loop while retaining the best valid champion.

History stores concise evidence, hashes, usage, repairs, relaxations, decisions,
and stop reasons—never private reasoning. Reference sessions are versioned
drafts until a named human promotes the exact reviewed content hash with a
timezone-aware approval. If no approved reference exists, the critic runs from
sources/rubric and explicitly lowers reference confidence.

`qualitative_required_for_publication: true` makes the bundle boundary require
a coherent publication-eligible record for every exact compiled unit. The
record is bound to course/unit/champion hashes plus the complete Gauntlet
policy and rubric, current source hashes, approved-reference hashes, and actual
model/backend roles. Any context drift invalidates the evidence. Gauntlet
edits are deliberately not written back to canonical banks automatically; an
edited champion must be reviewed/applied through the authored content path,
recompiled, and re-evaluated before it can satisfy that exact coverage gate.

---

## 7. Learning Runtime (app-side, spec-driven)

Shipped from this repo as `MASTERY_SPEC.md` + `SESSION_SPEC.md`, each with a
reference TypeScript implementation and exhaustive test vectors — the exact
pattern that shipped grading v1. Sketches (numbers are tunable constants,
frozen in the specs):

### 7.1 MASTERY_SPEC (SM-2-lite over concepts)

Per `(user, course, concept)`: `strength ∈ [0,1]`, `half_life_days`,
`last_review_at`.

```
effective(now)     = strength * 2^(-days_since_review / half_life_days)
on correct at rung r:
    strength   ← strength + (1 - strength) * gain(r)        # gain: R1 .15, R2 .20, R3 .30, R4 .35, R5 .40
    half_life  ← min(half_life * 1.8, 90d)                  # first success: 1d
on wrong:
    strength   ← strength * 0.5
    half_life  ← max(half_life * 0.5, 0.5d)
due for review     ⇔ effective < 0.55
level N complete   ⇔ every level-N concept has effective ≥ 0.8
                     (evidence at that level's rungs)
```

Production evidence (R3+) moves strength more than recognition — typing "LLM"
proves more than picking it. `grade = typo` (GRADING_SPEC) counts as correct.

### 7.2 SESSION_SPEC (composer)

A practice/lesson session of ~12–15 items:

```
1 warm-up          — easy item (low rung) of the user's STRONGEST concept   (confidence opener)
~60% new           — cells of the current level not yet passed
~25% review        — due concepts (lowest effective strength), unseen variants preferred
~15% mistakes      — from the mistakes queue (an item leaves after 2 correct redemptions)
constraints        — max 2 selected items per concept; shared deterministic
                     experience scheduler; after 2 consecutive wrong answers,
                     de-escalate: inject a lower-rung item of the same concept
target             — ~80% expected success (Duolingo's sweet spot); without
                     telemetry, rung distance from the user's proven rung is the
                     difficulty proxy
```

### 7.3 App-side storage (DDL suggestions shipped with the specs)

```
user_concept_strength(user_id, course_key, concept_id, strength, half_life_days, last_review_at)
mistake_queue(user_id, item_key, concept_id, times_missed, redeemed_count, last_missed_at)
answer_events(id, user_id, course_key, item_key, concept_id, rung, correct, grade, response_ms, session_id, client, ts)
imported_units(unit_import_key, content_hash, imported_at)
```

---

## 8. Telemetry flywheel (Phase 4)

The runtime→factory feedback loop — Duolingo's actual moat, right-sized:

1. App emits `answer_events` (schema in the bundle; versioned).
2. Nightly sync aggregates per `item_key`: `p_correct`, `n`, `typo_rate`,
   median `response_ms` → written into bank items' `telemetry` block.
3. Deterministic flags in the CMS queue:
   - `p_correct > 0.95, n ≥ 30` → too easy (retire or raise rung)
   - `p_correct < 0.40, n ≥ 20` → too hard or ambiguous → regenerate
   - `typo_rate > 0.30` → review `accepted_answers` (missing synonym?)
4. One-click regenerate (existing editor machinery) resolves flags; provenance
   rules still apply.

IDs make this possible from day one (`item_key` in every event) even though the
loop itself lands last.

---

## 9. CMS = Course Workspace UI (evolution of the existing web app)

The Next.js + FastAPI app already has 70% of the CMS: per-question view, typed
edit forms, one-click AI regeneration, validation banners, grading-true
preview. Evolution:

- **Phase 1**: course list over `courses/*` (replaces run list as the home);
  build status + incremental build trigger; coverage heatmap (concept × rung,
  from SQLite); editor rewired to bank files (run folders become read-only
  history); publish button (compile → bundle → optional import call).
- **Phase 2**: compiled-path preview (levels, checkpoints), recycle inspector
  ("which variants does Level 2 reuse?").
- **Authoring**: provenance badges, pin toggles, authored-unit editor
  (markdown pages, video/audio references) writing to `authored/`.
- **Phase 4**: telemetry dashboards + flagged-items queue.

---

## 10. Testing strategy (three layers)

1. **Deterministic unit/property tests** (pytest, no LLM):
   - workspace IO round-trips; graph merge determinism + id stability under
     re-extraction; dirty-set computation.
   - compiler: quota/worksheet builder, level composition (no repeated items,
     recycle ratios honored, unseen-variant guarantee), checkpoint sampling,
     seeded byte-identical bundles.
   - emit/bundle: hashes stable under no-op rebuild; TLQuestion encodings
     (existing tests carry over).
2. **Content eval harness** (`eval/`, gates prompt changes — the ≥9/10 content
   guarantee for ANY input):
   - golden corpus: 3 diverse source sets (tiny single-file; multi-file
     overlapping like ai-901; format-heavy with tables/lists).
   - deterministic metrics: coverage %, ladder completeness, dup rate, TF
     balance, distractor-source rate (share of options drawn from confusables).
   - calibrated LLM judge (A5 fact-check calibration learnings apply):
     grounding, distractor plausibility, scenario realism — scored 1–5 with
     quoted evidence; thresholds fail the eval.
   - `python main.py eval [--backend codex]`; run before merging any
     prompt/quota change. Results archived per commit for trend lines.
3. **Spec test vectors** (the app boundary): mastery update table + session
   composition constraints as data-driven vectors; the Python reference and the
   app's TS implementation must pass the identical set (grading precedent).

---

## 11. Migration plan (code-level)

| Current | Becomes | Notes |
|---|---|---|
| `outputs/run-*` run-centric flow | `courses/<id>/` workspace | runs remain as read-only history |
| `executors.py` A0 + A1 | `factory/extract.py` + `factory/plan.py` | + graph merge, id-stable matching; `_validate_a1_map` carries over per-file |
| A2–A4 per-lesson chunking | `factory/generate.py` | cell worksheets replace lesson quotas; prompts keep the internal Exercise payload |
| `validate.py` gates + normalize | `factory/gates.py` | + ladder completeness, variant-aware dedup tiers |
| `emit.py` single course.json | `compiler/` package: `levels.py`, `checkpoints.py`, `bundle.py` | reuses type mapping, encodings, slug/import_key logic |
| `cli.py run` | `main.py course init/build/compile/publish/status/eval` | `run` kept as legacy alias during Phase 1 |
| `server/main.py` + `web/` viewer | workspace-backed CMS (§9) | editing endpoints re-pointed at bank files; re-emit → recompile |
| — | `workspace.py`, `index.py` (new) | file IO + SQLite index |

Ground rules during migration: `models.py` internal Exercise payload unchanged;
GRADING_SPEC untouched; every phase keeps `main.py run` (legacy) green until its
replacement ships.

---

## 12. Roadmap & acceptance criteria

| Phase | Scope | Acceptance criteria |
|---|---|---|
| **1. Course Workspace** (~3–5 days agent work) | workspace format, multi-doc build (A0×N + merge + planner), incremental builds, bundle + `--flat` export, CMS course list + coverage view, eval harness v1 | `main.py course build courses/ai-901` from the 6 real files → valid workspace + bundle + flat course.json importable today; editing 1 source file rebuilds only affected lessons; eval green on golden corpus; graph merge produces stable ids across two consecutive builds |
| **2. Ladders & Levels** (~2–3 days) — **2a shipped 2026-07-16**: level compiler (levels=unit + recycling + checkpoints + final review, deterministic/seeded, `compile.yaml` defaults on). **2b generation shipped 2026-07-17**: concept `depth` (A1-classified), cell worksheets expand the §3.3 quotas into dictated per-lesson plans (`worksheet.py`; distributions become derived), variant-aware gates (ladder completeness, same-cell surface dedup tier, confusable reciprocity), worksheet-assigned rung persisted on bank items, recycler spends unseen-capable concepts first. **Still pending**: CMS path preview | cell quotas + variant generation, compiler levels (level=unit) + recycling + checkpoints, CMS path preview | each lesson emits L1/L2/L3 units playable in the CURRENT app ✅; recycle ratios honored ✅; zero exact-item repeats across a lesson's levels when the selected worksheet retains sufficient optional capacity ✅; tight owner budgets use explicit seen-item fallback across levels while preserving within-unit uniqueness ✅; module checkpoints present ✅; same seed → byte-identical bundle ✅ (tested); ai-901 course with levels imported end-to-end ⬜ (awaiting full 2b bank build) |
| **3. Learning Engine** (spec ~2 days here + app implementation) | MASTERY_SPEC + SESSION_SPEC + reference impls + vectors; app: mastery tables, composer, practice & mistakes replay, bundle importer with ledger | app passes 100% of both vector sets; practice session served from bank; mastery survives a v2 republish of ai-901; mistakes replay works on both clients |
| **4. Flywheel** (~1 week, spread) | answer_events ingestion, nightly aggregation → bank telemetry, CMS flag queue, threshold-driven regenerate | events flow e2e; ≥1 item auto-flagged and regenerated through the queue; difficulty ordering inside levels re-ranked by real p_correct |

Dependencies: 2 needs 1; 3 needs 2's bundle (bank shipping); 4 needs 3's events.

---

## 13. Decisions log

| # | Decision | Rationale |
|---|---|---|
| D1 | Concept is the atom; questions are variants of concept×rung cells | unlocks levels/recycling/repetition/practice as queries, not features |
| D2 | Three planes: LLM factory / deterministic compiler / spec-driven runtime | isolates cost, testability, and ownership boundaries |
| D3 | Files+git canonical, SQLite derived index; no new server DB, no graph/vector DB | agent-native workflow, versioning for free, zero ops at this scale |
| D4 | Bundle of small files + manifest hashes; not one monolithic JSON | incremental import, diffability, authored content slots |
| D5 | **Level = separate unit (Phase 2 bridge); first-class level model in Phase 3** | works with today's app immediately — *agreed 2026-07-16* |
| D6 | **App implements runtime per our specs (grading-spec pattern)** | proven mechanism of control across repos — *agreed 2026-07-16* |
| D7 | Linear path per course (light prerequisites only, no skill tree) | Duolingo's own 2022 lesson: choice paralysis hurts learning |
| D8 | SM-2-lite before any ML personalization | 90% of the effect, 5% of the complexity, no data required |
| D9 | Deterministic everything after the factory; seeded compiler | byte-identical rebuilds, property-testable, free re-runs |
| D10 | `concept_id`/`item_key` immutable post-publish; telemetry & mastery never key on positional import_keys | learner progress survives every republish |
| D11 | OpenAI API backend removed (2026-07-16); subscription CLIs only, `claude-code` default | owner decision — $0 marginal cost is a hard constraint (G6); API quota was permanently unfunded |

---

## 14. Risks & open questions

- **Concept id stability under aggressive source rewrites** (heading renames,
  file splits): matching falls back to similarity; worst case a concept retires
  and re-mints, orphaning its mastery rows (learners re-prove it). Acceptable;
  monitor in Phase 1 acceptance.
- **Bank generation cost/time** (~3–4× items): mitigated by $0 backends,
  concurrency, incremental builds; if A4 (slowest stage) hurts, feedback
  generation can be lazy (R1/R2 first, R4/R5 feedback on a second pass).
- **App-side delivery risk** (Phase 3 is ~60–70% of the remaining experience
  work and lives in another repo): mitigated by D6 + test vectors; sequence app
  work early in Phase 3, not after.
- **Variant quality** (do v2s stay fresh or converge?): variant-aware dedup gate
  + eval harness metric (surface distance within cells) watch this.
- **Open**: localization strategy (BCS/EN course copies vs. localized fields) —
  defer; schema keeps `locale` on course.yaml so it's additive later.
- **Open**: cross-course concept reuse (AI-900 ↔ AI-901 overlap → "you already
  know this" skip): global concept namespace per certification family is v2+;
  per-course namespaces for now.

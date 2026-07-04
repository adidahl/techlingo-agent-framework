# TechLingo Workflow — Resilience Plan & Checklist

> Living document. Goal: make the A1–A5 course-generation pipeline **never crash**
> on LLM output variance again, and make config/environment problems fail fast
> with clear messages. Check items off as they are applied and verified.

Last updated: 2026-07-03

---

## 1. Why the failures happened (root causes)

We hit five distinct crashes during the first successful run. They collapse into
**four root causes**:

| # | Root cause | Concrete symptoms we saw |
|---|------------|--------------------------|
| RC1 | **Non-deterministic LLM output, code assumed perfection.** Executors did `json.loads(str(result))` + `Model.model_validate(data)` exactly once, no safety net. | ` json.loads` crash on ```` ```json ```` fences; `union_tag_invalid` on invented enum `scenario_based`; A4's non-deterministic "45 validation errors" (missing/extra fields); A5 repair crash (same pattern). |
| RC2 | **Ambiguous prompt wording.** Prompt said exercises *"MUST be scenario-based"* → weaker models read this as a **question_type** value. | `gpt-4o-mini` systematically emitted `question_type: "scenario_based"`. |
| RC3 | **Config had no cross-field self-check / clear errors.** | `workflow_config.json` had `blooms=8 ≠ exercises=2`, and `6 modules` for `5 lessons` (impossible). Raw pydantic traceback, no guidance. |
| RC4 | **Deterministic rules delegated to the LLM.** Mechanical constraints were left to chance instead of enforced in code. | `rearrange.correct_order` tokens didn't match `word_bank`; total lessons 4 instead of 5–6. |

Bonus latent bug: the A2 prompt's JSON example used Python literals
`None`/`False`/`True` instead of JSON `null`/`false`/`true`
([prompts.py](src/techlingo_workflow/prompts.py) ~L220) — actively nudges the
model toward invalid JSON.

---

## 2. Layered defense (strategy)

Ordered strongest → supporting. Layers 1+2 together mean the worst case is a
course returned **with warnings**, never a crash.

| Layer | Fix | Eliminates |
|-------|-----|-----------|
| 1 | **Structured Outputs** — pass `response_format=Course` so OpenAI constrains generation to the schema. Invalid JSON / invented enums / missing fields become impossible at the API level. | RC1 (most), RC2 |
| 2 | **Robust parsing + schema-repair retry** — strip fences, extract JSON, feed validation errors back for correction. Safety net for anything past layer 1. | RC1 residual |
| 3 | **Prompt fixes** — reword "scenario-based", enforce the 5 allowed `question_type` values explicitly, fix JSON-literal example. | RC2 at the source |
| 4 | **Deterministic post-processing** — normalize mechanical constraints in code (rearrange token multiset). | RC4 (the fixable parts) |
| 5 | **Config self-validation** — cross-field checks + friendly, actionable CLI error before any LLM call. | RC3 |
| 6 | **Environment lock** — `.python-version` (3.13) + README note (3.14 not yet supported by `agent-framework` beta). | install-time crashes |

---

## 3. Checklist (apply in order)

### Layer 1 — Structured Outputs
- [x] `run_json_model` passes `response_format=<model>` to `ChatAgent.run`.
- [x] Graceful fallback: if the framework/API rejects the schema, drop to prompt-only mode (layer 2) instead of crashing.
- [x] A2/A3/A4/A5 all route Course generation through `run_json_model`.
- [x] **Structured Outputs now ACTIVE for `Course`** (discriminator→anyOf fix, §9); verified `ok:true` with it engaged.
- [x] Acceptance: a full run shows no `union_tag_invalid` / JSON parse crashes even with `gpt-4o-mini`.

### Layer 2 — Robust parsing + repair retry (baseline already added)
- [x] `_extract_json` strips markdown fences and surrounding prose.
- [x] `run_json` retries on `JSONDecodeError` with a repair prompt.
- [x] `run_json_model` retries on **both** `JSONDecodeError` and `ValidationError`.
- [x] A2/A3/A4 use `run_json_model`; A5 repair loop uses `run_json_model`.
- [ ] Confirm no remaining direct `run_json` + `model_validate` pairs that can crash (audit `executors.py`, `validate.py`).

### Layer 3 — Prompt fixes
- [ ] Reword A2/A3 "must be scenario-based" → "the prompt **text** must describe a real-world scenario with a decision point; `question_type` stays one of the five allowed values."
- [ ] Add explicit line: `question_type` MUST be exactly one of `single_choice|multi_choice|true_false|fill_gaps|rearrange`; never invent others.
- [ ] Fix JSON example literals `None/False/True` → `null/false/true` in [prompts.py](src/techlingo_workflow/prompts.py).

### Layer 4 — Deterministic post-processing
- [ ] Add `normalize_course(course)` that rebuilds `rearrange.word_bank` as a (seeded) shuffle of `correct_order` when the multiset mismatches — guarantees the constraint.
- [ ] Call it in the A5 stage before validation and after each repair.
- [ ] Note: total-lesson-count can't be fabricated deterministically → stays enforced via validation + self-correct retry (documented, not silently ignored).

### Layer 5 — Config self-validation
- [ ] Add cross-field check: `min_lessons_total >= modules_count` (each module needs ≥1 lesson).
- [ ] Make distribution error messages actionable (state both sums and the fix).
- [ ] Catch config `ValidationError` in the CLI and surface a clean `typer.BadParameter` instead of a raw traceback.

### Layer 6 — Environment lock
- [ ] Add `.python-version` = `3.13`.
- [ ] README note: use Python 3.13 (3.14 not yet supported by `agent-framework` beta).

### Verification (run 2026-07-03, `gpt-4o-mini`, run-20260703-164908)
- [x] Full pipeline run completes and writes `course.json` / `course.md` / `validation_report.json`. → **"Run complete"**, no crashes.
- [x] Re-run with `gpt-4o-mini` also completes (proves resilience, not just a stronger model). → previously crashed *systematically* on `scenario_based`; now runs clean through all A1–A5 + 3 self-correct attempts.
- [x] Zero `scenario_based` / `union_tag_invalid` crashes; zero JSON-parse crashes.
- [x] Zero `rearrange` token-multiset mismatches in final output (Layer 4 verified).
- [ ] `validation_report.json` → `ok: true`. → **NOT met with `gpt-4o-mini`**: remaining errors are content *quantity* (2 modules vs 3; 3 lessons vs 5–6) — the weak model under-generates. This is by design outside layers 1–4 (counts can't be enforced by schema); use `gpt-4o` for quality, or accept the validation+retry path. **No crash either way.**

### Outcome
Resilience goal **achieved**: the pipeline no longer crashes on LLM output variance —
worst case is a course returned with content warnings/errors, never an exception.
Reaching `ok: true` is a separate *quality* dimension driven by model capability +
the count-enforcement (validation+retry) path.

---

## 4. Files touched
- `src/techlingo_workflow/llm.py` — parsing, repair, structured outputs.
- `src/techlingo_workflow/executors.py` — route stages through `run_json_model`.
- `src/techlingo_workflow/validate.py` — repair loop + `normalize_course`.
- `src/techlingo_workflow/prompts.py` — wording + JSON-literal fix.
- `src/techlingo_workflow/config.py` — cross-field validation.
- `src/techlingo_workflow/cli.py` — friendly config error.
- `workflow_config.json` — consistent values (done).
- `.python-version`, `README.md` — environment.

## 5. Notes / decisions
- Model: with layers 1–3, `gpt-4o-mini` should work reliably. `gpt-4o` remains the safer default for quality.
- Structured Outputs cannot enforce array-length / cross-field counts (e.g. total lessons) → those stay in the validation+retry path by design.
- **Structured Outputs is REJECTED for the `Course` schema** (verified 2026-07-03):
  OpenAI returns `400 ... 'oneOf' is not permitted` because the pydantic
  discriminated unions (`fill_gaps.parts`, `Exercise`) emit `oneOf` (OpenAI strict
  mode requires `anyOf`). It *works* for simple models (e.g. `Flashcard`). So for
  the course pipeline, Layer 1 gracefully falls back and the real protection is
  Layer 2 (repair-retry) + graceful degradation + Layer 3 prompt fixes. Making it
  work for `Course` would require emitting OpenAI-compatible schemas
  (oneOf→anyOf, all-required, additionalProperties:false) — tracked as future work.

## 6. Additional hardening applied (2026-07-03, chasing ok:true)
- **A1 lesson distribution**: A1 prompt now specifies EXACT lessons per module
  (not a range). The self-correct loop re-runs A2, never A1, so A1 counts must be
  right first try. This fixed the persistent "wrong module/lesson count" errors.
- **Graceful repair**: `repair_course_if_needed` catches repair-call exhaustion
  and returns the last valid course + a warning, instead of crashing A5. (This
  closed a hole in Layer 2: `run_json_model` still *raised* on final exhaustion.)
- **Fact-checker calibration**: `a5_source_check_prompt` now flags only confirmed,
  quotable defects. Removed two design conflicts that made `ok:true` unreachable:
  (a) it penalized `rearrange` answers for "restating the prompt" (that is the
  task's nature); (b) it rejected scenario-framed `true_false` even though A3 is
  told to make Applying/Analyzing exercises scenario-based.
- **`error_type` normalization**: `normalize_course` now defaults the required
  `error_type` label on incorrect options when A3/A4 drop it during rewrites
  (mechanical presence requirement, not content-sensitive).

## 7. ✅ RESULT: `ok: true` achieved (run-20260703-175901, `gpt-4o`)
- `validation_report.json` → **ok: true, 0 errors** (12 non-blocking warnings).
- 3 modules / 6 lessons / 30 questions; 6 of each question type; 0 rearrange
  mismatches; 0 missing `error_type`. Passed on the **first A5 pass** (no loop-backs).
- Both goals met: (1) pipeline never crashes on LLM variance; (2) produces a
  fully valid course.

---

## 8. Decisions & knowledge for the future

Durable decisions (the "why"), so we don't relitigate them:

- **The self-correct loop re-runs A2, never A1.** Therefore module/lesson **counts
  are fixed by A1 and cannot be repaired downstream** — A1 must be exact on the
  first try. Any count constraint must be enforced in the A1 prompt (explicit
  per-module counts), not left to the loop. *(This was the single biggest
  blocker to `ok:true`.)*
- **Never let an LLM call crash the run.** `run_json_model` retries, but on final
  exhaustion it still *raises*. Every caller with a safe fallback (A5 repair) must
  catch that and degrade gracefully. Callers without a fallback (A2 first-gen) may
  still raise — acceptable, but revisit if it ever happens in practice.
- **Mechanical constraints belong in code, not the LLM.** `normalize_course`
  deterministically enforces them (rearrange token multiset; `error_type`
  presence). Add future mechanical rules there rather than hoping the model obeys.
- **Course title = source filename** (2026-07-03): the document's filename is the
  author's title. `cli.py` auto-derives `title` from `input_file.stem` when
  `--title` isn't given, and force-sets `result.course.title` on the final output
  so it matches exactly (the LLM's title is only used as context). Verified:
  run-20260703-191539 → title "Introduction to generative AI and agents".
- **The fact-checker must only flag confirmed defects.** An LLM judge that treats
  subjective opinions as blocking errors makes `ok:true` unreachable and can
  oscillate (full regeneration finds *new* opinions each pass). Keep it high-
  confidence and quote-based; keep it free of conflicts with what other stages are
  told to produce (e.g. scenario `true_false`).
- **Model choice** (updated 2026-07-03): `.env` uses `gpt-5.4-mini-2026-03-17`
  (newest mini). ✅ **VERIFIED reaches `ok:true`** on a real run
  (run-20260703-190844, new doc "Introduction to generative AI and agents":
  ok:true, 0 errors, 3 modules / 6 lessons / 30 questions, first pass). Unlike the
  old `gpt-4o-mini` (which under-generated), the new mini produces a fully valid
  course with Structured Outputs + all resilience layers — much cheaper than
  `gpt-4o`. Keep `gpt-5.4-mini` as the default; `gpt-4o` no longer required.
  (Reminder: a newer model can't fix the Structured-Outputs schema rejection — that
  was a model-independent schema issue, now fixed in §9.)

## 9. Structured Outputs for `Course` — ✅ FIXED & VERIFIED (2026-07-03)

**Status: DONE.** Removed the `discriminator=` from `FillGapsPart` and `Exercise`
in `models.py` (→ plain unions → `anyOf`). Verified: (a) pydantic still validates a
known-good course and discriminates all 5 exercise subtypes correctly; (b) OpenAI
now **accepts** `response_format=Course`; (c) a full pipeline run with Structured
Outputs active reached **`ok: true`, 0 errors** (run-20260703-190425). No further
strict-mode issues (datetime/optionals) surfaced for the `run` pipeline. Layer 1 is
now a real hard guarantee for course generation, not just a fallback.

> Note: the `analyze` command's `TextAnalysisResult` embeds `WorkflowConfig` with
> open `Dict[str,int]` fields, which OpenAI strict mode may still reject — that path
> falls back to Layer 2 gracefully (no crash). Fix later if `analyze` needs the
> guarantee.

**Original problem (for reference):** `response_format=Course` was rejected by OpenAI:
`400 ... 'oneOf' is not permitted` at `properties.parts.items`.

**Root cause (tested 2026-07-03):** pydantic **discriminated** unions
(`Field(discriminator=...)`) emit `oneOf`; OpenAI Structured Outputs forbids
`oneOf`. A **plain** union emits `anyOf`, which OpenAI **accepts**. Verified with a
minimal probe:
- `Annotated[Union[A,B], Field(discriminator="type")]` → `oneOf` → **REJECTED**.
- `Union[A,B]` (no discriminator) → `anyOf` → **ACCEPTED**.

**The fix (do this to enable Layer 1 for the whole pipeline):**
1. In `models.py`, drop the `discriminator=` from the two unions:
   - `FillGapsPart = Annotated[Union[FillGapsTextPart, FillGapsGapPart], Field(discriminator="type")]`
     → `FillGapsPart = Union[FillGapsTextPart, FillGapsGapPart]`
   - `Exercise = Annotated[Union[...5 types...], Field(discriminator="question_type")]`
     → `Exercise = Union[...5 types...]`
   Pydantic still validates correctly because each member has a distinct `Literal`
   on `type` / `question_type`; only the emitted schema changes (`oneOf`→`anyOf`).
   (Trade-off: slightly less precise pydantic *error messages* on mismatch — the
   repair-retry loop absorbs that.)
2. Re-run the probe against the **full** `Course` model. OpenAI strict mode has
   more requirements that may surface next (all properties `required`,
   `additionalProperties:false`, no unsupported `format`s — watch the
   `generated_at: datetime` default and any `Optional` defaults). Address each as
   it appears; the framework converts the pydantic model, so if it doesn't emit a
   strict-compliant schema we fall back to option 3.
3. **Fallback if the framework's converter can't produce a strict schema:** bypass
   `ChatAgent` for structured calls — build the JSON schema ourselves
   (`Course.model_json_schema()` → transform: `oneOf`→`anyOf`, inline `$defs`,
   set `additionalProperties:false`, mark all properties required, strip defaults)
   and pass it via the raw OpenAI `response_format={"type":"json_schema",
   "json_schema":{"schema":..., "strict":true}}`.
4. Keep Layer 2 (repair-retry + graceful degradation) regardless — Structured
   Outputs is a hard guarantee on top, not a replacement.

**Priority:** medium. The pipeline already never crashes and reaches `ok:true`
via Layers 2–4. Enabling Structured Outputs would remove an entire class of
retries (cheaper, faster, more deterministic) — worth doing, not urgent.

## 10. Question-quality architecture overhaul (2026-07-04)

**Root cause found:** A2 (question generator) never received the source text —
only A1's course map (titles + one-sentence SLOs). With one fact per lesson, the
5 question types became 5 re-skins of the same sentence ("What is this course
mainly about?", all-true true/false, rearrange that reconstructs the SLO, absurd
distractors like "Cook lunch"). No prompt tuning could fix a data-flow problem.

**Changes:**
- **A0 Content Inventory** now runs first in the main workflow (same analyzer
  LLM pass the standalone `analyze` command uses); its parts feed A1 as a
  coverage checklist. Non-fatal on failure.
- **A1 emits per-lesson content packs**: `concepts[]` atoms (id/label/summary/
  confusable_with), skips meta/navigation source content (welcome paragraphs).
- **A2/A3/A4 receive the full source text.** A2 tags every exercise with
  `concept_id` and must spread exercises evenly across concepts. Distractor
  policy: confusable siblings only, no absurd options, no tautologies, balanced
  true/false via minimal-corruption false statements, fill-gap answers must be
  key terms, rearrange 4–8 short tokens.
- **Bloom/type coupling** (enforced in config validator + course validator):
  Applying/Analyzing ⇒ scenario single/multi choice; true_false/fill_gaps/
  rearrange ⇒ Remembering/Understanding. Default question_type_distribution
  changed to 4/4/3/2/2 to stay feasible.
- **Deterministic gates in validate.py** (all tested in
  tests/test_quality_gates.py): near-duplicate detection via Jaccard on content
  signatures (error within lesson ≥0.6, warning across lessons ≥0.8), all-same
  true/false answers (error at ≥3), tautology + correct-longest-option bias
  (warnings), rearrange token count/length.
- **normalize_course**: word_bank is ALWAYS a seeded shuffle of correct_order
  (previously only when multisets differed — courses shipped with the answer
  pre-arranged!), generic prompt stems rotate through template banks, exercises
  sorted easy→hard (Bloom rank, then recognition→production mechanic).
- **Concept metadata plumbing**: `_seed_concepts_from_map` (A1→A2 safety net)
  and `_restore_concept_metadata` (A3/A4 index-aligned restoration) keep
  concepts/concept_id alive without trusting LLM echo-back; emit.py carries
  `concept_id` into every TLQuestion.options for future app-side adaptivity.

**Gotchas for the future:**
- The A5→A2 loop now sends content-quality errors (duplicates, balance) back to
  A2 — these are fixable by regeneration, unlike the old purely-structural ones.
- Validation runs the duplicate check pairwise over all exercises (fine up to a
  few hundred; revisit if course sizes explode).
- `difficulty` now explicitly means language simplicity, NOT content triviality
  (difficulty_contract rewritten).

### §10.1 Retry-degradation fixes (2026-07-04, after first E2E)

First E2E run exposed a failure mode: **the A5→A2 retry made things worse** (the
regenerated attempt collapsed 3 modules into 1, dropped true_false from the type
mix, and the A5 LLM repair inflated a lesson to 27 exercises). Fixes, all verified:

- **Best-attempt-wins**: `PipelineState.best_course/best_report` track the
  best-scoring attempt (fewest errors) across the loop; `a5_validator` yields the
  best attempt, never blindly the last. Same principle inside
  `repair_course_if_needed`: a repair candidate replaces the current version only
  if it has FEWER errors.
- **Retry anchoring**: the A2 validation-feedback section now explicitly says
  "fix ONLY the listed errors; keep the exact module/lesson structure and type mix".
- **Deterministic Bloom/type plan**: `_bloom_type_plan(config)` solves the
  coupling constraint (Applying/Analyzing→choice; tf/fg/ra→R/U) in Python and
  hands A2 the finished per-lesson assignment — the LLM repeatedly failed to
  solve it on its own (kept putting Analyzing on rearrange).
- **True/false alternation**: "roughly half false" was ignored; replaced with a
  mechanical rule (1st tf across the course = false, 2nd = true, alternating).
- **Fact-checker false positives**: source-check prompt now excludes domain
  vocabulary ("document analysis") from meta-reference flagging, excludes concept
  summaries from paraphrase flagging, and forbids reporting issues whose own
  description concludes there is no defect.

### §10.2 Second E2E round findings (2026-07-04)

Run 3 (with §10.1 fixes) exposed three more systematic failure modes, each now
fixed deterministically:

1. **A1 itself violates the module plan** (produced 4 modules/7 lessons despite
   "EXACTLY 3"). Since the loop re-runs A2 (never A1), a bad map poisons every
   attempt. Fix: `_validate_a1_map()` in executors.py — deterministic structural
   check (module count, lesson total, ≥2 concepts/lesson, unique concept ids)
   with up to 3 A1 retries carrying concrete feedback, while it's still one
   cheap LLM call.
2. **Raw error-count best-attempt picked the wrong winner**: attempt 2 (1 module,
   5 errors) beat attempt 1 (correct-shaped, 9 content nitpicks). Fix:
   `attempt_badness()` — lexicographic (structural, shape, content) ranking used
   both in `a5_validator` and inside `repair_course_if_needed`.
3. **All-true true/false survives every prompt instruction** (even mechanical
   alternation). Fix: `rebalance_true_false()` targeted micro-repair — a narrow
   LLM call rewrites every other statement into a minimally-corrupted FALSE one
   (with fresh feedback), instead of regenerating the whole course.

Also relaxed: per-lesson concept "evenness" (max-min ≤ 1) was unachievable with
more concepts than exercises; replaced with a coverage floor (use min(C,E)
distinct concepts) + over-drill cap (max(ceil(E/C), 2) per concept).

### §10.3 Final polish (2026-07-04)

- `rebalance_true_false` now fires proactively whenever the minority answer class
  is under 1/3 (not only on all-same), topping the split up to ~half/half before
  validation; direction-aware prompt handles the (rare) all-false case.
- A1 prompt: concepts must be DISJOINT facts — merge same-mechanism aliases
  ("speech recognition" / "speech-to-text") into one atom. Overlapping atoms were
  the root cause of recurring per-lesson coverage errors: A2 kept "using two
  concepts" that were really the same fact.
- `rebalance_true_false` flip-guard: the answer is flipped ONLY when the
  rewritten statement actually differs (Jaccard < 0.9) — an unchanged rewrite
  marked false shipped a true statement with a false answer key (caught live by
  the A5 fact-checker in run-20260704-095222).
- `_validate_a1_map` meta-guard: rejects concept atoms about the course/module
  itself ("training module overview") so "What is this course about?" questions
  can't come back through a sloppy A1 map.

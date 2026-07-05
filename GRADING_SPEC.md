# TechLingo Answer Grading Spec — v1 (2026-07-05)

How learner answers are graded at runtime, for every client (mobile app, web
app, Run Viewer preview). The generator guarantees the DATA (accepted answers,
unique rearrange order); clients apply THIS policy. Reference implementation:
`web/src/components/viewer/grading.ts` (the Run Viewer previews with it, so
authors see production behavior).

Design principle: **data carries answer semantics, graders apply one shared
policy.** Never enumerate typos in `accepted_answers` — typo tolerance is the
grader's job. `accepted_answers` is for genuinely different valid answers
(synonyms, spelling variants like summarize/summarise).

## 1. Normalization

Applied to the learner's input AND every accepted/rejected answer before any
comparison:

1. Unicode NFKC normalization (æ/ø/å, ligatures, full-width forms).
2. Lowercase.
3. Punctuation `. , ! ? ; : ' " ( ) ‘ ’ “ ”` → space.
4. Trim; collapse whitespace runs to a single space.

## 2. Distance metric

Optimal String Alignment (restricted Damerau-Levenshtein): insertion, deletion,
substitution, and **adjacent transposition** ("teh" → "the") each cost 1.

## 3. Typo tolerance by answer length

Tolerance is based on the NORMALIZED ACCEPTED ANSWER's length (not the input's):

| normalized length | max edit distance |
|---|---|
| 1–4 | 0 (exact only) |
| 5–9 | 1 |
| ≥ 10 | 2 |

Short answers get NO tolerance on purpose: domain terms like **LLM/SLM** differ
by one character with opposite meaning.

## 4. Grading a fill_gaps answer

```
grade(input, accepted_answers, rejected_answers):
  input ← normalize(input);  empty → WRONG
  if input ∈ normalize(accepted_answers)                    → CORRECT (exact)
  if input within tolerance of any normalize(rejected)      → WRONG   (confusable guard)
  if input within tolerance of any normalize(accepted)      → CORRECT (typo)
  else                                                      → WRONG
```

UX recommendation on `typo`: accept, but show the canonical spelling
("You have a small typo — accepted: parameters"), like Duolingo.

`rejected_answers` (per gap) are known confusables the fuzzy match must never
absorb (gap "LLM" rejects "SLM"). **Schema note (shipped 2026-07-05):**
`course.json` fill_blank questions carry `options.rejected_answers` (convenience
copy) and `options.parts[gap].rejected_answers`; the generator auto-fills them
from concept-pack confusables. Clients MUST treat a missing field as `[]`.

## 5. Rearrange

Grade by exact sequence equality against `correct_order` (tokens compared as
raw strings, no normalization — the learner arranges given tokens, not typing).

The generator guarantees uniqueness by default: a deterministic gate rejects
sentences built from interchangeable comma-list items ("power chatbots, create
content, translate text"), so exactly one order is correct by construction.

**Schema note (shipped 2026-07-05):** legitimately order-flexible questions
carry `options.accepted_orders: string[][]` (max 24, canonical order first,
expanded from authored `options.interchangeable_groups`). Clients MUST grade by
membership when `accepted_orders` is present and fall back to `correct_order`
equality when absent.

## 6. Other types

- single_choice / multi_choice: selected option set must equal the correct set.
- true_false: boolean equality.

## Versioning

Breaking changes to normalization, distance, or tolerances bump the spec major
version; the generator records course schema versions independently. Keep
`grading.ts` and this file in the same commit when either changes.

# Task: Implement the v1 Answer-Grading Spec in the TechLingo App

**Audience:** the agent working in the TechLingo application codebase (mobile +
web). This document is self-contained — everything you need is here, including
a reference implementation and test vectors. No access to the course-generator
repo is required.

## Why

Two learner-facing grading problems were reported:

1. **`arrange_sentence` questions with several valid orders.** Example word
   bank: `AI can | analyze images, | process speech, | and generate content`.
   Both "analyze images, process speech" and "process speech, analyze images"
   produce a correct sentence, but the app accepts only the stored order and
   marks the other WRONG.
2. **`fill_blank` rejects small typos.** A learner typing "paramaters" for
   "parameters" is marked wrong. Expected: Duolingo-style tolerance — accept,
   but show the canonical spelling.

The course **generator now ships the data** to fix both (see “Data contract”
below). Your job is the **runtime grading policy** in the app, identical on
mobile and web.

## Data contract (already live in imported `course.json`)

Questions arrive with a jsonb `options` column. New/relevant fields:

### `fill_blank`

```jsonc
{
  "question_type": "fill_blank",
  "question_text": "Compared to large language models, ___ language models are cheaper.",
  "correct_answer": "[\"small\",\"Small\"]",   // string; JSON array when several accepted
  "options": {
    "parts": [
      { "type": "text", "text": "Compared to large language models, " },
      { "type": "gap", "accepted_answers": ["small", "Small"],
        "rejected_answers": ["large"],          // NEW — may be absent on old courses
        "placeholder": "___" },
      { "type": "text", "text": " language models are cheaper." }
    ],
    "rejected_answers": ["large"]               // NEW — convenience copy of the gap's list
  }
}
```

- `correct_answer` encoding (existing): plain string for one accepted answer,
  or a JSON-encoded array string for several. Prefer reading
  `options.parts[gap].accepted_answers`; fall back to parsing `correct_answer`.
- `rejected_answers` (NEW): **confusable domain terms that must never be
  accepted, not even as a near-miss typo.** Example: gap "LLM" rejects "SLM" —
  one character apart, opposite meaning. Treat a missing field as `[]`.

### `arrange_sentence`

```jsonc
{
  "question_type": "arrange_sentence",
  "correct_answer": "Generative AI is commonly used to power chatbots, create content, translate text, and summarize documents",
  "options": {
    "word_bank": ["create content,", "Generative AI is", "..."],
    "correct_order": ["Generative AI is", "commonly used to", "power chatbots,",
                      "create content,", "translate text,", "and summarize documents"],
    "interchangeable_groups": [[2, 3, 4]],      // NEW — 0-based positions, may be absent
    "accepted_orders": [                          // NEW — max 24, canonical order FIRST
      ["Generative AI is", "commonly used to", "power chatbots,", "create content,", "translate text,", "and summarize documents"],
      ["Generative AI is", "commonly used to", "power chatbots,", "translate text,", "create content,", "and summarize documents"]
      // ... all permutations of positions 2,3,4
    ]
  }
}
```

- `accepted_orders` (NEW): when present, the learner's arrangement is correct
  iff it **equals any listed order** (array-of-tokens equality, raw string
  compare, no normalization — the learner arranges given tokens).
- When absent (all existing content): grade by equality with `correct_order`
  exactly as today.
- `interchangeable_groups` is provenance; you don't need it for grading
  (membership in `accepted_orders` is sufficient), but it's available.

## Grading algorithm (normative)

### 1. Normalization (fill_blank only)

Apply to the learner's input AND to every accepted/rejected answer:

1. Unicode **NFKC** normalization (critical for æ/ø/å, ligatures, full-width chars).
2. Lowercase.
3. Replace the punctuation characters `. , ! ? ; : ' " ( ) ‘ ’ “ ”` with a space.
4. Trim; collapse consecutive whitespace to a single space.

### 2. Edit distance

**Optimal String Alignment** (restricted Damerau-Levenshtein): insertion,
deletion, substitution, and **adjacent transposition** each cost 1.
(Transposition matters: "paramaters" → "parameters" must count as distance 1.)

### 3. Typo tolerance — scaled by the ACCEPTED answer's normalized length

| normalized length of accepted answer | max edit distance |
|---|---|
| 1–4 | 0 (exact only) |
| 5–9 | 1 |
| ≥ 10 | 2 |

Short answers deliberately get zero tolerance: "LLM"/"SLM", "GPU"/"CPU" differ
by one character with different meanings.

### 4. fill_blank decision procedure (exact order matters)

```
grade(input, accepted, rejected):        # all lists may be empty; rejected may be missing
  input ← normalize(input)
  if input is empty                                   → WRONG
  if input ∈ { normalize(a) for a in accepted }       → CORRECT (exact)
  for r in rejected:
      rn ← normalize(r)
      if distance(input, rn) ≤ tolerance(len(rn))     → WRONG      # confusable guard FIRST
  for a in accepted:
      an ← normalize(a)
      if distance(input, an) ≤ tolerance(len(an))     → CORRECT (typo)
  → WRONG
```

**UX on `typo`:** count as correct, but show the canonical answer, e.g.
*“You have a small typo — accepted: parameters”* (first accepted answer).

### 5. arrange_sentence decision procedure

```
if options.accepted_orders exists and is non-empty:
    correct ⇔ learner_order equals ANY order in accepted_orders   # token-array equality
else:
    correct ⇔ learner_order equals options.correct_order          # today's behavior
```

## Test vectors (must all pass)

fill_blank — accepted `["parameters"]`, rejected `[]` unless noted:

| input | accepted | rejected | expected |
|---|---|---|---|
| `parameters` | `["parameters"]` | | CORRECT (exact) |
| `  Parameters. ` | `["parameters"]` | | CORRECT (exact — normalization) |
| `paramaters` | `["parameters"]` | | CORRECT (typo, transposition) |
| `parameterss` | `["parameters"]` | | CORRECT (typo, insertion) |
| `paramters` | `["parameters"]` | | CORRECT (typo, deletion) |
| `paramatersz` | `["parameters"]` | | CORRECT (typo, distance 2, len ≥ 10) |
| `pearameterz` | `["parameters"]` | | WRONG (distance 3) |
| `LLM` | `["LLM"]` | | CORRECT (exact) |
| `SLM` | `["LLM"]` | | WRONG (len < 5 ⇒ no tolerance) |
| `slm` | `["LLM"]` | `["SLM"]` | WRONG (rejected) |
| `LLMs` | `["LLM"]` | | WRONG (len < 5 ⇒ no tolerance) |
| `smal` | `["small"]` | `["large"]` | CORRECT (typo, distance 1 on len 5) |
| `larg` | `["small"]` | `["large"]` | WRONG (near a rejected answer) |
| `sumarize` | `["summarize", "summarise"]` | | CORRECT (typo vs either) |
| `` (empty) | anything | | WRONG |
| `små` vs accepted `små` (NFKC forms differ) | | | CORRECT (exact after NFKC) |

arrange_sentence:

| learner order | data | expected |
|---|---|---|
| equals `correct_order` | no `accepted_orders` | CORRECT |
| any other order | no `accepted_orders` | WRONG |
| equals `accepted_orders[3]` | `accepted_orders` present | CORRECT |
| order not in `accepted_orders` | `accepted_orders` present | WRONG |

## Reference implementation (TypeScript)

Port faithfully; this exact code is what the course authors' preview uses, so
learner grading will match what authors saw when reviewing content.

```typescript
export type GapGrade = "exact" | "typo" | "wrong";

export function normalizeAnswer(s: string): string {
    return (s || "")
        .normalize("NFKC")
        .toLowerCase()
        .replace(/[.,!?;:'"()‘’“”]+/g, " ")
        .trim()
        .split(/\s+/)
        .join(" ");
}

/** Optimal String Alignment distance (Damerau-Levenshtein w/ adjacent transposition). */
export function editDistance(a: string, b: string): number {
    const m = a.length, n = b.length;
    if (m === 0) return n;
    if (n === 0) return m;
    const d: number[][] = Array.from({ length: m + 1 }, (_, i) =>
        Array.from({ length: n + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0))
    );
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            const cost = a[i - 1] === b[j - 1] ? 0 : 1;
            d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
            if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
                d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
            }
        }
    }
    return d[m][n];
}

export function typoTolerance(normalizedAnswerLength: number): number {
    if (normalizedAnswerLength < 5) return 0;
    if (normalizedAnswerLength < 10) return 1;
    return 2;
}

export function gradeGapAnswer(
    userInput: string,
    acceptedAnswers: string[],
    rejectedAnswers: string[] = [],
): GapGrade {
    const input = normalizeAnswer(userInput);
    if (!input) return "wrong";
    const accepted = (acceptedAnswers || []).map(normalizeAnswer).filter(Boolean);

    if (accepted.includes(input)) return "exact";

    for (const rejected of (rejectedAnswers || []).map(normalizeAnswer).filter(Boolean)) {
        if (input === rejected || editDistance(input, rejected) <= typoTolerance(rejected.length)) {
            return "wrong";
        }
    }
    for (const answer of accepted) {
        if (editDistance(input, answer) <= typoTolerance(answer.length)) return "typo";
    }
    return "wrong";
}

export function isArrangeCorrect(
    learnerOrder: string[],
    correctOrder: string[],
    acceptedOrders?: string[][],
): boolean {
    const eq = (a: string[], b: string[]) => a.length === b.length && a.every((t, i) => t === b[i]);
    if (acceptedOrders && acceptedOrders.length > 0) {
        return acceptedOrders.some(o => eq(learnerOrder, o));
    }
    return eq(learnerOrder, correctOrder);
}
```

## Scope & constraints

- Apply to `fill_blank` and `arrange_sentence` grading everywhere answers are
  checked (quiz sessions, practice, review) on BOTH mobile and web.
- `multiple_choice` / `true_false` grading is unchanged.
- **Backwards compatibility is mandatory:** all fields may be absent on
  existing courses — missing `rejected_answers` ⇒ `[]`, missing
  `accepted_orders` ⇒ grade by `correct_order` equality. No migration needed.
- If the importer whitelists `options` keys, allow `rejected_answers`,
  `interchangeable_groups`, `accepted_orders` to pass through.
- Do not log learner inputs at grading time beyond what the app already logs.

## Acceptance criteria

1. All test vectors above pass (add them as unit tests).
2. A typo-accepted answer shows the “small typo” hint with the canonical spelling.
3. An `arrange_sentence` question with `accepted_orders` accepts every listed
   order and rejects everything else.
4. Courses imported before this change behave exactly as before.

## Versioning

This is grading spec **v1** (generator side: `GRADING_SPEC.md` in the
techlingo-agent-framework repo, reference file
`web/src/components/viewer/grading.ts`). If you need to deviate, coordinate a
spec version bump with the course-generator team rather than diverging silently.

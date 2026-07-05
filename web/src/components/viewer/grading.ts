/**
 * Reference implementation of the TechLingo answer-grading spec (GRADING_SPEC.md, v1).
 *
 * The Run Viewer grades exactly like the production app should, so authors see
 * real behavior when previewing. Keep this file in sync with GRADING_SPEC.md —
 * the mobile/web app ports the spec, not this code.
 */

export type GapGrade = "exact" | "typo" | "wrong";

/** Spec §1: normalization applied to both the learner's input and every accepted answer. */
export function normalizeAnswer(s: string): string {
    return (s || "")
        .normalize("NFKC")           // unicode compatibility (æ, ø, å, ligatures, width)
        .toLowerCase()
        .replace(/[.,!?;:'"()‘’“”]+/g, " ") // punctuation → space
        .trim()
        .split(/\s+/)
        .join(" ");
}

/**
 * Spec §2: optimal string alignment (restricted Damerau-Levenshtein) distance —
 * insertions, deletions, substitutions, and adjacent transpositions all cost 1.
 */
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
            d[i][j] = Math.min(
                d[i - 1][j] + 1,        // deletion
                d[i][j - 1] + 1,        // insertion
                d[i - 1][j - 1] + cost, // substitution
            );
            if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
                d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1); // transposition
            }
        }
    }
    return d[m][n];
}

/** Spec §3: how many typo edits an answer of this (normalized) length may tolerate. */
export function typoTolerance(normalizedAnswerLength: number): number {
    if (normalizedAnswerLength < 5) return 0;  // short tokens: exact only (LLM vs SLM!)
    if (normalizedAnswerLength < 10) return 1;
    return 2;
}

/**
 * Spec §4: grade one gap answer.
 *
 * `rejectedAnswers` are known confusables (e.g. gap "LLM" rejects "SLM") that
 * fuzzy matching must never absorb: if the input is a rejected answer (or
 * within ITS tolerance), the answer is wrong regardless of closeness to an
 * accepted one. Current course data does not carry rejected_answers yet
 * (schema phase 2) — pass [] until it does.
 */
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

import { ChoiceUIOption, ChoiceOption } from "./types";

export function getDeterministicRandom(seed: number) {
    return function () {
        var t = seed += 0x6D2B79F5;
        t = Math.imul(t ^ t >>> 15, t | 1);
        t ^= t + Math.imul(t ^ t >>> 7, t | 61);
        return ((t ^ t >>> 14) >>> 0) / 4294967296;
    }
}

export function shuffle<T>(array: T[], seed: number): T[] {
    const rng = getDeterministicRandom(seed);
    const copy = [...array];
    for (let i = copy.length - 1; i > 0; i--) {
        const j = Math.floor(rng() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

export function getChoiceOptions(options: ChoiceOption[], seed: number): ChoiceUIOption[] {
    const opts = options.map((o, i) => ({
        id: i.toString(),
        label: o.text,
        is_correct: o.is_correct,
        feedback: o.feedback,
        rationale: o.rationale
    }));
    return shuffle(opts, seed);
}

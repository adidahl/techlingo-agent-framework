export interface Feedback {
    intrinsic: string;
    instructional: string;
}

export interface ChoiceOption {
    text: string;
    is_correct: boolean;
    feedback?: Feedback | null;
    rationale?: string | null;
}

export interface BaseExercise {
    blooms_level: string;
    question_type: string;
    prompt: string;
    feedback_for_correct?: string | null;
}

export interface SingleChoiceExercise extends BaseExercise {
    question_type: "single_choice";
    options: ChoiceOption[];
}

export interface MultiChoiceExercise extends BaseExercise {
    question_type: "multi_choice";
    options: ChoiceOption[];
}

export interface TrueFalseExercise extends BaseExercise {
    question_type: "true_false";
    statement: string;
    correct_answer: boolean;
    feedback_for_incorrect?: Feedback | null;
}

export interface FillGapsPart {
    type: "text" | "gap";
    text?: string;       // for text parts
    placeholder?: string; // for gap parts
    accepted_answers?: string[]; // for gap parts
    rejected_answers?: string[]; // confusables fuzzy matching must never accept
}

export interface FillGapsExercise extends BaseExercise {
    question_type: "fill_gaps";
    parts: FillGapsPart[];
}

export interface RearrangeExercise extends BaseExercise {
    question_type: "rearrange";
    word_bank: string[];
    correct_order: string[];
    // 0-based positions in correct_order that may permute among themselves
    interchangeable_groups?: number[][];
}

export type Exercise =
    | SingleChoiceExercise
    | MultiChoiceExercise
    | TrueFalseExercise
    | FillGapsExercise
    | RearrangeExercise;

export interface ChoiceUIOption {
    id: string;
    label: string;
    is_correct: boolean;
    feedback?: Feedback | null;
    rationale?: string | null;
}

export interface QuestionProps<T extends Exercise> {
    exercise: T;
    value: any;
    onChange: (val: any) => void;
    submitted: boolean;
    seed: number;
}

export interface Flashcard {
    front: string;
    back: string;
    hint?: string;
}

export interface Lesson {
    title: string;
    slo: string;
    exercises: Exercise[];
    flashcards?: Flashcard[];
}

export interface Module {
    title: string;
    lessons: Lesson[];
}

export interface Course {
    schema_version: string;
    title?: string;   // internal format (current runs)
    topic?: string;   // legacy runs
    difficulty: any;
    modules: Module[];
}

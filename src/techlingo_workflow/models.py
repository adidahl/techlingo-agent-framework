from __future__ import annotations

from .config import WorkflowConfig, DifficultyLevel

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class BloomsLevel(str, Enum):
    remembering = "Remembering"
    understanding = "Understanding"
    applying = "Applying"
    analyzing_evaluating = "Analyzing/Evaluating"


class Feedback(BaseModel):
    intrinsic: str = Field(..., description="Immediate simulated consequence.")
    instructional: str = Field(..., description="Coaching explanation of the concept/principle.")

FeedbackLike = Feedback | str


class ConceptAtom(BaseModel):
    """One teachable fact/term extracted from the source — the unit exercises target.

    A lesson carries several atoms; every exercise points at exactly one via
    `concept_id`, which is what lets validation enforce coverage (no concept
    over-drilled, none skipped) and lets the app track skills per question.
    """

    id: str = Field(..., description="Stable kebab-case id, unique within the course (e.g., 'object-detection').")
    label: str = Field(..., description="Short human name of the concept/term (e.g., 'Object detection').")
    summary: str = Field(..., description="The teachable fact, stated directly from the source (1-2 sentences).")
    depth: Optional[Literal["fact", "mechanism", "decision"]] = Field(
        default=None,
        description="How far the concept can be meaningfully drilled: 'fact' (a term/definition), "
        "'mechanism' (how something works, a distinction or trade-off), 'decision' (when to "
        "choose it over alternatives). Drives the exercise quota per difficulty rung "
        "(ARCHITECTURE.md §3.1/§3.3).",
    )
    confusable_with: list[str] = Field(
        default_factory=list,
        description="Ids of sibling concepts a learner could confuse this with — the preferred distractor pool.",
    )


class ChoiceOption(BaseModel):
    text: str
    is_correct: bool
    error_type: Optional[str] = Field(
        default=None,
        description="Short label describing the conceptual error for incorrect options (e.g., Performance Error).",
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Explanation of why this option is correct or incorrect in this context.",
    )
    better_fit: Optional[str] = Field(
        default=None,
        description="Context where this incorrect option would be the correct answer (distractors only).",
    )
    feedback: Optional[FeedbackLike] = None


class ExerciseBase(BaseModel):
    blooms_level: BloomsLevel
    question_type: Literal["single_choice", "multi_choice", "true_false", "fill_gaps", "rearrange"]
    prompt: str = Field(..., description="Learner-facing prompt (may include scenario/context).")
    concept_id: Optional[str] = Field(
        default=None,
        description="Id of the ConceptAtom (from the lesson's concepts) this exercise tests.",
    )


class SingleChoiceExercise(ExerciseBase):
    question_type: Literal["single_choice"] = "single_choice"
    options: list[ChoiceOption]
    feedback_for_correct: Optional[str] = Field(default=None, description="Optional brief reinforcement for correct.")


class MultiChoiceExercise(ExerciseBase):
    question_type: Literal["multi_choice"] = "multi_choice"
    options: list[ChoiceOption]
    feedback_for_correct: Optional[str] = Field(default=None, description="Optional brief reinforcement for correct.")


class TrueFalseExercise(ExerciseBase):
    question_type: Literal["true_false"] = "true_false"
    statement: str = Field(..., description="The statement the learner marks True/False.")
    correct_answer: bool
    feedback_for_correct: Optional[str] = Field(default=None, description="Optional brief reinforcement for correct.")
    feedback_for_incorrect: Optional[FeedbackLike] = Field(
        default=None,
        description="Feedback shown when learner chooses the wrong value (paired intrinsic + instructional).",
    )


class FillGapsTextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class FillGapsGapPart(BaseModel):
    type: Literal["gap"] = "gap"
    accepted_answers: list[str] = Field(..., description="Accepted answers for this gap (case-insensitive match).")
    rejected_answers: list[str] = Field(
        default_factory=list,
        description="Confusable terms that graders must NEVER accept, even via typo "
        "tolerance (e.g. gap 'LLM' rejects 'SLM'). Typically the concept's confusables.",
    )
    placeholder: Optional[str] = Field(default=None, description="Optional placeholder shown in the UI.")


# Plain union (no discriminator) so the JSON schema emits `anyOf` instead of
# `oneOf`; OpenAI Structured Outputs rejects `oneOf`. Pydantic still discriminates
# correctly via each member's distinct `type` Literal. See RESILIENCE_PLAN.md §9.
FillGapsPart = Union[FillGapsTextPart, FillGapsGapPart]


class FillGapsExercise(ExerciseBase):
    question_type: Literal["fill_gaps"] = "fill_gaps"
    parts: list[FillGapsPart] = Field(
        ...,
        description="Structured sentence parts with gaps; UI renders by interleaving text and input fields.",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Why the accepted answer is the right term here (shown after answering, right or wrong).",
    )
    feedback_for_incorrect: Optional[FeedbackLike] = Field(
        default=None,
        description="Coaching shown when the learner types a wrong term (paired intrinsic + instructional).",
    )


class RearrangeExercise(ExerciseBase):
    question_type: Literal["rearrange"] = "rearrange"
    word_bank: list[str] = Field(..., description="Tokens to rearrange.")
    correct_order: list[str] = Field(..., description="Tokens in the correct order (must use the same tokens).")
    interchangeable_groups: list[list[int]] = Field(
        default_factory=list,
        description="0-based positions in correct_order that may be permuted among "
        "themselves (each group independently) and still be correct. Declares "
        "legitimately order-flexible segments; graders accept any such arrangement. "
        "Generated questions should have a unique order (empty list); this is "
        "authored mainly by human editors.",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Why this order is correct (the logic/sequence), shown after answering.",
    )
    feedback_for_incorrect: Optional[FeedbackLike] = Field(
        default=None,
        description="Coaching shown on a wrong arrangement (paired intrinsic + instructional).",
    )


# Plain union (no discriminator) → schema emits `anyOf`, accepted by OpenAI
# Structured Outputs. Members are still discriminated by their distinct
# `question_type` Literal. See RESILIENCE_PLAN.md §9.
Exercise = Union[SingleChoiceExercise, MultiChoiceExercise, TrueFalseExercise, FillGapsExercise, RearrangeExercise]


class Flashcard(BaseModel):
    front: str
    back: str
    hint: Optional[str] = None


class Lesson(BaseModel):
    title: str
    slo: str = Field(..., description="Single, measurable learning objective.")
    concepts: list[ConceptAtom] = Field(
        default_factory=list,
        description="Content pack for this lesson: the concept atoms exercises must cover.",
    )
    exercises: list[Exercise] = Field(default_factory=list)
    flashcards: list[Flashcard] = Field(default_factory=list)


class LessonGen(Lesson):
    """LLM response shape for chunked per-lesson generation/rewriting.

    Chunking keeps each completion's output bounded by lesson size (not course
    size), so raising exercises_per_lesson can never hit output-token limits.
    """

    thought_process: Optional[list[str]] = Field(
        default=None,
        description="Step-by-step reasoning log from the agent.",
    )


class Module(BaseModel):
    title: str
    lessons: list[Lesson] = Field(default_factory=list)


class Course(BaseModel):
    title: str = "AI Core Capabilities and Responsibility"
    difficulty: DifficultyLevel = DifficultyLevel.beginner
    modules: list[Module] = Field(default_factory=list)
    source_summary: Optional[str] = Field(
        default=None,
        description="Optional short summary of the source content used to generate this course.",
    )
    thought_process: Optional[list[str]] = Field(
        default=None,
        description="Step-by-step reasoning log from the agent.",
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    schema_version: str = "v2"


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    path: str = Field(..., description="Dotpath-like location in the output (e.g., modules[0].lessons[2]).")
    message: str


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    counts: dict[str, Any] = Field(default_factory=dict)
    repaired: bool = False
    thought_process: Optional[list[str]] = Field(
        default=None,
        description="Step-by-step reasoning log from the agent.",
    )


class TextPartType(str, Enum):
    term = "term"
    definition = "definition"
    explanation = "explanation"
    example = "example"
    analogy = "analogy"
    subject = "subject"


class TextPart(BaseModel):
    type: TextPartType
    content: str
    context: Optional[str] = Field(default=None, description="Surrounding context if needed for clarity.")


class TextAnalysisMetadata(BaseModel):
    total_parts: int
    parts_by_type: dict[TextPartType, int]
    estimated_questions_needed: int = Field(..., description="Estimated number of questions needed to cover all parts.")
    completeness_score: float = Field(..., description="0.0 to 1.0 score of how complete this analysis is.")


class TextAnalysisResult(BaseModel):
    input_summary: str
    parts: list[TextPart]
    metadata: TextAnalysisMetadata
    recommended_config: WorkflowConfig = Field(..., description="Recommended workflow configuration based on analysis.")
    thought_process: Optional[list[str]] = Field(
        default=None,
        description="Step-by-step reasoning log from the agent.",
    )



class PipelineState(BaseModel):
    """State passed through the workflow between executors."""

    run_id: str
    run_dir: str

    input_text: str
    model_id: str = Field(..., description="Backend-qualified model label (e.g. 'claude-code:sonnet', 'codex').")
    difficulty: DifficultyLevel = DifficultyLevel.beginner

    # Step artifacts (structured)
    a1_course_map: Optional[dict[str, Any]] = None
    a2_course: Optional[Course] = None
    a3_course: Optional[Course] = None
    a4_course: Optional[Course] = None
    a5_course: Optional[Course] = None
    validation_report: Optional[ValidationReport] = None
    analysis_result: Optional[TextAnalysisResult] = None

    # Best attempt seen across A5->A2 lesson-content retries. Regeneration can
    # come back WORSE than the attempt it was meant to fix, so the final output
    # is always the best-scoring attempt. An A5->A1 map retry clears these because
    # attempts from different authoritative maps cannot be mixed.
    best_course: Optional[Course] = None
    best_report: Optional[ValidationReport] = None

    # Lessons touched in the current loop pass, as "mi:li" keys. On a retry, A2
    # regenerates only the failing lessons and A3/A4 rewrite only these — clean
    # lessons are never re-run through an LLM again (cheaper, and immune to
    # regeneration-made-it-worse). None means all lessons are dirty (first pass).
    dirty_lessons: Optional[list[str]] = None
    
    # Configuration
    config: WorkflowConfig = Field(default_factory=lambda: WorkflowConfig())
    override_title: Optional[str] = Field(default=None, description="Manual override for the output course/module title.")
    retry_count: int = Field(default=0, description="Number of times the workflow has looped back for self-correction.")
    retry_target: Optional[Literal["a1", "a2"]] = Field(
        default=None,
        description=(
            "The authoritative stage that owns the current validation errors. "
            "Concept-pack errors return to A1; lesson-content errors return to A2."
        ),
    )



class WorkflowRunResult(BaseModel):
    run_id: str
    run_dir: str
    course: Course
    validation_report: ValidationReport



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
    placeholder: Optional[str] = Field(default=None, description="Optional placeholder shown in the UI.")


FillGapsPart = Annotated[Union[FillGapsTextPart, FillGapsGapPart], Field(discriminator="type")]


class FillGapsExercise(ExerciseBase):
    question_type: Literal["fill_gaps"] = "fill_gaps"
    parts: list[FillGapsPart] = Field(
        ...,
        description="Structured sentence parts with gaps; UI renders by interleaving text and input fields.",
    )


class RearrangeExercise(ExerciseBase):
    question_type: Literal["rearrange"] = "rearrange"
    word_bank: list[str] = Field(..., description="Tokens to rearrange.")
    correct_order: list[str] = Field(..., description="Tokens in the correct order (must use the same tokens).")


Exercise = Annotated[
    Union[SingleChoiceExercise, MultiChoiceExercise, TrueFalseExercise, FillGapsExercise, RearrangeExercise],
    Field(discriminator="question_type"),
]


class Flashcard(BaseModel):
    front: str
    back: str
    hint: Optional[str] = None


class Lesson(BaseModel):
    title: str
    slo: str = Field(..., description="Single, measurable learning objective.")
    exercises: list[Exercise] = Field(default_factory=list)
    flashcards: list[Flashcard] = Field(default_factory=list)


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


class PipelineState(BaseModel):
    """State passed through the workflow between executors."""

    run_id: str
    run_dir: str

    input_text: str
    model_id: str = Field(..., description="OpenAI chat model id to use (e.g., from OPENAI_CHAT_MODEL_ID).")
    difficulty: DifficultyLevel = DifficultyLevel.beginner

    # Step artifacts (structured)
    a1_course_map: Optional[dict[str, Any]] = None
    a2_course: Optional[Course] = None
    a3_course: Optional[Course] = None
    a4_course: Optional[Course] = None
    a5_course: Optional[Course] = None
    validation_report: Optional[ValidationReport] = None
    
    # Configuration
    config: WorkflowConfig = Field(default_factory=lambda: WorkflowConfig())


class WorkflowRunResult(BaseModel):
    run_id: str
    run_dir: str
    course: Course
    validation_report: ValidationReport



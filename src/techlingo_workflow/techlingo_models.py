"""TechLingo-native output contract models.

These models define the *emitted* `course.json` shape that maps 1:1 onto the
TechLingo application hierarchy and DB tables:

    TLCourse    -> courses
    TLModule    -> lessons
    TLUnit      -> units
    TLQuestion  -> questions (FK unit_id)
    TLFlashcard -> unit_pages (page_type="exercise")

The generator produces our rich *internal* models (see ``models.py``) through
the A1-A5 pipeline; ``emit.py`` deterministically transforms them into these
TechLingo-native models right before writing the final ``course.json``. Keeping
this as a separate, explicit contract lets the importer stay trivial (upsert
hierarchy + insert these fields) and lets us validate the exact output shape.

See TECHLINGO_OUTPUT_PLAN.md for the full mapping and decisions.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

# The only question_type values the TechLingo app accepts. `matching` also
# exists in the app but the generator does not emit it (v1).
TLQuestionType = Literal["multiple_choice", "true_false", "fill_blank", "arrange_sentence"]

SCHEMA_VERSION = "techlingo-course-v1"


class TLFlashcard(BaseModel):
    """Maps to a `unit_pages` row (page_type="exercise")."""

    import_key: str
    front: str
    back: str
    hint: Optional[str] = None


class TLQuestion(BaseModel):
    """Maps to a `questions` row (FK unit_id)."""

    import_key: str
    question_type: TLQuestionType
    question_text: str
    # `options` is stored in a jsonb column, so it carries the rich structure the
    # UI/importer needs (options[], parts, word_bank/correct_order) plus metadata
    # (blooms_level, original_question_type, per-option rationale/feedback...).
    options: dict[str, Any] = Field(default_factory=dict)
    # Always a string (questions.correct_answer is `text not null`). Encoding is
    # per-type: index ("0") / index-array ("[0,1]") for multiple_choice,
    # "true"/"false", accepted answer(s) for fill_blank, joined sentence for
    # arrange_sentence. See emit.py.
    correct_answer: str
    explanation: Optional[str] = None
    points: int = 1


class TLUnit(BaseModel):
    """Maps to a `units` row. title -> units.title, slo -> units.description."""

    import_key: str
    title: str
    slo: str
    exercises: List[TLQuestion] = Field(default_factory=list)
    flashcards: List[TLFlashcard] = Field(default_factory=list)


class TLModule(BaseModel):
    """Maps to a `lessons` row."""

    import_key: str
    title: str
    description: Optional[str] = None
    lessons: List[TLUnit] = Field(default_factory=list)


class TLCourse(BaseModel):
    """Maps to a `courses` row.

    category_id / path_id are NOT emitted here; the importer receives them as
    CLI parameters (they live above Course in the TechLingo hierarchy).
    """

    schema_version: Literal["techlingo-course-v1"] = SCHEMA_VERSION
    import_key: str
    title: str
    difficulty: str = "beginner"
    source_summary: Optional[str] = None
    modules: List[TLModule] = Field(default_factory=list)

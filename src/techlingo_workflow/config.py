from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from pydantic import BaseModel, Field, model_validator


from enum import Enum

class DifficultyLevel(str, Enum):
    novice = "novice"
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"


class WorkflowConfig(BaseModel):
    """Configuration for the Techlingo workflow constraints."""
    
    # Global Settings
    difficulty: DifficultyLevel = Field(DifficultyLevel.beginner, description="Overall course difficulty.")
    
    # A1: Curriculum Structure
    modules_count: int = Field(1, description="Fixed number of modules in the course.")
    min_lessons_total: int = Field(20, description="Minimum total lessons across all modules.")
    max_lessons_total: int = Field(25, description="Maximum total lessons across all modules.")
    
    # A2: Lesson Content
    exercises_per_lesson: int = Field(15, description="Number of exercises to generate per lesson.")
    flashcards_per_lesson: int = Field(8, description="Number of flashcards to generate per lesson.")
    
    # Distributions
    blooms_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "Remembering": 3,
            "Understanding": 4,
            "Applying": 4,
            "Analyzing/Evaluating": 4
        },
        description="Number of exercises per Bloom's Taxonomy level."
    )
    
    question_type_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "single_choice": 4,
            "multi_choice": 4,
            "true_false": 3,
            "fill_gaps": 2,
            "rearrange": 2
        },
        description="Number of exercises per question type."
    )

    @model_validator(mode='after')
    def check_distributions(self) -> WorkflowConfig:
        # Check Bloom's
        blooms_sum = sum(self.blooms_distribution.values())
        if blooms_sum != self.exercises_per_lesson:
            raise ValueError(
                f"blooms_distribution sums to {blooms_sum} but exercises_per_lesson is "
                f"{self.exercises_per_lesson}. Fix: make the blooms_distribution values add up "
                f"to {self.exercises_per_lesson} (or change exercises_per_lesson to {blooms_sum})."
            )

        # Check Question Types
        types_sum = sum(self.question_type_distribution.values())
        if types_sum != self.exercises_per_lesson:
            raise ValueError(
                f"question_type_distribution sums to {types_sum} but exercises_per_lesson is "
                f"{self.exercises_per_lesson}. Fix: make the question_type_distribution values add up "
                f"to {self.exercises_per_lesson} (or change exercises_per_lesson to {types_sum})."
            )

        # Bloom <-> question-type coupling feasibility. Applying and
        # Analyzing/Evaluating exercises must be scenario-based choice questions
        # (single/multi), while true_false / fill_gaps / rearrange are mechanically
        # Remembering/Understanding formats. That coupling is only satisfiable when
        # the higher-order Bloom slots fit inside the choice-type slots.
        higher_order = self.blooms_distribution.get("Applying", 0) + self.blooms_distribution.get(
            "Analyzing/Evaluating", 0
        )
        choice_slots = self.question_type_distribution.get("single_choice", 0) + self.question_type_distribution.get(
            "multi_choice", 0
        )
        if higher_order > choice_slots:
            raise ValueError(
                f"Applying + Analyzing/Evaluating ({higher_order}) exceeds single_choice + multi_choice "
                f"({choice_slots}). Higher-order Bloom exercises must be scenario-based choice questions, "
                f"so raise the choice-type counts or lower the higher-order Bloom counts."
            )

        # Check lesson bounds are sane and can accommodate every module (>= 1 lesson each).
        if self.min_lessons_total > self.max_lessons_total:
            raise ValueError(
                f"min_lessons_total ({self.min_lessons_total}) cannot exceed "
                f"max_lessons_total ({self.max_lessons_total})."
            )
        if self.modules_count > self.min_lessons_total:
            raise ValueError(
                f"modules_count ({self.modules_count}) exceeds min_lessons_total "
                f"({self.min_lessons_total}); each module needs at least one lesson. "
                f"Fix: raise min_lessons_total to >= {self.modules_count} (or lower modules_count)."
            )

        return self

def get_default_config() -> WorkflowConfig:
    return WorkflowConfig()

def load_workflow_config(path: Path | None) -> WorkflowConfig:
    if path is None or not path.exists():
        return get_default_config()
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    return WorkflowConfig.model_validate(data)

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
            "single_choice": 3,
            "multi_choice": 3,
            "true_false": 3,
            "fill_gaps": 3,
            "rearrange": 3
        },
        description="Number of exercises per question type."
    )

    @model_validator(mode='after')
    def check_distributions(self) -> WorkflowConfig:
        # Check Bloom's
        blooms_sum = sum(self.blooms_distribution.values())
        if blooms_sum != self.exercises_per_lesson:
            raise ValueError(
                f"Sum of blooms_distribution ({blooms_sum}) must match exercises_per_lesson ({self.exercises_per_lesson})."
            )
            
        # Check Question Types
        types_sum = sum(self.question_type_distribution.values())
        if types_sum != self.exercises_per_lesson:
            raise ValueError(
                f"Sum of question_type_distribution ({types_sum}) must match exercises_per_lesson ({self.exercises_per_lesson})."
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

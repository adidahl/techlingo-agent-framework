from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from pydantic import BaseModel, Field


class WorkflowConfig(BaseModel):
    """Configuration for the Techlingo workflow constraints."""
    
    # A1: Curriculum Structure
    modules_count: int = Field(6, description="Fixed number of modules in the course.")
    min_lessons_total: int = Field(20, description="Minimum total lessons across all modules.")
    max_lessons_total: int = Field(25, description="Maximum total lessons across all modules.")
    
    # A2: Lesson Content
    exercises_per_lesson: int = Field(8, description="Number of exercises to generate per lesson.")
    flashcards_per_lesson: int = Field(8, description="Number of flashcards to generate per lesson.")
    
    # Distributions
    blooms_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "Remembering": 2,
            "Understanding": 2,
            "Applying": 2,
            "Analyzing/Evaluating": 2
        },
        description="Number of exercises per Bloom's Taxonomy level."
    )
    
    question_type_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "single_choice": 1,
            "multi_choice": 2,
            "true_false": 2,
            "fill_gaps": 2,
            "rearrange": 1
        },
        description="Number of exercises per question type."
    )

def get_default_config() -> WorkflowConfig:
    return WorkflowConfig()

def load_workflow_config(path: Path | None) -> WorkflowConfig:
    if path is None or not path.exists():
        return get_default_config()
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    return WorkflowConfig.model_validate(data)

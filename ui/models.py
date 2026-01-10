from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from techlingo_workflow.models import Feedback


@dataclass(frozen=True)
class ChoiceUIOption:
    id: str
    label: str
    is_correct: bool
    feedback: Optional[Feedback]
    rationale: Optional[str] = None

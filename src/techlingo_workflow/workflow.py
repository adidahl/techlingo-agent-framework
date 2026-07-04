from __future__ import annotations

import warnings

from agent_framework import WorkflowBuilder
from .models import PipelineState
from .executors import (
    a1_modularizer,
    a2_scaffolder,
    a3_scenario_designer,
    a4_feedback_architect,
    a5_validator,
    content_inventory,
    text_analyzer,
    text_reviewer,
)

# Filter warnings at the module level
warnings.filterwarnings(
    "ignore",
    message=r".*Adding an edge with Executor or AgentProtocol instances.*",
)

def should_loop(state: PipelineState) -> bool:
    # Loop back if validation report exists and has errors
    return state.validation_report is not None and not state.validation_report.ok

# Pre-build workflows using function references
# We ignore the warning because we purposely build these once at module level
# A0 (content inventory) runs first so A1 gets a coverage checklist of every
# term/definition/example in the source — the content packs A2 draws questions
# from are built against it.
_techlingo_workflow = (
    WorkflowBuilder()
    .set_start_executor(content_inventory)
    .add_edge(content_inventory, a1_modularizer)
    .add_edge(a1_modularizer, a2_scaffolder)
    .add_edge(a2_scaffolder, a3_scenario_designer)
    .add_edge(a3_scenario_designer, a4_feedback_architect)
    .add_edge(a4_feedback_architect, a5_validator)
    .add_edge(a5_validator, a2_scaffolder, condition=should_loop)
    .build()
)

_analysis_workflow = (
    WorkflowBuilder()
    .set_start_executor(text_analyzer)
    .add_edge(text_analyzer, text_reviewer)
    .build()
)

def build_techlingo_workflow():
    """Returns the pre-built Techlingo workflow."""
    return _techlingo_workflow

def build_analysis_workflow():
    """Returns the pre-built Analysis workflow."""
    return _analysis_workflow



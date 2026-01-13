from __future__ import annotations

import warnings

from agent_framework import WorkflowBuilder
from .models import PipelineState
from .executors import a1_modularizer, a2_scaffolder, a3_scenario_designer, a4_feedback_architect, a5_validator, text_analyzer, text_reviewer

def should_loop(state: PipelineState) -> bool:
    # Loop back if validation report exists and has errors
    # (The executor A5 already checks retry limit before sending)
    return state.validation_report is not None and not state.validation_report.ok

def build_techlingo_workflow():
    """Build the strict A1→A5 sequential workflow graph with feedback loop."""
    # Agent Framework warns that adding edges with executor instances isn't recommended.
    # For this MVP we build a fresh workflow per run; filter to avoid spamming the console.
    warnings.filterwarnings(
        "ignore",
        message=r"Adding an edge with Executor or AgentProtocol instances directly is not recommended.*",
    )
    return (
        WorkflowBuilder()
        .set_start_executor(a1_modularizer)
        .add_edge(a1_modularizer, a2_scaffolder)
        .add_edge(a2_scaffolder, a3_scenario_designer)
        .add_edge(a3_scenario_designer, a4_feedback_architect)
        .add_edge(a4_feedback_architect, a5_validator)
        # Self-correction loop: A5 -> A2
        .add_edge(a5_validator, a2_scaffolder, condition=should_loop)
        .build()
    )

def build_analysis_workflow():
    """Build the Analyzer→Reviewer sequential workflow graph."""
    warnings.filterwarnings(
        "ignore",
        message=r"Adding an edge with Executor or AgentProtocol instances directly is not recommended.*",
    )
    return (
        WorkflowBuilder()
        .set_start_executor(text_analyzer)
        .add_edge(text_analyzer, text_reviewer)
        .build()
    )



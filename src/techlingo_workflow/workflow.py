from __future__ import annotations

import warnings

from agent_framework import WorkflowBuilder

from .executors import a1_modularizer, a2_scaffolder, a3_scenario_designer, a4_feedback_architect, a5_validator


def build_techlingo_workflow():
    """Build the strict A1→A5 sequential workflow graph."""
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
        .build()
    )



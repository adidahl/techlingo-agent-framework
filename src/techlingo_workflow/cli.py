from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from .io import read_input_text, write_json, write_text
from .models import PipelineState, WorkflowRunResult
from .models import DifficultyLevel
from .workflow import build_techlingo_workflow


app = typer.Typer(no_args_is_help=True)


@app.callback()
def _root() -> None:
    """Techlingo Agent Framework CLI."""
    # Defining a callback forces Typer to keep subcommands even if there's only one.
    return


@app.command()
def run(
    input_text: Optional[str] = typer.Option(None, help="Raw source text to convert into a course."),
    input_file: Optional[Path] = typer.Option(None, exists=True, dir_okay=False, help="Path to a text file input."),
    out_dir: Path = typer.Option(Path("outputs"), help="Output directory for run artifacts."),
    dotenv_path: Optional[Path] = typer.Option(None, help="Optional .env path (defaults to .env in repo root)."),
    model_id: Optional[str] = typer.Option(
        None,
        help="OpenAI chat model id. If omitted, uses OPENAI_CHAT_MODEL_ID from .env/environment.",
    ),
    difficulty: DifficultyLevel = typer.Option(
        DifficultyLevel.beginner,
        help="Difficulty of generated questions.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--no-verbose",
        help="Print workflow progress events (and agent streaming updates when available).",
    ),
) -> None:
    """Run the Techlingo A1–A5 workflow and write JSON artifacts to disk."""
    # Important: load .env BEFORE reading OPENAI_* vars. (Typer's envvar= reads too early.)
    env_path = dotenv_path if dotenv_path is not None else Path(".env")
    load_dotenv(env_path, override=False)

    if not model_id:
        model_id = os.getenv("OPENAI_CHAT_MODEL_ID")

    if not os.getenv("OPENAI_API_KEY"):
        raise typer.BadParameter("OPENAI_API_KEY is required. Set it in .env.")

    if not model_id:
        raise typer.BadParameter(
            "OpenAI model id is required. Set OPENAI_CHAT_MODEL_ID in .env or pass --model-id."
        )

    text = read_input_text(input_text, str(input_file) if input_file else None)

    # Build workflow and state in-process so we can stream progress events.
    from .io import new_run_dir

    workflow = build_techlingo_workflow()
    run_id, run_dir = new_run_dir(out_dir)
    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        input_text=text,
        model_id=model_id,
        difficulty=difficulty,
    )

    typer.echo(f"Run started: {run_id}")
    typer.echo(f"Run dir: {run_dir}")
    typer.echo(f"Difficulty: {difficulty.value}")

    def _get_executor_id(evt: object) -> str | None:
        # Different AF versions may use slightly different attribute names.
        return (
            getattr(evt, "executor_id", None)
            or getattr(evt, "executorId", None)
            or getattr(evt, "ExecutorId", None)
        )

    async def _run() -> WorkflowRunResult:
        output: WorkflowRunResult | None = None
        started_at: dict[str, float] = {}

        async for evt in workflow.run_stream(state):
            name = evt.__class__.__name__
            executor_id = _get_executor_id(evt)

            # Always surface explicit stage logs emitted from inside executors.
            if name == "StageLogEvent":
                msg = getattr(evt, "message", None)
                ts = time.strftime("%H:%M:%S")
                if msg:
                    typer.echo(f"[{ts}] {msg}")

            # Always show stage progress (so it never looks "stuck").
            ts = time.strftime("%H:%M:%S")
            if name in {"ExecutorInvokedEvent", "ExecutorInvokeEvent"} and executor_id:
                started_at[executor_id] = time.monotonic()
                typer.echo(f"[{ts}] START {executor_id}")

            elif name in {"ExecutorCompletedEvent", "ExecutorCompleteEvent"} and executor_id:
                dt = ""
                if executor_id in started_at:
                    dt = f" ({time.monotonic() - started_at[executor_id]:.1f}s)"
                typer.echo(f"[{ts}] DONE  {executor_id}{dt}")

            elif name == "ExecutorFailedEvent" and executor_id:
                details = getattr(evt, "details", None)
                msg = getattr(details, "message", None) if details is not None else None
                typer.echo(f"[{ts}] FAIL  {executor_id}: {msg or 'unknown error'}")

            # Extra noisier logs only when requested.
            if verbose:
                # If we see an unknown event type, print it (helps debugging "stuck" runs).
                if name not in {
                    "StageLogEvent",
                    "ExecutorInvokedEvent",
                    "ExecutorInvokeEvent",
                    "ExecutorCompletedEvent",
                    "ExecutorCompleteEvent",
                    "ExecutorFailedEvent",
                    "WorkflowOutputEvent",
                    "WorkflowErrorEvent",
                    "WorkflowWarningEvent",
                    "AgentRunUpdateEvent",
                    "AgentRunEvent",
                    "WorkflowStatusEvent",
                    "WorkflowStartedEvent",
                    "SuperStepStartedEvent",
                    "SuperStepCompletedEvent",
                }:
                    typer.echo(f"[{ts}] EVENT {name}: {evt}")

                if name in {"AgentRunUpdateEvent", "AgentRunEvent"} and executor_id:
                    data = getattr(evt, "data", None)
                    s = str(data) if data is not None else ""
                    s = s.replace("\n", " ").strip()
                    if s:
                        typer.echo(f"[{ts}] STREAM {executor_id}: {s[:120]}")

                elif name == "WorkflowWarningEvent":
                    details = getattr(evt, "details", None)
                    msg = getattr(details, "message", None) if details is not None else None
                    typer.echo(f"[{ts}] WARN: {msg or evt}")

            # Capture the final output
            if name == "WorkflowOutputEvent":
                output = getattr(evt, "data", None)

            if name == "WorkflowErrorEvent":
                exc = getattr(evt, "exception", None)
                raise RuntimeError(str(exc) if exc is not None else "WorkflowErrorEvent")

        if output is None:
            raise RuntimeError("Workflow completed without WorkflowOutputEvent.")
        return output

    try:
        result = asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nInterrupted (Ctrl+C). Partial outputs may exist in the run dir above.")
        raise typer.Exit(code=130)

    # Write canonical outputs at run root
    run_dir = Path(result.run_dir)
    write_json(run_dir / "course.json", result.course.model_dump(mode="json"))
    write_json(run_dir / "validation_report.json", result.validation_report.model_dump())

    # Minimal human-readable summary
    md_lines: list[str] = []
    md_lines.append(f"# {result.course.title}")
    md_lines.append("")
    for mod in result.course.modules:
        md_lines.append(f"## {mod.title}")
        for lesson in mod.lessons:
            md_lines.append(f"- **{lesson.title}** — {lesson.slo}")
            # Include one example question with rationales to show the new feature
            if lesson.exercises:
                ex = lesson.exercises[0]
                if hasattr(ex, "options"):
                    md_lines.append(f"  - Example Question: {ex.prompt}")
                    for opt in ex.options:
                        status = "✅" if opt.is_correct else "❌"
                        md_lines.append(f"    - {status} {opt.text}")
                        if opt.rationale:
                            md_lines.append(f"      - Rationale: {opt.rationale}")
                        if opt.better_fit:
                            md_lines.append(f"      - Better Fit: {opt.better_fit}")
    write_text(run_dir / "course.md", "\n".join(md_lines) + "\n")

    typer.echo(f"Run complete: {result.run_id}")
    typer.echo(f"Outputs: {result.run_dir}")



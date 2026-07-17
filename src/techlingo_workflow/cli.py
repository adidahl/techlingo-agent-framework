from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from .backends import (
    KNOWN_BACKENDS,
    preflight_backend,
    resolve_backend_name,
    resolve_model_label,
)
from .config import load_workflow_config, DifficultyLevel
from .io import read_input_text, write_json, write_text
from .models import PipelineState, WorkflowRunResult, TextAnalysisResult
from .workflow import build_techlingo_workflow, build_analysis_workflow


app = typer.Typer(no_args_is_help=True)

# Workspace-centric course commands (ARCHITECTURE.md Phase 1): init/build/compile/status.
from .cli_course import course_app  # noqa: E402

app.add_typer(course_app, name="course")


def _resolve_backend_and_model(backend: Optional[str], model_id: Optional[str]) -> tuple[str, str]:
    """Resolve (backend name, backend-qualified model label) or exit with a clean message."""
    try:
        backend_name = resolve_backend_name(backend)
        model_label = resolve_model_label(backend_name, model_id)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    return backend_name, model_label


def _preflight_or_die(backend_name: str) -> None:
    """Fail fast (binary/auth) before burning pipeline time on a broken backend."""
    failures = [(check, detail) for check, ok, detail in preflight_backend(backend_name) if not ok]
    if failures:
        problems = "\n".join(f"  - {check}: {detail}" for check, detail in failures)
        raise typer.BadParameter(
            f"Backend '{backend_name}' failed preflight:\n{problems}"
        )


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
        help="Model id for the chosen backend (claude-code: e.g. 'sonnet'/'opus'; "
        "codex: CLI default if omitted).",
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=f"LLM backend: {' | '.join(KNOWN_BACKENDS)}. "
        "Defaults to TECHLINGO_LLM_BACKEND or 'claude-code'.",
    ),
    difficulty: Optional[DifficultyLevel] = typer.Option(
        None,
        help="Difficulty of generated questions. Overrides config if set.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--no-verbose",
        help="Print workflow progress events (and agent streaming updates when available).",
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to workflow_config.json. Defaults to workflow_config.json if present, or internal defaults.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help="Manual override for the output course/module title.",
    ),
    course_key: Optional[str] = typer.Option(
        None,
        "--course-key",
        help="Stable import_key for the emitted course (e.g. 'ai-900'). "
        "Falls back to a slug of the title if omitted (explicit key recommended for production imports).",
    ),
) -> None:
    """Run the Techlingo A1–A5 workflow and write JSON artifacts to disk."""
    # Important: load .env BEFORE reading backend env vars. (Typer's envvar= reads too early.)
    env_path = dotenv_path if dotenv_path is not None else Path(".env")
    load_dotenv(env_path, override=False)

    backend_name, model_id = _resolve_backend_and_model(backend, model_id)
    _preflight_or_die(backend_name)

    text = read_input_text(input_text, str(input_file) if input_file else None)

    # Build workflow and state in-process so we can stream progress events.
    from .io import new_run_dir

    workflow = build_techlingo_workflow()
    run_id, run_dir = new_run_dir(out_dir)
    
    # Load config from passed path or default location
    if not config_path:
        default_config = Path("workflow_config.json")
        if default_config.exists():
            config_path = default_config
            
    try:
        loaded_config = load_workflow_config(config_path)
    except ValidationError as e:
        # Surface a clean, actionable message instead of a raw pydantic traceback.
        problems = "\n".join(f"  - {err['msg']}" for err in e.errors())
        raise typer.BadParameter(
            f"Invalid workflow config ({config_path}):\n{problems}"
        )

    # Resolve difficulty: CLI arg > Config > Default(Beginner)
    final_difficulty = difficulty or loaded_config.difficulty

    # Resolve course title: explicit --title > source filename > internal default.
    # The document's filename is the title the author gave it, so use it automatically.
    if not title and input_file is not None:
        title = input_file.stem

    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        input_text=text,
        model_id=model_id,
        difficulty=final_difficulty,
        config=loaded_config,
        override_title=title,
    )

    typer.echo(f"Run started: {run_id}")
    typer.echo(f"Run dir: {run_dir}")
    typer.echo(f"Backend/model: {model_id}")
    typer.echo(f"Difficulty: {final_difficulty.value}")
    if title:
        typer.echo(f"Title Override: {title}")

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

    # Force the resolved title onto the final course so it matches the source
    # document (filename) or explicit --title exactly, regardless of what the LLM
    # produced. (Mirrors how `difficulty` is force-set inside the executors.)
    if title:
        result.course.title = title

    # Write canonical outputs at run root.
    run_dir = Path(result.run_dir)

    # The rich internal model is kept for viewers/debug; the canonical
    # `course.json` is the TechLingo-native output that the importer consumes.
    from .emit import emit_and_validate, slugify
    from .validate_techlingo import TechLingoValidationError

    write_json(run_dir / "course.internal.json", result.course.model_dump(mode="json"))
    write_json(run_dir / "validation_report.json", result.validation_report.model_dump())

    resolved_course_key = course_key or slugify(result.course.title, fallback="course")
    try:
        tl_course = emit_and_validate(result.course, course_key=resolved_course_key)
    except TechLingoValidationError as e:
        typer.echo(f"\nERROR: emitted course is not TechLingo-native:\n{e}")
        raise typer.Exit(code=1)
    write_json(run_dir / "course.json", tl_course.model_dump(mode="json"))
    typer.echo(f"TechLingo course key: {resolved_course_key}")

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



@app.command()
def analyze(
    input_text: Optional[str] = typer.Option(None, help="Raw source text to analyze."),
    input_file: Optional[Path] = typer.Option(None, exists=True, dir_okay=False, help="Path to a text file input."),
    out_dir: Path = typer.Option(Path("outputs"), help="Output directory for run artifacts."),
    dotenv_path: Optional[Path] = typer.Option(None, help="Optional .env path (defaults to .env in repo root)."),
    model_id: Optional[str] = typer.Option(
        None,
        help="Model id for the chosen backend (claude-code: e.g. 'sonnet'/'opus'; "
        "codex: CLI default if omitted).",
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=f"LLM backend: {' | '.join(KNOWN_BACKENDS)}. "
        "Defaults to TECHLINGO_LLM_BACKEND or 'claude-code'.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--no-verbose",
        help="Print workflow progress events (and agent streaming updates when available).",
    ),
) -> None:
    """Run the Text Analysis workflow (Analyzer -> Reviewer)."""
    env_path = dotenv_path if dotenv_path is not None else Path(".env")
    load_dotenv(env_path, override=False)

    backend_name, model_id = _resolve_backend_and_model(backend, model_id)
    _preflight_or_die(backend_name)

    text = read_input_text(input_text, str(input_file) if input_file else None)

    from .io import new_run_dir

    workflow = build_analysis_workflow()
    run_id, run_dir = new_run_dir(out_dir)
    
    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        input_text=text,
        model_id=model_id,
    )

    typer.echo(f"Analysis Run started: {run_id}")
    typer.echo(f"Run dir: {run_dir}")
    typer.echo(f"Backend/model: {model_id}")

    def _get_executor_id(evt: object) -> str | None:
        return (
            getattr(evt, "executor_id", None)
            or getattr(evt, "executorId", None)
            or getattr(evt, "ExecutorId", None)
        )

    async def _run() -> TextAnalysisResult:
        output: TextAnalysisResult | None = None
        started_at: dict[str, float] = {}

        async for evt in workflow.run_stream(state):
            name = evt.__class__.__name__
            executor_id = _get_executor_id(evt)

            # Surface logs
            if name == "StageLogEvent":
                msg = getattr(evt, "message", None)
                ts = time.strftime("%H:%M:%S")
                if msg:
                    typer.echo(f"[{ts}] {msg}")

            # Progress tracking
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

            if verbose:
                 if name not in {
                    "StageLogEvent",
                    "ExecutorInvokedEvent",
                    "ExecutorInvokeEvent",
                    "ExecutorCompletedEvent",
                    "ExecutorCompleteEvent",
                    "ExecutorFailedEvent",
                    "WorkflowOutputEvent",
                    "WorkflowErrorEvent",
                    "WorkflowStartedEvent",
                    "SuperStepStartedEvent",
                    "SuperStepCompletedEvent",
                }:
                    typer.echo(f"[{ts}] EVENT {name}: {evt}")

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
        typer.echo("\nInterrupted (Ctrl+C).")
        raise typer.Exit(code=130)

    typer.echo(f"Analysis complete: {run_id}")
    
    # Save recommended config
    rec_config_path = run_dir / "recommended_workflow_config.json"
    write_json(rec_config_path, result.recommended_config.model_dump(mode="json"))
    typer.echo(f"Recommended config written to: {rec_config_path}")

    typer.echo(f"Outputs: {result.parts[0].content[:50]}..." if result.parts else "No parts found")
    typer.echo(f"Full artifacts in: {run_dir}")


@app.command()
def doctor(
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Only check this backend. Default: check all of "
        f"{', '.join(KNOWN_BACKENDS)}.",
    ),
    ping: bool = typer.Option(
        False,
        "--ping",
        help="Also run one tiny completion per backend to prove end-to-end (uses seat/API quota).",
    ),
    dotenv_path: Optional[Path] = typer.Option(None, help="Optional .env path (defaults to .env in repo root)."),
) -> None:
    """Preflight the LLM backends: binary + version + auth (and optionally a live ping)."""
    env_path = dotenv_path if dotenv_path is not None else Path(".env")
    load_dotenv(env_path, override=False)

    if backend is not None:
        try:
            targets = [resolve_backend_name(backend)]
        except ValueError as e:
            raise typer.BadParameter(str(e))
    else:
        targets = list(KNOWN_BACKENDS)

    any_failed = False
    for name in targets:
        typer.echo(f"\n[{name}]")
        checks = preflight_backend(name)
        backend_ok = all(ok for _, ok, _ in checks)
        for check, ok, detail in checks:
            mark = "OK " if ok else "FAIL"
            typer.echo(f"  {mark} {check}: {detail}")
        if not backend_ok:
            any_failed = True
            continue

        if ping:
            try:
                model_label = resolve_model_label(name, None)
            except ValueError as e:
                typer.echo(f"  SKIP ping: {e}")
                any_failed = True
                continue
            from .llm import LLMClient

            async def _ping() -> dict:
                client = LLMClient(model_id=model_label, name="Doctor_Ping")
                return await client.run_json(
                    'Reply with exactly this JSON object and nothing else: {"ok": true}'
                )
            try:
                t0 = time.monotonic()
                data = asyncio.run(_ping())
                dt = time.monotonic() - t0
                ok = data.get("ok") is True
                typer.echo(f"  {'OK ' if ok else 'FAIL'} ping ({model_label}): {data} ({dt:.1f}s)")
                any_failed = any_failed or not ok
            except Exception as e:  # noqa: BLE001 - report, don't crash the doctor
                typer.echo(f"  FAIL ping ({model_label}): {type(e).__name__}: {e}")
                any_failed = True

    if any_failed:
        raise typer.Exit(code=1)
    typer.echo("\nAll checks passed.")

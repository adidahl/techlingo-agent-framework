"""`course` CLI subcommands — the workspace-centric verbs (ARCHITECTURE.md §11).

    python main.py course init courses/ai-901 --from documents/ai-901 --course-key ai-901
    python main.py course build courses/ai-901 --backend claude-code
    python main.py course compile courses/ai-901
    python main.py course status courses/ai-901

The legacy single-document `run` command stays untouched during Phase 1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from .backends import KNOWN_BACKENDS, preflight_backend, resolve_backend_name, resolve_model_label
from .workspace import Workspace, WorkspaceError, init_workspace

course_app = typer.Typer(no_args_is_help=True, help="Course workspace commands (folder of .md files -> importable course).")


def _load_env(dotenv_path: Optional[Path]) -> None:
    load_dotenv(dotenv_path if dotenv_path is not None else Path(".env"), override=False)


def _resolve_model_or_die(backend: Optional[str], model_id: Optional[str]) -> str:
    try:
        backend_name = resolve_backend_name(backend)
        model_label = resolve_model_label(backend_name, model_id)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    failures = [(check, detail) for check, ok, detail in preflight_backend(backend_name) if not ok]
    if failures:
        problems = "\n".join(f"  - {check}: {detail}" for check, detail in failures)
        raise typer.BadParameter(f"Backend '{backend_name}' failed preflight:\n{problems}")
    return model_label


@course_app.command()
def init(
    course_dir: Path = typer.Argument(..., help="Workspace directory to create (e.g. courses/ai-901)."),
    from_dir: Path = typer.Option(
        ..., "--from", exists=True, file_okay=False, help="Folder containing the source .md/.txt files."
    ),
    course_key: Optional[str] = typer.Option(
        None, "--course-key", help="Stable course import_key. Defaults to the workspace directory name."
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Course title. Defaults to the course key."),
    difficulty: str = typer.Option("beginner", "--difficulty"),
    backend: Optional[str] = typer.Option(
        None, "--backend", help=f"Default build backend for this course: {' | '.join(KNOWN_BACKENDS)}."
    ),
) -> None:
    """Create a course workspace from a folder of source documents."""
    sources = sorted(
        p for p in from_dir.iterdir() if p.is_file() and p.suffix.lower() in Workspace.SOURCE_SUFFIXES
    )
    if not sources:
        raise typer.BadParameter(f"No .md/.txt files found in {from_dir}.")

    resolved_key = course_key or course_dir.name
    try:
        ws = init_workspace(
            course_dir,
            course_id=resolved_key,
            title=title or resolved_key,
            source_files=sources,
            difficulty=difficulty,
            backend=backend,
        )
    except WorkspaceError as e:
        raise typer.BadParameter(str(e))

    typer.echo(f"Workspace created: {ws.root}")
    typer.echo(f"Course key: {resolved_key}")
    typer.echo(f"Sources ({len(sources)}):")
    for src in ws.iter_sources():
        typer.echo(f"  - {src.name}")
    typer.echo(f"\nNext: python main.py course build {course_dir}")


@course_app.command()
def build(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    backend: Optional[str] = typer.Option(
        None, "--backend", help="LLM backend override (default: course.yaml, then env/claude-code)."
    ),
    model_id: Optional[str] = typer.Option(None, "--model-id", help="Model id override for the chosen backend."),
    force: bool = typer.Option(False, "--force", help="Rebuild every source, ignoring hashes."),
    only: list[str] = typer.Option(
        [], "--only", help="Build only these source files (name or stem). Repeatable."
    ),
    lessons: Optional[int] = typer.Option(
        None,
        "--lessons",
        min=1,
        help="Pin lessons per source file (fast test builds: --lessons 1). Normal builds omit this.",
    ),
    dotenv_path: Optional[Path] = typer.Option(None, help="Optional .env path (defaults to .env in repo root)."),
) -> None:
    """Incrementally build the course: run the pipeline on changed sources only."""
    _load_env(dotenv_path)
    from .course_build import build_course  # deferred: imports the LLM stack

    try:
        ws = Workspace(course_dir).require()
    except WorkspaceError as e:
        raise typer.BadParameter(str(e))
    meta = ws.load_meta()

    model_label = _resolve_model_or_die(backend or meta.backend, model_id or meta.model_id)
    typer.echo(f"Course: {meta.id} ({meta.title})")
    typer.echo(f"Backend/model: {model_label}")
    if lessons is not None:
        typer.echo(f"Fast-test mode: {lessons} lesson(s) per source file")

    outcomes = build_course(
        course_dir,
        model_label=model_label,
        force=force,
        only=only or None,
        lessons_override=lessons,
        echo=typer.echo,
    )

    if outcomes:
        typer.echo("\n=== Build summary ===")
        for o in outcomes:
            if o.ok:
                vmark = "ok" if o.validation_ok else "WITH VALIDATION ERRORS"
                typer.echo(
                    f"  OK   {o.source}: {o.lessons} lessons, {o.items} items, "
                    f"concepts +{o.concepts_created}/~{o.concepts_matched}/-{o.concepts_retired}, validation {vmark}"
                )
            else:
                typer.echo(f"  FAIL {o.source}: {o.error}")
        failed = [o for o in outcomes if not o.ok]
        typer.echo(f"\nNext: python main.py course compile {course_dir}")
        if failed:
            typer.echo(f"{len(failed)} source(s) failed — re-run build to retry them.")
            raise typer.Exit(code=1)


@course_app.command()
def compile(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    flat: bool = typer.Option(True, "--flat/--no-flat", help="Also write course.flat.json (today's importer format)."),
) -> None:
    """Compile the workspace into a versioned bundle under dist/ (deterministic, no LLM)."""
    from .compiler import compile_workspace, write_bundle

    try:
        compiled = compile_workspace(course_dir)
    except WorkspaceError as e:
        raise typer.BadParameter(str(e))
    except FileNotFoundError as e:
        raise typer.BadParameter(f"Workspace is missing content (run `course build` first): {e}")

    for key in compiled.skipped_modules:
        typer.echo(f"NOTE: module '{key}' has no bank content yet — skipped.")
    for note in compiled.notes:
        typer.echo(f"NOTE: {note}")

    if compiled.problems:
        typer.echo(f"ERROR: compiled course is not TechLingo-native ({len(compiled.problems)} problem(s)):")
        for p in compiled.problems[:20]:
            typer.echo(f"  - {p}")
        raise typer.Exit(code=1)

    out = write_bundle(course_dir, compiled, flat=flat)
    cfg = compiled.cfg
    unit_counts = compiled.unit_counts
    counts = {
        "modules": len(compiled.tl_course.modules),
        "units": sum(len(m.lessons) for m in compiled.tl_course.modules),
        "questions": sum(len(u.exercises) for m in compiled.tl_course.modules for u in m.lessons),
        "flashcards": sum(len(u.flashcards) for m in compiled.tl_course.modules for u in m.lessons),
        "concepts": len([c for c in compiled.graph.concepts if c.status == "active"]),
    }
    typer.echo(
        f"Bundle v{out.version}: {counts['modules']} modules, {counts['units']} units, "
        f"{counts['questions']} questions, {counts['flashcards']} flashcards, {counts['concepts']} active concepts"
    )
    if cfg.levels > 1:
        level_summary = "  ".join(f"L{n}:{unit_counts.get(f'l{n}', 0)}" for n in range(1, cfg.levels + 1))
        typer.echo(
            f"Levels: {cfg.levels} (seed {cfg.seed})  units per level: {level_summary}  "
            f"checkpoints: {unit_counts.get('checkpoint', 0)}  final review: {unit_counts.get('final_review', 0)}"
        )
    else:
        typer.echo(f"Levels: flat (one unit per lesson; {unit_counts.get('lesson', 0)} lesson units)")
    typer.echo(f"Bundle dir: {out.bundle_dir}")
    if out.flat_path:
        typer.echo(f"Flat course (today's importer): {out.flat_path}")


@course_app.command()
def status(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
) -> None:
    """Show source freshness and workspace content counts."""
    from .course_build import config_hash, plan_build, resolve_build_config

    try:
        ws = Workspace(course_dir).require()
    except WorkspaceError as e:
        raise typer.BadParameter(str(e))

    meta = ws.load_meta()
    state = ws.load_build_state()
    config = resolve_build_config(meta.workflow, meta.difficulty)
    plan = plan_build(ws, state, config_hash(config))

    typer.echo(f"Course: {meta.id} ({meta.title})  difficulty={meta.difficulty}")
    typer.echo("\nSources:")
    dirty_names = {p.name: reason for p, reason in plan.dirty}
    for src in ws.iter_sources():
        prev = state.sources.get(src.name)
        if src.name in dirty_names:
            mark, note = "DIRTY", dirty_names[src.name]
        else:
            mark, note = "clean", f"built {prev.built_at}" if prev else ""
        typer.echo(f"  [{mark}] {src.name}  {note}")

    curriculum = ws.load_curriculum()
    graph = ws.load_graph()
    banks = list(ws.iter_banks())
    items = [it for b in banks for it in b.items if it.status != "retired"]
    by_rung: dict[int, int] = {}
    for it in items:
        by_rung[it.rung] = by_rung.get(it.rung, 0) + 1
    rung_summary = "  ".join(f"R{r}:{by_rung[r]}" for r in sorted(by_rung)) or "-"

    typer.echo(
        f"\nContent: {len(curriculum.modules)} modules, {len(curriculum.lesson_keys())} lessons, "
        f"{len([c for c in graph.concepts if c.status == 'active'])} active concepts "
        f"({len([c for c in graph.concepts if c.status == 'retired'])} retired), {len(items)} active items"
    )
    typer.echo(f"Items by rung: {rung_summary}")
    typer.echo(f"Flashcards: {sum(len(b.flashcards) for b in banks)}")

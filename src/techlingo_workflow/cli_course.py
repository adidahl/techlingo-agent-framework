"""`course` CLI subcommands — the workspace-centric verbs (ARCHITECTURE.md §11).

    python main.py course init courses/ai-901 --from documents/ai-901 --course-key ai-901
    python main.py course build courses/ai-901 --backend claude-code
    python main.py course compile courses/ai-901
    python main.py course status courses/ai-901

The legacy single-document `run` command stays untouched during Phase 1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

import typer
from dotenv import load_dotenv

from .backends import (
    KNOWN_BACKENDS,
    preflight_backend,
    resolve_backend_name,
    resolve_model_label,
    split_backend_label,
)
from .workspace import Workspace, WorkspaceError, init_workspace

course_app = typer.Typer(no_args_is_help=True, help="Course workspace commands (folder of .md files -> importable course).")
reference_app = typer.Typer(
    no_args_is_help=True,
    help="Create, approve, and inspect human-curated reference sessions.",
)
gauntlet_app = typer.Typer(
    no_args_is_help=True,
    help="Run optional qualitative QA over exact compiled learner sessions.",
)
gauntlet_history_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect immutable qualitative Gauntlet history.",
)
gauntlet_proposal_app = typer.Typer(
    no_args_is_help=True,
    help="Review, approve, and incorporate hash-bound Gauntlet proposals.",
)
course_app.add_typer(reference_app, name="reference")
course_app.add_typer(gauntlet_app, name="gauntlet")
gauntlet_app.add_typer(gauntlet_history_app, name="history")
gauntlet_app.add_typer(gauntlet_proposal_app, name="proposal")


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
    if only:
        sources = list(ws.iter_sources())
        selectors = {source.name for source in sources} | {source.stem for source in sources}
        unknown = [value for value in only if value not in selectors]
        if unknown:
            raise typer.BadParameter(
                "Unknown --only source selector(s): "
                + ", ".join(unknown)
                + ". Use an exact source filename or stem."
            )
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
        if failed:
            typer.echo(f"{len(failed)} source(s) failed — re-run build to retry them.")
            raise typer.Exit(code=1)
        typer.echo(f"\nNext: python main.py course compile {course_dir}")

    # A filtered/no-op build must not return success while another current
    # source still has unresolved hard validation.  This keeps shell/CI status
    # authoritative even when `--only` was used.
    from .publication_safety import inspect_publication_readiness

    readiness = inspect_publication_readiness(course_dir)
    if not readiness.ok:
        typer.echo(
            f"ERROR: workspace still has {len(readiness.blockers)} "
            "publication blocker(s):"
        )
        for problem in readiness.blockers[:20]:
            typer.echo(f"  - {problem}")
        if len(readiness.blockers) > 20:
            typer.echo(f"  - ... and {len(readiness.blockers) - 20} more")
        raise typer.Exit(code=1)


@course_app.command()
def compile(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    flat: bool = typer.Option(True, "--flat/--no-flat", help="Also write course.flat.json (today's importer format)."),
) -> None:
    """Compile the workspace into a versioned bundle under dist/ (deterministic, no LLM)."""
    from .compiler import compile_workspace, write_bundle
    from .publication_safety import PublicationSafetyError, inspect_publication_readiness

    try:
        readiness = inspect_publication_readiness(course_dir)
        if not readiness.ok:
            typer.echo(
                f"ERROR: workspace is not publishable ({len(readiness.blockers)} blocking problem(s)):"
            )
            for problem in readiness.blockers[:20]:
                typer.echo(f"  - {problem}")
            if len(readiness.blockers) > 20:
                typer.echo(f"  - ... and {len(readiness.blockers) - 20} more")
            raise typer.Exit(code=1)
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

    try:
        out = write_bundle(course_dir, compiled, flat=flat)
    except PublicationSafetyError as e:
        typer.echo(f"ERROR: {e}")
        raise typer.Exit(code=1)
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


@course_app.command("quality")
def quality(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        help="Optional path for the complete machine-readable JSON audit.",
    ),
) -> None:
    """Audit final in-memory sequence/schema quality without publishing."""

    from .compiler import compile_workspace
    from .workspace import _atomic_write_text

    try:
        compiled = compile_workspace(course_dir)
    except (WorkspaceError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"ERROR: deterministic quality compilation failed: {exc}")
        raise typer.Exit(code=1)

    sequence = compiled.sequence_quality
    sequence_errors = [issue for issue in sequence.issues if issue.severity == "error"]
    warnings = [issue for issue in sequence.issues if issue.severity == "warning"]
    # compile_workspace optionally mirrors sequence errors into `problems`; the
    # remaining entries are exact final TechLingo schema/answer failures.
    schema_errors = [
        problem
        for problem in compiled.problems
        if not problem.startswith("sequence quality [")
    ]
    relaxation_count = sum(
        len(unit.metrics.constraint_relaxations) for unit in sequence.units
    )
    ok = not schema_errors and not sequence_errors
    machine_report = {
        "schema_version": "course-quality-audit-v1",
        "course_id": compiled.tl_course.import_key,
        "ok": ok,
        "schema_errors": schema_errors,
        "sequence": sequence.to_dict(),
    }

    typer.echo(
        f"Course quality: {'PASS' if ok else 'FAIL'}  units={len(sequence.units)}  "
        f"sequence_errors={len(sequence_errors)}  schema_errors={len(schema_errors)}  "
        f"warnings={len(warnings)}  declared_relaxations={relaxation_count}"
    )
    for problem in schema_errors[:20]:
        typer.echo(f"  ERROR schema: {problem}")
    for issue in sequence_errors[:20]:
        typer.echo(f"  ERROR [{issue.code}] {issue.unit_path}: {issue.message}")
    for issue in warnings[:10]:
        marker = "relaxed" if issue.relaxed else "warning"
        typer.echo(f"  {marker.upper()} [{issue.code}] {issue.unit_path}: {issue.message}")
    if len(warnings) > 10:
        typer.echo(f"  ... and {len(warnings) - 10} more warning(s)")

    if output is not None:
        _atomic_write_text(
            output,
            json.dumps(machine_report, ensure_ascii=False, indent=2) + "\n",
        )
        typer.echo(f"Machine report: {output}")
    else:
        typer.echo("Machine report not written (pass --output PATH).")
    if not ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Human-curated qualitative references
# ---------------------------------------------------------------------------


def _compiled_artifacts_or_die(course_dir: Path):
    """Read-only compile used by reference/Gauntlet commands."""

    from .compiler import compile_workspace
    from .gauntlet_io import compiled_unit_artifacts

    try:
        ws = Workspace(course_dir).require()
        compiled = compile_workspace(course_dir)
        artifacts = compiled_unit_artifacts(compiled)
    except (WorkspaceError, FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if compiled.problems:
        preview = "\n".join(f"  - {problem}" for problem in compiled.problems[:20])
        raise typer.BadParameter(
            "compiled learner artifact fails deterministic validation:\n" + preview
        )
    return ws, compiled, artifacts


def _select_gauntlet_units(
    artifacts: dict,
    requested: list[str],
    all_units: bool,
) -> list[str]:
    if all_units and requested:
        raise typer.BadParameter("Use either repeated --unit options or --all, not both.")
    if not all_units and not requested:
        raise typer.BadParameter("Select at least one --unit, or pass --all.")
    selected = list(artifacts) if all_units else list(dict.fromkeys(requested))
    missing = [unit_key for unit_key in selected if unit_key not in artifacts]
    if missing:
        available = ", ".join(artifacts) or "(none)"
        raise typer.BadParameter(
            f"Unknown compiled unit(s): {', '.join(missing)}. Available: {available}"
        )
    return selected


@reference_app.command("draft")
def reference_draft(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    unit: str = typer.Option(..., "--unit", help="Exact compiled unit import_key."),
    reference_id: str = typer.Option(..., "--reference-id", help="Stable candidate reference id."),
    annotation: list[str] = typer.Option(
        [], "--annotation", help="Why this exact session is strong. Repeatable and required."
    ),
    known_weakness: list[str] = typer.Option(
        [], "--known-weakness", help="Known limitation of this candidate. Repeatable."
    ),
    exception: list[str] = typer.Option(
        [], "--exception", help="Intentional rubric exception. Repeatable."
    ),
) -> None:
    """Create a draft reference from one exact, hard-gated compiled unit."""

    from .gauntlet_io import (
        GauntletIOError,
        hard_gate_artifact,
        sequence_policy_from_compile_config,
        source_excerpts_for_artifact,
        write_course_reference,
    )
    from .gauntlet_models import ReferenceContext
    from .references import create_reference_draft

    if not annotation or any(not value.strip() for value in annotation):
        raise typer.BadParameter("At least one non-empty --annotation is required.")
    ws, compiled, artifacts = _compiled_artifacts_or_die(course_dir)
    if unit not in artifacts:
        raise typer.BadParameter(
            f"Unknown compiled unit {unit!r}. Available: {', '.join(artifacts)}"
        )
    artifact = artifacts[unit]
    gate = hard_gate_artifact(
        artifact,
        sequence_policy=sequence_policy_from_compile_config(compiled.cfg),
    )
    if not gate.passed:
        typer.echo("ERROR: reference candidate failed the authoritative hard gate:")
        for issue in gate.issues:
            typer.echo(f"  - [{issue.code}] {issue.path}: {issue.message}")
        raise typer.Exit(code=1)
    try:
        sources = source_excerpts_for_artifact(ws, artifact)
    except GauntletIOError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not sources:
        raise typer.BadParameter(
            f"No source excerpt could be resolved for compiled unit {unit!r}."
        )
    module_key = str(artifact.metadata.get("module_key") or "")
    module_title = next(
        (module.title for module in compiled.tl_course.modules if module.import_key == module_key),
        None,
    )
    draft = create_reference_draft(
        reference_id=reference_id,
        context=ReferenceContext(
            course_id=artifact.course_id,
            course_title=compiled.tl_course.title,
            module_id=module_key or None,
            module_title=module_title,
            lesson_id=artifact.session_id,
            lesson_title=str(artifact.metadata.get("unit_title") or artifact.session_id),
        ),
        relevant_sources=sources,
        artifact=artifact,
        annotations=[value.strip() for value in annotation],
        known_weaknesses=[value.strip() for value in known_weakness if value.strip()],
        exceptions=[value.strip() for value in exception if value.strip()],
    )
    try:
        path = write_course_reference(course_dir, draft)
    except GauntletIOError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Draft reference written: {path}")
    typer.echo("Status: draft (not human-approved and never treated as approved).")


@reference_app.command("promote")
def reference_promote(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    reference_id: str = typer.Argument(..., help="Draft reference id."),
    approved_by: str = typer.Option(
        ..., "--approved-by", help="Human reviewer identity; required for promotion."
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Optional approval note."),
) -> None:
    """Promote a reviewed draft to a separately persisted approved reference."""

    from .gauntlet_io import GauntletIOError, promote_course_reference

    try:
        draft_path, approved_path, _approved = promote_course_reference(
            course_dir,
            reference_id,
            approved_by=approved_by,
            note=note,
        )
    except GauntletIOError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Approved reference written: {approved_path}")
    typer.echo(f"Reviewed draft retained: {draft_path}")


@reference_app.command("list")
def reference_list(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
) -> None:
    """List draft and approved reference sessions."""

    from .gauntlet_io import GauntletIOError, iter_course_references

    try:
        entries = list(iter_course_references(course_dir))
    except (WorkspaceError, GauntletIOError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not entries:
        typer.echo("No reference sessions found.")
        return
    for path, reference in entries:
        lesson = reference.context.lesson_id or "course-wide"
        typer.echo(
            f"{reference.status.value:8}  {reference.reference_id}  "
            f"course={reference.context.course_id}  unit={lesson}  path={path}"
        )


# ---------------------------------------------------------------------------
# Optional live qualitative Gauntlet
# ---------------------------------------------------------------------------


def _gauntlet_model_or_die(settings, meta, backend: Optional[str], model_id: Optional[str]) -> str:
    explicit_backend = backend or settings.critic_backend
    configured_backend = explicit_backend or meta.backend
    configured_model = model_id
    if configured_model is None and settings.critic_model:
        try:
            label_backend, label_model = split_backend_label(settings.critic_model)
        except ValueError:
            configured_model = settings.critic_model
        else:
            if explicit_backend and resolve_backend_name(explicit_backend) != label_backend:
                raise typer.BadParameter(
                    "critic_backend conflicts with the backend-qualified critic_model"
                )
            return _resolve_model_or_die(label_backend, label_model)
    if configured_model is None:
        configured_model = meta.model_id
    return _resolve_model_or_die(configured_backend, configured_model)


@gauntlet_app.command("run")
def gauntlet_run(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    unit: list[str] = typer.Option(
        [], "--unit", help="Compiled unit import_key to evaluate. Repeatable."
    ),
    all_units: bool = typer.Option(False, "--all", help="Evaluate every compiled unit."),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Required opt-in for live subscription-CLI model calls.",
    ),
    backend: Optional[str] = typer.Option(
        None, "--backend", help=f"Critic backend override: {' | '.join(KNOWN_BACKENDS)}."
    ),
    model_id: Optional[str] = typer.Option(
        None, "--model-id", help="Critic/editor/comparator model id override."
    ),
    dotenv_path: Optional[Path] = typer.Option(
        None, help="Optional .env path (defaults to .env in repo root)."
    ),
) -> None:
    """Run bounded qualitative QA; persist records without modifying banks or bundles."""

    from .gauntlet import (
        BlindComparator,
        DEFAULT_GAUNTLET_GOAL,
        GauntletConfig,
        IndependentCritic,
        QualitativeGauntlet,
        TargetedEditor,
    )
    from .gauntlet_backends import (
        FreshCLIComparisonBackend,
        FreshCLICriticBackend,
        FreshCLIEditorBackend,
    )
    from .gauntlet_io import (
        GauntletIOError,
        hard_gate_artifact,
        iter_course_references,
        sequence_policy_from_compile_config,
        source_excerpts_for_artifact,
        write_gauntlet_record,
    )
    from .gauntlet_models import ReferenceStatus
    from .publication_safety import hash_data

    ws, compiled, artifacts = _compiled_artifacts_or_die(course_dir)
    selected = _select_gauntlet_units(artifacts, unit, all_units)
    typer.echo(f"Compiled units selected ({len(selected)}): {', '.join(selected)}")
    if not execute:
        typer.echo("Dry run only: no model calls or history writes were made.")
        typer.echo("Re-run with --execute to opt in to live subscription-CLI calls.")
        return

    _load_env(dotenv_path)
    meta = ws.load_meta()
    model_label = _gauntlet_model_or_die(compiled.cfg.gauntlet, meta, backend, model_id)
    backend_name, _model = split_backend_label(model_label)
    config_values = compiled.cfg.gauntlet.model_dump(mode="python")
    config_values.update(
        {"critic_backend": backend_name, "critic_model": model_label}
    )
    try:
        config = GauntletConfig.from_mapping(config_values)
        approved = [
            reference
            for _path, reference in iter_course_references(
                course_dir, status=ReferenceStatus.approved
            )
            if reference.context.course_id == compiled.tl_course.import_key
        ]
    except (GauntletIOError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    critic_backend = FreshCLICriticBackend(model_label)
    editor_backend = FreshCLIEditorBackend(model_label)
    comparison_backend = FreshCLIComparisonBackend(model_label)
    sequence_policy = sequence_policy_from_compile_config(compiled.cfg)
    compiled_hash = hash_data(compiled.tl_course)
    any_blocked = False
    for unit_key in selected:
        artifact = artifacts[unit_key]
        try:
            sources = source_excerpts_for_artifact(ws, artifact)
        except GauntletIOError as exc:
            typer.echo(f"ERROR {unit_key}: {exc}")
            any_blocked = True
            continue
        if not sources:
            typer.echo(f"ERROR {unit_key}: no relevant source material could be resolved.")
            any_blocked = True
            continue
        run_id = f"{unit_key}-{uuid4().hex[:12]}"
        runner = QualitativeGauntlet(
            config=config,
            critic=IndependentCritic(
                critic_backend, builder_model=config.builder_model
            ),
            editor=TargetedEditor(editor_backend),
            comparator=BlindComparator(comparison_backend, config),
            hard_gate=lambda candidate, policy=sequence_policy: hard_gate_artifact(
                candidate, sequence_policy=policy
            ),
        )
        try:
            outcome = asyncio.run(
                runner.run(
                    run_id=run_id,
                    champion=artifact,
                    goal=DEFAULT_GAUNTLET_GOAL,
                    source_material=sources,
                    references=approved,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one live model failure must not abort a batch
            detail = str(exc).strip().splitlines()[-1] if str(exc).strip() else repr(exc)
            typer.echo(
                f"ERROR {unit_key}: qualitative run failed without publication evidence: "
                f"{type(exc).__name__}: {detail[:500]}"
            )
            any_blocked = True
            continue
        try:
            path = write_gauntlet_record(
                course_dir,
                compiled_artifact_sha256=compiled_hash,
                unit_key=unit_key,
                outcome=outcome,
            )
        except GauntletIOError as exc:
            raise typer.BadParameter(str(exc)) from exc
        changed = outcome.champion.content_hash() != artifact.content_hash()
        typer.echo(
            f"{unit_key}: stop={outcome.history.stop_reason.value}  "
            f"eligible={outcome.history.publication_eligible}  history={path}"
        )
        if changed:
            typer.echo(
                "  NOTE: the record contains an edited champion proposal; canonical banks and "
                "compiled bundles were not changed. Apply and recompile through an explicit workflow."
            )
        if (
            not outcome.history.publication_eligible
            or outcome.history.human_review_recommended
        ):
            any_blocked = True
    if any_blocked:
        raise typer.Exit(code=1)


@gauntlet_history_app.command("list")
def gauntlet_history_list(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    unit: Optional[str] = typer.Option(None, "--unit", help="Filter by unit import_key."),
) -> None:
    """List immutable Gauntlet records, failing loudly on malformed history."""

    from .gauntlet_io import GauntletIOError, iter_gauntlet_records

    try:
        entries = list(iter_gauntlet_records(course_dir, strict=True))
    except (WorkspaceError, GauntletIOError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if unit is not None:
        entries = [(path, record) for path, record in entries if record.unit_key == unit]
    if not entries:
        typer.echo("No Gauntlet history records found.")
        return
    for path, record in entries:
        history = record.history
        typer.echo(
            f"{history.run_id}  unit={record.unit_key}  stop={history.stop_reason.value}  "
            f"eligible={history.publication_eligible}  rounds={len(history.rounds)}  path={path}"
        )


@gauntlet_history_app.command("show")
def gauntlet_history_show(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    identifier: str = typer.Argument(..., help="Run id, record filename, or record stem."),
    as_json: bool = typer.Option(False, "--json", help="Print the complete machine-readable record."),
) -> None:
    """Show one exact Gauntlet record."""

    from .gauntlet_io import GauntletIOError, iter_gauntlet_records

    try:
        entries = list(iter_gauntlet_records(course_dir, strict=True))
    except (WorkspaceError, GauntletIOError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    matches = [
        (path, record)
        for path, record in entries
        if identifier
        in {record.history.run_id, path.name, path.stem}
    ]
    if not matches:
        raise typer.BadParameter(f"No Gauntlet history record matches {identifier!r}.")
    if len(matches) > 1:
        raise typer.BadParameter(
            f"Identifier {identifier!r} is ambiguous; use the exact record filename."
        )
    path, record = matches[0]
    if as_json:
        typer.echo(record.model_dump_json(indent=2))
        return
    history = record.history
    typer.echo(f"Record: {path}")
    typer.echo(f"Run: {history.run_id}")
    typer.echo(f"Unit: {record.unit_key}")
    typer.echo(f"Compiled artifact: {record.compiled_artifact_sha256}")
    typer.echo(f"Champion: {history.final_champion_hash}")
    typer.echo(f"Stop: {history.stop_reason.value} — {history.stop_evidence}")
    typer.echo(f"Publication eligible: {history.publication_eligible}")
    typer.echo(f"Human review recommended: {history.human_review_recommended}")
    if history.human_decision is not None:
        typer.echo(
            f"Human decision: {history.human_decision.action.value} by "
            f"{history.human_decision.reviewer}"
        )
    if history.evaluation_context is not None:
        context = history.evaluation_context
        typer.echo(f"Evaluation context: {context.context_sha256}")
        typer.echo(f"Gauntlet policy: {context.gauntlet_policy_sha256}")
        typer.echo(
            "Models: "
            f"critic={context.models.critic_model}, "
            f"editor={context.models.editor_model}, "
            f"comparator={context.models.comparator_model}"
        )
    typer.echo(
        f"Usage: calls={history.total_usage.backend_calls}, "
        f"tokens={history.total_usage.total_tokens}, cost_usd={history.total_usage.cost_usd:.6f}"
    )
    typer.echo(f"Rounds: {len(history.rounds)}")


# ---------------------------------------------------------------------------
# Reviewed Gauntlet proposal -> authoritative rebuild workflow
# ---------------------------------------------------------------------------


@gauntlet_proposal_app.command("list")
def gauntlet_proposal_list(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    as_json: bool = typer.Option(False, "--json", help="Print complete exact proposal JSON."),
) -> None:
    """List only reconstructable promoted content edits for the current artifact."""

    from .gauntlet_proposals import GauntletProposalError, list_authored_proposals

    try:
        proposals = list_authored_proposals(course_dir)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in proposals], indent=2))
        return
    if not proposals:
        typer.echo("No current promoted authored proposals found.")
        return
    for proposal in proposals:
        evidence = proposal.evidence
        typer.echo(
            f"{proposal.proposal_id}  unit={proposal.unit_key}  "
            f"proposal={proposal.proposal_sha256}"
        )
        typer.echo(
            f"  history={proposal.history_sha256}  "
            f"compiled={proposal.compiled_artifact_sha256}  "
            f"challenger={proposal.champion_after_sha256}"
        )
        typer.echo(
            "  evidence: "
            f"hard_gate={evidence.hard_gate.passed}  "
            f"source_fidelity={evidence.source_fidelity_gate.passed}  "
            f"comparison={evidence.comparison.decision.value}/"
            f"stable={evidence.comparison.stable}  "
            f"positions={len(evidence.comparison.records)}"
        )
        for change in proposal.emitted_changes:
            typer.echo(
                f"  {change.item_key} [{change.field}]\n"
                f"    before={json.dumps(change.before, ensure_ascii=False, sort_keys=True)}\n"
                f"    after ={json.dumps(change.after, ensure_ascii=False, sort_keys=True)}"
            )


@gauntlet_proposal_app.command("queue")
def gauntlet_proposal_queue(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    as_json: bool = typer.Option(False, "--json", help="Print complete queue JSON."),
) -> None:
    """List unsafe/unbounded authored-rebuild findings separately from proposals."""

    from .gauntlet_proposals import GauntletProposalError, list_authored_rebuild_queue

    try:
        entries = list_authored_rebuild_queue(course_dir)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in entries], indent=2))
        return
    if not entries:
        typer.echo("No unsafe/unbounded authored-rebuild findings found.")
        return
    for entry in entries:
        typer.echo(
            f"{entry.queue_id}  unit={entry.unit_key}  reason={entry.reason.value}\n"
            f"  {entry.summary}\n"
            f"  history={entry.history_sha256}  compiled={entry.compiled_artifact_sha256}"
        )


@gauntlet_proposal_app.command("export")
def gauntlet_proposal_export(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    identifier: str = typer.Argument(..., help="Exact proposal id or unambiguous run id."),
    output: Path = typer.Option(..., "--output", help="New human-review JSON artifact path."),
) -> None:
    """Export an exact review artifact without changing canonical banks."""

    from .gauntlet_proposals import (
        GauntletProposalError,
        find_authored_proposal,
        write_authored_proposal,
    )

    try:
        proposal = find_authored_proposal(course_dir, identifier)
        path = write_authored_proposal(proposal, output)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Review artifact: {path}")
    typer.echo(f"Proposal SHA-256: {proposal.proposal_sha256}")
    typer.echo(f"History SHA-256: {proposal.history_sha256}")
    typer.echo(f"Compiled artifact SHA-256: {proposal.compiled_artifact_sha256}")
    typer.echo(f"Proposed champion SHA-256: {proposal.champion_after_sha256}")
    typer.echo("Canonical banks were not changed.")


@gauntlet_proposal_app.command("review")
def gauntlet_proposal_review(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    identifier: str = typer.Argument(..., help="Exact proposal id or unambiguous run id."),
    output: Path = typer.Option(..., "--output", help="New human-readable HTML review page."),
) -> None:
    """Create a side-by-side question review page without changing banks."""

    from .gauntlet_proposals import (
        GauntletProposalError,
        find_authored_proposal,
        write_authored_proposal_review,
    )

    try:
        proposal = find_authored_proposal(course_dir, identifier)
        path = write_authored_proposal_review(proposal, output)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Human review page: {path}")
    typer.echo("Open it in a browser. Nothing was applied and no model was called.")


@gauntlet_proposal_app.command("approve")
def gauntlet_proposal_approve(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    proposal_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    approved_by: str = typer.Option(..., "--approved-by", help="Named human reviewer."),
    proposal_sha256: str = typer.Option(..., "--proposal-sha256"),
    history_sha256: str = typer.Option(..., "--history-sha256"),
    compiled_artifact_sha256: str = typer.Option(..., "--compiled-artifact-sha256"),
    champion_after_sha256: str = typer.Option(..., "--champion-after-sha256"),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        help="New approval path; defaults to gauntlet/proposals/approved/.",
    ),
) -> None:
    """Approve only the explicitly repeated exact proposal/history/artifact hashes."""

    from .gauntlet_proposals import (
        GauntletProposalError,
        approve_authored_proposal,
        load_authored_proposal,
        verify_approval_against_workspace,
        write_proposal_approval,
    )

    try:
        proposal = load_authored_proposal(proposal_path)
        approval = approve_authored_proposal(
            proposal,
            approved_by=approved_by,
            exact_proposal_sha256=proposal_sha256,
            exact_history_sha256=history_sha256,
            exact_compiled_artifact_sha256=compiled_artifact_sha256,
            exact_champion_after_sha256=champion_after_sha256,
        )
        verify_approval_against_workspace(course_dir, approval)
        path = write_proposal_approval(course_dir, approval, output)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Approval artifact: {path}")
    typer.echo(f"Approval SHA-256: {approval.approval_sha256}")
    typer.echo("Canonical banks were not changed; incorporation remains a separate explicit step.")


def _run_ai901_deterministic_audit(course_dir: Path) -> dict:
    """Run the repository's read-only AI-901 audit without writing a bundle."""

    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "ai901_sequence_audit.py"
    spec = importlib.util.spec_from_file_location("techlingo_ai901_sequence_audit", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load deterministic audit: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.audit_course(course_dir)
    if not report["compiled"]["ok"]:
        raise RuntimeError("deterministic AI-901 audit failed")
    return report


@gauntlet_proposal_app.command("incorporate")
def gauntlet_proposal_incorporate(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    approval_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Required opt-in for the one approved bank edit and deterministic validation.",
    ),
    fresh_gauntlet: bool = typer.Option(
        False,
        "--fresh-gauntlet",
        help="After deterministic validation, explicitly run Luna on only the affected unit.",
    ),
    dotenv_path: Optional[Path] = typer.Option(
        None, help="Optional .env path (defaults to .env in repo root)."
    ),
) -> None:
    """Apply one approved bank edit; never regenerate the course or source."""

    from .gauntlet_proposals import (
        GauntletProposalError,
        apply_approved_proposal_to_banks,
        load_proposal_approval,
        restore_approved_proposal_banks,
        verify_approval_against_workspace,
    )

    try:
        approval = load_proposal_approval(approval_path)
        proposal = verify_approval_against_workspace(course_dir, approval)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    sources = list(dict.fromkeys(change.source_file for change in proposal.authored_changes))
    typer.echo(
        f"Verified approval {approval.approval_sha256} for {proposal.proposal_id}; "
        f"source(s): {', '.join(sources)}"
    )
    if not execute:
        typer.echo("Dry run only: no bank, build, audit-history, or bundle files were changed.")
        typer.echo("Re-run with --execute to apply only the approved authoritative bank edit.")
        return

    if fresh_gauntlet:
        _load_env(dotenv_path)
        # Fail before the bank edit if the explicitly requested reviewer is unavailable.
        _ws, compiled_before, _artifacts = _compiled_artifacts_or_die(course_dir)
        _gauntlet_model_or_die(
            compiled_before.cfg.gauntlet,
            Workspace(course_dir).require().load_meta(),
            None,
            None,
        )

    try:
        apply_approved_proposal_to_banks(course_dir, approval)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        "Applied the approved payload only to the authoritative bank item(s), marked "
        "human-edited and pinned. No source, generated unit, or bundle was rebuilt."
    )

    try:
        audit = _run_ai901_deterministic_audit(course_dir)
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed on the audit boundary
        try:
            restore_approved_proposal_banks(course_dir, approval)
        except (WorkspaceError, GauntletProposalError) as restore_exc:
            typer.echo(
                f"ERROR: {exc}; automatic restoration also failed: {restore_exc}. "
                "Publication remains blocked."
            )
            raise typer.Exit(code=1)
        typer.echo(
            f"ERROR: {exc}. The exact previous bank item was restored; "
            "the approved change was not retained."
        )
        raise typer.Exit(code=1)
    compiled_report = audit["compiled"]
    typer.echo(
        "Deterministic audit PASS: "
        f"artifact={compiled_report['artifact_sha256']}  "
        f"units={sum(compiled_report['unit_counts'].values())}  "
        f"placements={compiled_report['preservation']['placements']}"
    )

    if fresh_gauntlet:
        # This persists one new immutable history for the affected unit only.
        gauntlet_run(
            course_dir=course_dir,
            unit=[proposal.unit_key],
            all_units=False,
            execute=True,
            backend=None,
            model_id=None,
            dotenv_path=dotenv_path,
        )
    else:
        typer.echo(
            "Fresh Luna review was not run. Use --fresh-gauntlet only when you explicitly "
            "want qualitative review of this one affected unit."
        )


@gauntlet_proposal_app.command("promote")
def gauntlet_proposal_promote(
    course_dir: Path = typer.Argument(..., help="Course workspace directory."),
    approval_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    amendment_path: Path = typer.Option(..., "--amendment", exists=True, dir_okay=False),
    receipt_path: Path = typer.Option(..., "--receipt", exists=True, dir_okay=False),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Required opt-in to promote the already-applied, validated repair evidence.",
    ),
) -> None:
    """Promote one validated authored repair without rebuilding or compiling a bundle."""

    from .gauntlet_proposals import (
        GauntletProposalError,
        load_proposal_approval,
        promote_validated_authored_repair,
    )

    try:
        approval = load_proposal_approval(approval_path)
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not execute:
        typer.echo(
            "Dry run only: no publication state was changed. Re-run with --execute to "
            "verify and promote the exact approval, amendment, receipt, and bank delta."
        )
        return
    try:
        record = promote_validated_authored_repair(
            course_dir,
            approval,
            amendment_path=amendment_path,
            receipt_path=receipt_path,
        )
    except (WorkspaceError, GauntletProposalError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        "Authored repair promotion PASS: "
        f"item={record.item_key} artifact={record.artifact_sha256}"
    )
    typer.echo("No source, bank, generated unit, Gauntlet history, or bundle was changed.")

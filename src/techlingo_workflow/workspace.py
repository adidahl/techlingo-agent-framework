"""Course workspace: schemas + file IO (ARCHITECTURE.md §3).

A course workspace is a directory under ``courses/<course-id>/`` holding the
canonical, git-versioned content of ONE course:

    course.yaml            course meta + per-course workflow overrides
    compile.yaml           curriculum-compiler config (levels arrive in Phase 2)
    sources/               the input .md files (copied in at `course init`)
    graph/concepts.yaml    concept graph (the atom of the whole platform)
    curriculum.yaml        teaching order: modules -> lessons -> concept ids
    bank/<lesson-key>.json exercise bank per lesson (cell-addressed items)
    authored/              human-authored units/pages — never touched by builds
    build/                 per-source pipeline run artifacts (derived, debug)
    build_state.json       last-built source hashes -> incremental builds
    dist/                  emitted bundles (derived)

Everything in this module is deterministic file IO — no LLM calls. The factory
(course_build.py) writes these files; the compiler (compiler.py) reads them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

import yaml
from pydantic import BaseModel, Field, TypeAdapter

from .models import Exercise

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

WORKSPACE_SCHEMA = "course-workspace-v1"
GRAPH_SCHEMA = "concept-graph-v1"
CURRICULUM_SCHEMA = "curriculum-v1"
BANK_SCHEMA = "exercise-bank-v1"
BUILD_STATE_SCHEMA = "build-state-v1"
COMPILE_SCHEMA = "compile-v1"

Provenance = Literal["generated", "human-edited", "human-authored"]


class CourseMeta(BaseModel):
    """course.yaml — identity + per-course build defaults."""

    schema_version: Literal["course-workspace-v1"] = WORKSPACE_SCHEMA
    id: str = Field(..., description="Stable course import_key (e.g. 'ai-901'). Immutable once published.")
    title: str
    difficulty: str = "beginner"
    locale: str = "en"
    backend: Optional[str] = Field(default=None, description="Default LLM backend for builds of this course.")
    model_id: Optional[str] = Field(default=None, description="Default model id for builds of this course.")
    workflow: dict[str, Any] = Field(
        default_factory=dict,
        description="Overrides merged onto the per-source WorkflowConfig (e.g. exercises_per_lesson).",
    )


class CompileConfig(BaseModel):
    """compile.yaml — deterministic compiler knobs (ARCHITECTURE.md §5).

    Phase 2 defaults: 3 levels per lesson (D5: level = separate unit),
    per-module checkpoints and a course-wide final review. ``levels: 1``
    remains the Phase-1 flat path (one unit per lesson) and stays
    byte-identical for today's importer."""

    schema_version: Literal["compile-v1"] = COMPILE_SCHEMA
    levels: int = Field(default=3, ge=1, le=3)
    recycle: dict[str, float] = Field(default_factory=lambda: {"l2": 0.40, "l3": 0.30})
    session_size_hint: int = 12
    checkpoints: Literal["none", "per_module"] = "per_module"
    final_review: bool = True
    seed: int = 901


class Concept(BaseModel):
    """One node of the concept graph. `id` is immutable once published —
    learner mastery keys on it (ARCHITECTURE.md D10)."""

    id: str
    label: str
    summary: str
    depth: Optional[Literal["fact", "mechanism", "decision"]] = None  # Phase 2 fills this
    confusable_with: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)  # {"file": ..., "heading": ...}
    lessons: list[str] = Field(default_factory=list, description="Lesson keys that teach/reference this concept.")
    prerequisites: list[str] = Field(default_factory=list)
    provenance: Provenance = "generated"
    pinned: bool = False
    status: Literal["active", "retired"] = "active"


class ConceptGraph(BaseModel):
    schema_version: Literal["concept-graph-v1"] = GRAPH_SCHEMA
    concepts: list[Concept] = Field(default_factory=list)

    def by_id(self) -> dict[str, Concept]:
        return {c.id: c for c in self.concepts}


class CurriculumLesson(BaseModel):
    key: str
    title: str
    slo: str = ""
    concepts: list[str] = Field(default_factory=list)


class CurriculumModule(BaseModel):
    key: str
    title: str
    source_file: Optional[str] = Field(
        default=None, description="Source filename this module was generated from (None for authored modules)."
    )
    authored: bool = False
    lessons: list[CurriculumLesson] = Field(default_factory=list)


class Curriculum(BaseModel):
    schema_version: Literal["curriculum-v1"] = CURRICULUM_SCHEMA
    modules: list[CurriculumModule] = Field(default_factory=list)

    def lesson_keys(self) -> list[str]:
        return [l.key for m in self.modules for l in m.lessons]


class BankFlashcard(BaseModel):
    front: str
    back: str
    hint: Optional[str] = None


class BankItem(BaseModel):
    """One exercise in the bank, addressed by (concept, rung, variant).

    `payload` is the untouched internal Exercise model (models.py) as a dict —
    the same shape the A1–A5 pipeline produces and emit.py consumes. Parsing
    back into the typed union goes through `parse_exercise()`.

    NOTE on identity: `item_key` is stable while the item lives; a rebuild of
    the lesson replaces items at the same keys with new content. `payload_hash`
    is what downstream consumers (telemetry, Phase 4) use to detect that "same
    key, different question" and reset per-item stats.
    """

    item_key: str
    concept_id: Optional[str] = None
    rung: int = Field(..., ge=1, le=5)
    variant: int = Field(..., ge=1)
    payload: dict[str, Any]
    payload_hash: str
    provenance: Provenance = "generated"
    pinned: bool = False
    status: Literal["active", "retired", "flagged"] = "active"
    source_hash: Optional[str] = None
    telemetry: dict[str, Any] = Field(default_factory=dict)


class LessonBank(BaseModel):
    schema_version: Literal["exercise-bank-v1"] = BANK_SCHEMA
    lesson: str
    module: str
    items: list[BankItem] = Field(default_factory=list)
    flashcards: list[BankFlashcard] = Field(default_factory=list)


class SourceState(BaseModel):
    sha256: str
    status: Literal["ok", "failed"] = "ok"
    built_at: str = ""
    module_keys: list[str] = Field(default_factory=list)
    validation_ok: Optional[bool] = None


class BuildState(BaseModel):
    schema_version: Literal["build-state-v1"] = BUILD_STATE_SCHEMA
    workflow_config_hash: Optional[str] = None
    sources: dict[str, SourceState] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

_exercise_adapter: TypeAdapter[Exercise] = TypeAdapter(Exercise)


def parse_exercise(payload: dict[str, Any]) -> Exercise:
    """dict -> typed internal Exercise (discriminated by question_type Literal)."""
    return _exercise_adapter.validate_python(payload)


def canonical_json(data: Any) -> str:
    """Stable serialization used for all content hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json(payload))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def natural_sort_key(name: str) -> list[Any]:
    """'10. Foo' sorts after '2. Bar' (numeric-aware), so numbered source files
    keep their authored order."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", name)]


def derive_rung(blooms_level: str, question_type: str) -> int:
    """Deterministic (Bloom, mechanic) -> difficulty rung (ARCHITECTURE.md §3.2).

    The existing Bloom/type coupling (validate.py) guarantees the inputs:
    Applying/Analyzing only occur on choice types, fill_gaps/rearrange only on
    Remembering/Understanding — so this mapping is total and unambiguous.
    """
    if blooms_level == "Applying":
        return 4
    if blooms_level == "Analyzing/Evaluating":
        return 5
    if question_type in ("fill_gaps", "rearrange"):
        return 3  # production mechanic of recall/understanding content
    if blooms_level == "Remembering":
        return 1
    return 2  # Understanding × choice/true_false


def make_item_key(lesson_key: str, concept_id: Optional[str], rung: int, variant: int) -> str:
    concept_part = concept_id or "general"
    return f"{lesson_key}/{concept_part}/r{rung}/v{variant}"


# ---------------------------------------------------------------------------
# Workspace IO
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class WorkspaceError(Exception):
    """Raised for structurally broken/missing workspaces (clean CLI message)."""


class Workspace:
    """Path map + typed load/save for one course workspace directory."""

    SOURCE_SUFFIXES = (".md", ".txt")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # --- paths -------------------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.root / "course.yaml"

    @property
    def compile_path(self) -> Path:
        return self.root / "compile.yaml"

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def graph_path(self) -> Path:
        return self.root / "graph" / "concepts.yaml"

    @property
    def curriculum_path(self) -> Path:
        return self.root / "curriculum.yaml"

    @property
    def bank_dir(self) -> Path:
        return self.root / "bank"

    @property
    def authored_dir(self) -> Path:
        return self.root / "authored"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def build_state_path(self) -> Path:
        return self.root / "build_state.json"

    @property
    def dist_dir(self) -> Path:
        return self.root / "dist"

    def bank_path(self, lesson_key: str) -> Path:
        return self.bank_dir / f"{lesson_key}.json"

    # --- existence ----------------------------------------------------------
    def exists(self) -> bool:
        return self.meta_path.exists()

    def require(self) -> "Workspace":
        if not self.exists():
            raise WorkspaceError(
                f"{self.root} is not a course workspace (missing course.yaml). "
                f"Create one with: python main.py course init {self.root} --from <folder-of-md-files>"
            )
        return self

    # --- sources ------------------------------------------------------------
    def iter_sources(self) -> Iterator[Path]:
        if not self.sources_dir.exists():
            return iter(())
        files = [
            p
            for p in self.sources_dir.iterdir()
            if p.is_file() and p.suffix.lower() in self.SOURCE_SUFFIXES and not p.name.startswith(".")
        ]
        return iter(sorted(files, key=lambda p: natural_sort_key(p.name)))

    def source_hash(self, path: Path) -> str:
        return sha256_text(path.read_text(encoding="utf-8"))

    # --- typed load/save ------------------------------------------------------
    def load_meta(self) -> CourseMeta:
        return CourseMeta.model_validate(_read_yaml(self.meta_path))

    def save_meta(self, meta: CourseMeta) -> None:
        _write_yaml(self.meta_path, meta.model_dump(mode="json"))

    def load_compile_config(self) -> CompileConfig:
        if not self.compile_path.exists():
            return CompileConfig()
        return CompileConfig.model_validate(_read_yaml(self.compile_path))

    def save_compile_config(self, cfg: CompileConfig) -> None:
        _write_yaml(self.compile_path, cfg.model_dump(mode="json"))

    def load_graph(self) -> ConceptGraph:
        if not self.graph_path.exists():
            return ConceptGraph()
        return ConceptGraph.model_validate(_read_yaml(self.graph_path))

    def save_graph(self, graph: ConceptGraph) -> None:
        _write_yaml(self.graph_path, graph.model_dump(mode="json"))

    def load_curriculum(self) -> Curriculum:
        if not self.curriculum_path.exists():
            return Curriculum()
        return Curriculum.model_validate(_read_yaml(self.curriculum_path))

    def save_curriculum(self, curriculum: Curriculum) -> None:
        _write_yaml(self.curriculum_path, curriculum.model_dump(mode="json"))

    def load_bank(self, lesson_key: str) -> LessonBank:
        return LessonBank.model_validate(_read_json(self.bank_path(lesson_key)))

    def save_bank(self, bank: LessonBank) -> None:
        _write_json(self.bank_path(bank.lesson), bank.model_dump(mode="json"))

    def delete_bank(self, lesson_key: str) -> None:
        path = self.bank_path(lesson_key)
        if path.exists():
            path.unlink()

    def iter_banks(self) -> Iterator[LessonBank]:
        if not self.bank_dir.exists():
            return iter(())
        banks = []
        for p in sorted(self.bank_dir.glob("*.json")):
            banks.append(LessonBank.model_validate(_read_json(p)))
        return iter(banks)

    def load_build_state(self) -> BuildState:
        if not self.build_state_path.exists():
            return BuildState()
        return BuildState.model_validate(_read_json(self.build_state_path))

    def save_build_state(self, state: BuildState) -> None:
        _write_json(self.build_state_path, state.model_dump(mode="json"))


def init_workspace(
    root: str | Path,
    *,
    course_id: str,
    title: str,
    source_files: list[Path],
    difficulty: str = "beginner",
    backend: Optional[str] = None,
) -> Workspace:
    """Create a fresh workspace and copy the source files in (sorted naturally).

    Refuses to overwrite an existing workspace — `course build` handles updates.
    """
    ws = Workspace(root)
    if ws.exists():
        raise WorkspaceError(f"{ws.root} already contains a course workspace (course.yaml exists).")

    ws.sources_dir.mkdir(parents=True, exist_ok=True)
    ws.authored_dir.mkdir(parents=True, exist_ok=True)
    (ws.root / "graph").mkdir(parents=True, exist_ok=True)
    ws.bank_dir.mkdir(parents=True, exist_ok=True)

    for src in sorted(source_files, key=lambda p: natural_sort_key(p.name)):
        target = ws.sources_dir / src.name
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    ws.save_meta(
        CourseMeta(id=course_id, title=title, difficulty=difficulty, backend=backend)
    )
    ws.save_compile_config(CompileConfig())
    ws.save_graph(ConceptGraph())
    ws.save_curriculum(Curriculum())
    ws.save_build_state(BuildState())
    return ws

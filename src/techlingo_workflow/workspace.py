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
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .models import Exercise

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

WORKSPACE_SCHEMA = "course-workspace-v1"
GRAPH_SCHEMA = "concept-graph-v1"
CURRICULUM_SCHEMA = "curriculum-v1"
BANK_SCHEMA = "exercise-bank-v1"
BUILD_STATE_SCHEMA = "build-state-v2"
LEGACY_BUILD_STATE_SCHEMA = "build-state-v1"
COMPILE_SCHEMA = "compile-v1"

Provenance = Literal["generated", "human-edited", "human-authored"]

ExperienceConstraint = Literal[
    "mechanics_window",
    "true_false_answer_streak",
    "ui_family_streak",
    "mechanic_streak",
    "concept_adjacency",
]

_QUALITY_DIMENSIONS = (
    "factual_fidelity",
    "answer_unambiguity",
    "distractor_plausibility",
    "misconception_quality",
    "cognitive_progression",
    "mechanic_rhythm_variation",
    "prompt_language_variation",
    "scenario_authenticity",
    "feedback_usefulness",
    "difficulty_appropriateness",
    "terminology_consistency",
    "overall_learner_experience",
)


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


class ExperienceConfig(BaseModel):
    """Shared compiler/runtime learner-experience constraints."""

    model_config = ConfigDict(extra="forbid")

    max_same_mechanic_streak: int = Field(default=2, ge=1)
    max_same_ui_family_streak: int = Field(default=2, ge=1)
    max_same_true_false_answer_streak: int = Field(default=2, ge=1)
    mechanics_window_size: int = Field(default=6, ge=1)
    min_mechanics_per_window: int = Field(default=3, ge=1)
    avoid_adjacent_same_concept: bool = True
    max_search_states: int = Field(default=100_000, ge=1)
    relaxation_order: list[ExperienceConstraint] = Field(
        default_factory=lambda: [
            "mechanics_window",
            "true_false_answer_streak",
            "ui_family_streak",
            "mechanic_streak",
            "concept_adjacency",
        ]
    )

    @model_validator(mode="after")
    def coherent_policy(self) -> "ExperienceConfig":
        if self.min_mechanics_per_window > self.mechanics_window_size:
            raise ValueError(
                "min_mechanics_per_window cannot exceed mechanics_window_size"
            )
        legacy = {
            "mechanics_window",
            "true_false_answer_streak",
            "mechanic_streak",
            "concept_adjacency",
        }
        if (
            len(self.relaxation_order) == len(legacy)
            and set(self.relaxation_order) == legacy
        ):
            # UI family is the coarser/stronger streak constraint.  Insert it
            # immediately before original mechanic so relaxing UI does not
            # prematurely discard useful fine-grained mechanic variation.
            insertion = self.relaxation_order.index("mechanic_streak")
            self.relaxation_order = [
                *self.relaxation_order[:insertion],
                "ui_family_streak",
                *self.relaxation_order[insertion:],
            ]
        expected = {*legacy, "ui_family_streak"}
        if len(self.relaxation_order) != len(expected) or set(self.relaxation_order) != expected:
            raise ValueError(
                "relaxation_order must contain each experience constraint exactly once"
            )
        return self


class SequenceQualityConfig(BaseModel):
    """Final-artifact validator thresholds beyond scheduler constraints."""

    model_config = ConfigDict(extra="forbid")

    max_same_correct_position_streak: int = Field(default=2, ge=1)
    prompt_stem_words: int = Field(default=5, ge=1)
    max_repeated_prompt_stem: int = Field(default=2, ge=1)
    max_downward_rung_jump: int = Field(default=1, ge=0)
    max_same_rung_streak: int = Field(default=6, ge=1)
    block_on_errors: bool = True
    permute_choice_options: bool = True


class GauntletSettings(BaseModel):
    """YAML-facing configuration for the optional qualitative Gauntlet."""

    model_config = ConfigDict(extra="forbid")

    critic_backend: Optional[str] = None
    critic_model: Optional[str] = None
    builder_model: Optional[str] = None
    max_rounds: int = Field(default=4, ge=1)
    plateau_rounds: int = Field(default=2, ge=1)
    repeated_loss_rounds: int = Field(default=2, ge=1)
    minimum_improvement_margin: float = Field(default=0.02, ge=0.0, le=1.0)
    confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    human_review_threshold: float = Field(default=0.40, ge=0.0, le=1.0)
    max_time_seconds: Optional[float] = Field(default=None, ge=0.0)
    max_tokens: Optional[int] = Field(default=None, ge=0)
    max_cost_usd: Optional[float] = Field(default=None, ge=0.0)
    qualitative_required_for_publication: bool = False
    quality_thresholds: dict[str, float] = Field(
        default_factory=lambda: {dimension: 0.80 for dimension in _QUALITY_DIMENSIONS}
    )
    comparison_seed: int = 0
    protected_dimensions: list[str] = Field(
        default_factory=lambda: ["factual_fidelity", "answer_unambiguity"]
    )
    unstable_comparison_requires_human_review: bool = True
    identifying_metadata_keys: list[str] = Field(
        default_factory=lambda: [
            "artifact_id",
            "builder_model",
            "candidate_role",
            "challenger",
            "champion",
            "critic_model",
            "generated_at",
            "item_key",
            "model_id",
        ]
    )

    @model_validator(mode="after")
    def coherent_policy(self) -> "GauntletSettings":
        if (self.critic_backend is None) != (self.critic_model is None):
            raise ValueError("critic_backend and critic_model must be configured together")
        if self.qualitative_required_for_publication and self.critic_backend is None:
            raise ValueError(
                "critic_backend and critic_model are required when qualitative QA gates publication"
            )
        if self.human_review_threshold > self.confidence_threshold:
            raise ValueError(
                "human_review_threshold cannot exceed confidence_threshold"
            )
        expected = set(_QUALITY_DIMENSIONS)
        if set(self.quality_thresholds) != expected:
            missing = sorted(expected - set(self.quality_thresholds))
            extra = sorted(set(self.quality_thresholds) - expected)
            raise ValueError(
                f"quality_thresholds must cover every dimension; missing={missing}, extra={extra}"
            )
        invalid = {
            dimension: threshold
            for dimension, threshold in self.quality_thresholds.items()
            if not 0.0 <= threshold <= 1.0
        }
        if invalid:
            raise ValueError(f"quality thresholds must be between 0 and 1: {invalid}")
        if len(self.protected_dimensions) != len(set(self.protected_dimensions)):
            raise ValueError("protected_dimensions cannot contain duplicates")
        unknown = sorted(set(self.protected_dimensions) - expected)
        if unknown:
            raise ValueError(f"unknown protected quality dimensions: {unknown}")
        return self


class CompileConfig(BaseModel):
    """compile.yaml — deterministic compiler knobs (ARCHITECTURE.md §5).

    Phase 2 defaults: 3 levels per lesson (D5: level = separate unit),
    per-module checkpoints and a course-wide final review. ``levels: 1``
    remains the Phase-1 flat shape (one unit per lesson) and stays schema- and
    answer-compatible with today's importer while presentation order may be
    improved deterministically."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["compile-v1"] = COMPILE_SCHEMA
    levels: int = Field(default=3, ge=1, le=3)
    recycle: dict[str, float] = Field(default_factory=lambda: {"l2": 0.40, "l3": 0.30})
    session_size_hint: int = Field(default=12, ge=1)
    checkpoints: Literal["none", "per_module"] = "per_module"
    final_review: bool = True
    seed: int = 901
    experience: ExperienceConfig = Field(default_factory=ExperienceConfig)
    sequence_quality: SequenceQualityConfig = Field(default_factory=SequenceQualityConfig)
    gauntlet: GauntletSettings = Field(default_factory=GauntletSettings)

    @field_validator("recycle", mode="before")
    @classmethod
    def recycle_values_are_numeric(cls, value: Any) -> Any:
        if isinstance(value, dict):
            boolean_values = sorted(
                str(key) for key, entry in value.items() if isinstance(entry, bool)
            )
            if boolean_values:
                raise ValueError(
                    f"recycle values must be numeric fractions, not booleans: {boolean_values}"
                )
        return value

    @model_validator(mode="after")
    def coherent_compile_policy(self) -> "CompileConfig":
        allowed_recycle_keys = {"l2", "l3"}
        unknown = sorted(set(self.recycle) - allowed_recycle_keys)
        if unknown:
            raise ValueError(f"unknown recycle levels: {unknown}")
        invalid = {
            key: value
            for key, value in self.recycle.items()
            if not math.isfinite(value) or not 0.0 <= value <= 1.0
        }
        if invalid:
            raise ValueError(
                f"recycle fractions must be finite values between 0 and 1: {invalid}"
            )
        return self


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


class SourcePublication(BaseModel):
    """Hashes for the source-local content most recently promoted to LKG."""

    source_sha256: str
    config_sha256: str
    bank_sha256: str
    validation_report_sha256: str
    promoted_at: str
    module_keys: list[str] = Field(default_factory=list)


class SourceState(BaseModel):
    """Latest build attempt for one source.

    The original v1 fields remain intact.  v2 adds hashes which bind the
    attempt to the workflow configuration and validation report, plus an
    explicit last-known-good record.  That separation is important: a failed
    challenger must be visible (and retryable) without pretending it produced
    the banks currently in the workspace.
    """

    sha256: str
    status: Literal["ok", "failed"] = "ok"
    built_at: str = ""
    module_keys: list[str] = Field(default_factory=list)
    validation_ok: Optional[bool] = None
    config_sha256: Optional[str] = None
    validation_report_sha256: Optional[str] = None
    error: Optional[str] = None
    last_known_good: Optional[SourcePublication] = None


class CompilationPublication(BaseModel):
    """Trace for the most recent atomically promoted compiled bundle."""

    source_set_sha256: str
    validation_set_sha256: str
    workflow_config_sha256: str
    compile_config_sha256: str
    bank_sha256: str
    artifact_sha256: str
    # Optional on read so v1/v2 state written before these bindings existed
    # remains diagnosable and can be migrated by a normal rebuild.  Every new
    # publication writes all three values and the publication gate fails
    # closed when it cannot establish them.
    course_meta_sha256: Optional[str] = None
    curriculum_sha256: Optional[str] = None
    concept_graph_sha256: Optional[str] = None
    bundle_version: int
    bundle_path: str
    published_at: str


class BuildState(BaseModel):
    # Accept v1 files in place; newly-created workspaces and subsequently
    # saved build state use v2.  No migration command is required.
    schema_version: Literal["build-state-v1", "build-state-v2"] = BUILD_STATE_SCHEMA
    workflow_config_hash: Optional[str] = None
    bank_sha256: Optional[str] = None
    sources: dict[str, SourceState] = Field(default_factory=dict)
    last_compilation: Optional[CompilationPublication] = None


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
    _atomic_write_text(
        path,
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    """Durably replace one workspace file without exposing a partial write.

    The temporary file lives beside the target so ``os.replace`` stays on one
    filesystem.  This protects build_state and every canonical workspace file
    from truncation on Ctrl-C or process failure during serialization.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


_PUBLICATION_LOCKS_GUARD = threading.Lock()
_PUBLICATION_LOCKS: dict[str, threading.RLock] = {}
_PUBLICATION_LOCK_LOCAL = threading.local()


def _process_lock(handle: Any) -> None:
    """Acquire the platform's advisory exclusive lock for ``handle``."""

    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise WorkspaceError(f"workspace publication locking is unsupported on {os.name!r}")


def _process_unlock(handle: Any) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def workspace_publication_lock(root: str | Path) -> Iterator[None]:
    """Serialize canonical promotion and bundle publication for a workspace.

    The in-process ``RLock`` makes the context safe across threads and
    re-entrant across distinct :class:`Workspace` instances for the same
    resolved root.  ``flock``/``msvcrt`` supplies the corresponding
    cross-process exclusion.  The small lock file is stable workspace state;
    it is never used as publication evidence.
    """

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    key = str(root_path.resolve())
    with _PUBLICATION_LOCKS_GUARD:
        thread_lock = _PUBLICATION_LOCKS.setdefault(key, threading.RLock())

    with thread_lock:
        held = getattr(_PUBLICATION_LOCK_LOCAL, "held", None)
        if held is None:
            held = {}
            _PUBLICATION_LOCK_LOCAL.held = held
        entry = held.get(key)
        if entry is not None:
            entry[0] += 1
            try:
                yield
            finally:
                entry[0] -= 1
            return

        lock_path = root_path / ".techlingo-publication.lock"
        handle = lock_path.open("a+b")
        try:
            _process_lock(handle)
            held[key] = [1, handle]
            try:
                yield
            finally:
                del held[key]
                _process_unlock(handle)
        finally:
            handle.close()


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

    def publication_lock(self) -> Iterator[None]:
        """Return the shared, re-entrant workspace publication lock."""

        return workspace_publication_lock(self.root)

    # --- typed load/save ------------------------------------------------------
    def load_meta(self) -> CourseMeta:
        return CourseMeta.model_validate(_read_yaml(self.meta_path))

    def save_meta(self, meta: CourseMeta) -> None:
        with self.publication_lock():
            _write_yaml(self.meta_path, meta.model_dump(mode="json"))

    def load_compile_config(self) -> CompileConfig:
        if not self.compile_path.exists():
            return CompileConfig()
        return CompileConfig.model_validate(_read_yaml(self.compile_path))

    def save_compile_config(self, cfg: CompileConfig) -> None:
        with self.publication_lock():
            _write_yaml(self.compile_path, cfg.model_dump(mode="json"))

    def load_graph(self) -> ConceptGraph:
        if not self.graph_path.exists():
            return ConceptGraph()
        return ConceptGraph.model_validate(_read_yaml(self.graph_path))

    def save_graph(self, graph: ConceptGraph) -> None:
        with self.publication_lock():
            _write_yaml(self.graph_path, graph.model_dump(mode="json"))

    def load_curriculum(self) -> Curriculum:
        if not self.curriculum_path.exists():
            return Curriculum()
        return Curriculum.model_validate(_read_yaml(self.curriculum_path))

    def save_curriculum(self, curriculum: Curriculum) -> None:
        with self.publication_lock():
            _write_yaml(self.curriculum_path, curriculum.model_dump(mode="json"))

    def load_bank(self, lesson_key: str) -> LessonBank:
        return LessonBank.model_validate(_read_json(self.bank_path(lesson_key)))

    def save_bank(self, bank: LessonBank) -> None:
        with self.publication_lock():
            _write_json(self.bank_path(bank.lesson), bank.model_dump(mode="json"))

    def delete_bank(self, lesson_key: str) -> None:
        with self.publication_lock():
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
        with self.publication_lock():
            state.schema_version = BUILD_STATE_SCHEMA
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

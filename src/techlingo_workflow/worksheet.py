"""Cell worksheets: deterministic per-lesson generation plans (ARCHITECTURE.md §3.3/§4.2).

A worksheet expands each lesson's concept pack into an explicit list of cells
`(concept_id, rung, variant, question_type, blooms_level)` — the exact items A2
must produce. It replaces the per-lesson Bloom/type plan (`_bloom_type_plan`,
RESILIENCE_PLAN §10.1): instead of solving a distribution constraint, the plan
IS the quota table applied per concept depth, so exercise counts and type/Bloom
mixes become *derived* quantities.

Everything here is pure and deterministic — same concepts in, same worksheet
out — which is what lets loop retries regenerate a lesson against an identical
contract, and lets validation reconstruct the expected shape from the lesson's
own concept pack (no config round-trip).

Precedence (documented for WorkflowConfig coherence): when a lesson carries a
concept pack in which EVERY atom has an explicit `depth`, the worksheet derives
`exercises_per_lesson` / `blooms_distribution` / `question_type_distribution`
for that lesson and the configured values are ignored. Lessons without such a
pack (legacy runs, degenerate A1 maps) keep the configured distributions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from .models import ConceptAtom

# ---------------------------------------------------------------------------
# Tunable constants (ARCHITECTURE.md §3.3 quota table + §3.2 rung ladder)
# ---------------------------------------------------------------------------

# Generation quota per concept: depth -> {rung: variant count}.
CELL_QUOTAS: dict[str, dict[int, int]] = {
    "fact": {1: 2, 2: 2, 3: 1},                      # 5 items/concept
    "mechanism": {1: 2, 2: 2, 3: 2, 4: 1},           # 7 items/concept
    "decision": {1: 1, 2: 2, 3: 2, 4: 2, 5: 2},      # 9 items/concept
}

# Depth used when an atom arrives unclassified (A1 stamps this default too, so
# in practice every worksheet lesson has explicit depths end to end).
DEFAULT_DEPTH = "fact"

RUNG_NAMES: dict[int, str] = {1: "Recognize", 2: "Judge", 3: "Produce", 4: "Apply", 5: "Analyze"}

# Variant -> question type rotation per rung (cycled when quota > sequence).
# Rotating types is what makes same-cell variants differ in mechanic, not just
# wording (R1 v1 recognizes via single_choice, v2 via multi_choice, ...).
RUNG_TYPE_SEQUENCES: dict[int, tuple[str, ...]] = {
    1: ("single_choice", "multi_choice"),
    2: ("true_false",),
    3: ("fill_gaps", "rearrange"),
    4: ("single_choice", "multi_choice"),
    5: ("single_choice", "multi_choice"),
}

# INVARIANT (tested): cell_blooms is chosen so that
# derive_rung(cell_blooms(rung, qtype), qtype) == rung for every cell this
# module can emit — the persisted rung and the legacy fallback derivation can
# never disagree for worksheet-generated content.
_RUNG_BLOOMS: dict[int, str] = {
    1: "Remembering",
    2: "Understanding",
    4: "Applying",
    5: "Analyzing/Evaluating",
}


def cell_blooms(rung: int, question_type: str) -> str:
    if rung == 3:
        # Production of recall/understanding content: typing the term is recall,
        # reconstructing the fact/process is comprehension.
        return "Remembering" if question_type == "fill_gaps" else "Understanding"
    return _RUNG_BLOOMS[rung]


@dataclass
class WorksheetCell:
    """One item A2 must produce: the cell address plus its dictated shape.

    `tf_answer` is only set on true_false cells — the course-wide alternating
    answer pattern (§10.1's dictation, moved into the worksheet) lands here so
    each cell carries its own contract.
    """

    concept_id: str
    rung: int
    variant: int
    question_type: str
    blooms_level: str
    tf_answer: Optional[bool] = None


def normalized_depth(depth: Optional[str]) -> str:
    return depth if depth in CELL_QUOTAS else DEFAULT_DEPTH


def required_rungs(depth: Optional[str]) -> tuple[int, ...]:
    """Rungs a concept of this depth must have covered (ladder completeness)."""
    return tuple(sorted(CELL_QUOTAS[normalized_depth(depth)]))


def concept_cells(concept_id: str, depth: Optional[str]) -> list[WorksheetCell]:
    quota = CELL_QUOTAS[normalized_depth(depth)]
    cells: list[WorksheetCell] = []
    for rung in sorted(quota):
        seq = RUNG_TYPE_SEQUENCES[rung]
        for variant in range(1, quota[rung] + 1):
            qtype = seq[(variant - 1) % len(seq)]
            cells.append(
                WorksheetCell(
                    concept_id=concept_id,
                    rung=rung,
                    variant=variant,
                    question_type=qtype,
                    blooms_level=cell_blooms(rung, qtype),
                )
            )
    return cells


def worksheet_applies(concepts: Sequence[ConceptAtom]) -> bool:
    """Worksheet mode is all-or-nothing per lesson: a non-empty concept pack in
    which every atom has an explicit depth. Generation guarantees this (A1
    validates + stamps defaults); anything else — legacy runs, artifacts from
    before Phase 2b — stays on the configured distributions."""
    return bool(concepts) and all(c.depth in CELL_QUOTAS for c in concepts)


def build_lesson_worksheet(concepts: Sequence[ConceptAtom]) -> list[WorksheetCell]:
    """Concept-major expansion (map order), rungs ascending, variants ascending.

    Grouping a cell's variants adjacently is deliberate: the generator sees
    them side by side, which is what makes "these two must differ in surface"
    followable."""
    cells: list[WorksheetCell] = []
    for concept in concepts:
        cells.extend(concept_cells(concept.id, concept.depth))
    return cells


def assign_tf_answers(worksheets: Sequence[list[WorksheetCell]], *, start: bool = False) -> None:
    """Course-wide alternating true_false answers across every worksheet, in
    lesson order (starts False — generators default to all-true, §10.1). All
    depths quota R2×2, so each cell's variant pair lands one false + one true:
    the two statements are forced to genuinely differ."""
    value = start
    for cells in worksheets:
        for cell in cells:
            if cell.question_type == "true_false":
                cell.tf_answer = value
                value = not value


def worksheet_type_distribution(cells: Sequence[WorksheetCell]) -> dict[str, int]:
    return dict(Counter(c.question_type for c in cells))


def worksheet_blooms_distribution(cells: Sequence[WorksheetCell]) -> dict[str, int]:
    return dict(Counter(c.blooms_level for c in cells))


def cell_rung_index(cells: Sequence[WorksheetCell]) -> dict[tuple[str, str, str], int]:
    """(concept_id, question_type, blooms_level) -> rung, for stamping the
    worksheet-assigned rung onto bank items after generation (the exercise
    payload carries type+bloom+concept but not the rung itself)."""
    return {(c.concept_id, c.question_type, c.blooms_level): c.rung for c in cells}


def format_worksheet_rows(cells: Sequence[WorksheetCell]) -> str:
    """The numbered cell plan as it appears inside the A2/A5 prompts."""
    variant_totals = Counter((c.concept_id, c.rung) for c in cells)
    lines: list[str] = []
    for i, c in enumerate(cells, start=1):
        total = variant_totals[(c.concept_id, c.rung)]
        line = (
            f"{i}. concept_id=\"{c.concept_id}\" | question_type={c.question_type} | "
            f"blooms_level={c.blooms_level} | R{c.rung} {RUNG_NAMES[c.rung]} | variant {c.variant} of {total}"
        )
        if c.question_type == "true_false" and c.tf_answer is not None:
            line += f" | correct_answer MUST be {'true' if c.tf_answer else 'false'}"
        lines.append(line)
    return "\n".join(lines)

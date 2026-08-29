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
its Bloom/type distributions from the concept quota table. Courses may opt into
an exact `worksheet_items_per_lesson` budget; that budget is applied here before
generation, never by trimming a generated lesson. Lessons without such a pack
(legacy runs, degenerate A1 maps) keep the configured distributions.
"""

from __future__ import annotations

import hashlib
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


class WorksheetBudgetError(ValueError):
    """The requested exact worksheet size cannot preserve the quota contract."""


def normalized_depth(depth: Optional[str]) -> str:
    return depth if depth in CELL_QUOTAS else DEFAULT_DEPTH


def required_rungs(depth: Optional[str]) -> tuple[int, ...]:
    """Rungs a concept of this depth must have covered (ladder completeness)."""
    return tuple(sorted(CELL_QUOTAS[normalized_depth(depth)]))


def concept_cells(
    concept_id: str,
    depth: Optional[str],
    *,
    type_offset: int = 0,
) -> list[WorksheetCell]:
    """Expand one concept's quota, optionally rotating variant mechanics.

    ``type_offset`` is used by exact-budget lessons to distribute the first
    available mechanic across neighboring concepts.  A two-variant cell still
    contains exactly the same two mechanics; only which one is ``v1`` changes.
    This matters when a tight budget retains the required first variant but
    cannot retain every optional second variant.
    """
    quota = CELL_QUOTAS[normalized_depth(depth)]
    cells: list[WorksheetCell] = []
    for rung in sorted(quota):
        seq = RUNG_TYPE_SEQUENCES[rung]
        for variant in range(1, quota[rung] + 1):
            qtype = seq[(variant - 1 + type_offset) % len(seq)]
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


def worksheet_size_bounds(concepts: Sequence[ConceptAtom]) -> tuple[int, int]:
    """Return the inclusive exact-budget range for a classified concept pack.

    The lower bound keeps one row at every depth-required rung for every
    concept. The upper bound is the complete 5/7/9-row quota expansion.
    """

    minimum = sum(len(required_rungs(concept.depth)) for concept in concepts)
    maximum = sum(len(concept_cells(concept.id, concept.depth)) for concept in concepts)
    return minimum, maximum


# Optional variants are apportioned across the three learner bands, then fairly
# across concepts. Within a band, mechanic-diversifying rows precede another
# T/F or scenario-choice variant. The selected rows are finally filtered back
# into authoritative concept-major order.
_OPTIONAL_BANDS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("foundation", (1, 2)),
    ("practice", (3, 4)),
    ("mastery", (5,)),
)


def _stable_optional_rank(band: str, concept_id: str) -> str:
    return hashlib.sha256(f"{band}\0{concept_id}".encode("utf-8")).hexdigest()


def _allocate_optional_bands(seats: int, capacities: Sequence[int]) -> list[int]:
    """Allocate recyclable-band seats first, alternating foundation/practice.

    Foundation receives an odd remainder because the default next-level recycle
    share is larger there. Mastery variants are useful but cannot serve a later
    lesson level, so they receive seats only after both recyclable bands fill.
    """

    total = sum(capacities)
    if seats < 0 or seats > total:
        raise WorksheetBudgetError(
            f"cannot allocate {seats} optional worksheet rows across capacity {total}"
        )
    allocations = [0 for _ in capacities]

    while seats and any(allocations[index] < capacities[index] for index in (0, 1)):
        for index in (0, 1):
            if seats == 0:
                break
            if allocations[index] < capacities[index]:
                allocations[index] += 1
                seats -= 1

    for index in range(2, len(capacities)):
        take = min(seats, capacities[index])
        allocations[index] += take
        seats -= take
    if seats:  # defensive: the total-capacity check makes this unreachable
        raise WorksheetBudgetError(f"could not allocate {seats} optional worksheet rows")
    return allocations


def build_lesson_worksheet(
    concepts: Sequence[ConceptAtom], *, item_budget: int | None = None
) -> list[WorksheetCell]:
    """Build the authoritative concept-major worksheet.

    With no budget, expand every quota row (map order, rungs ascending,
    variants ascending). With an exact budget, retain variant 1 at every
    required concept/rung and select optional variants round-robin across
    concepts. Selection happens before generation; no emitted exercise is
    discarded or relabeled later.

    Grouping a cell's variants adjacently is deliberate: the generator sees
    them side by side, which is what makes "these two must differ in surface"
    followable.
    """

    full: list[WorksheetCell] = []
    by_concept: list[tuple[int, str, list[WorksheetCell]]] = []
    for concept_index, concept in enumerate(concepts):
        # Exact budgets necessarily keep every v1 row while some v2 rows are
        # omitted.  Alternating the per-concept starting mechanic prevents a
        # level from exposing a single scarce mechanic that no rolling-window
        # ordering can distribute.  Unbudgeted worksheets retain their legacy
        # identity mapping.
        type_offset = concept_index if item_budget is not None else 0
        concept_plan = concept_cells(
            concept.id,
            concept.depth,
            type_offset=type_offset,
        )
        by_concept.append((concept_index, concept.id, concept_plan))
        full.extend(concept_plan)

    if item_budget is None or item_budget == len(full):
        return full

    minimum, maximum = worksheet_size_bounds(concepts)
    if not (minimum <= item_budget <= maximum):
        raise WorksheetBudgetError(
            f"worksheet item budget {item_budget} is infeasible for this concept pack; "
            f"complete rung coverage requires at least {minimum} rows and the full "
            f"quota provides at most {maximum}"
        )

    selected: set[tuple[str, int, int]] = {
        (cell.concept_id, cell.rung, cell.variant)
        for cell in full
        if cell.variant == 1
    }
    remaining = item_budget - len(selected)
    band_candidates: list[list[tuple[int, str, list[WorksheetCell]]]] = []
    for _band, rungs in _OPTIONAL_BANDS:
        candidates: list[tuple[int, str, list[WorksheetCell]]] = []
        rung_order = {rung: index for index, rung in enumerate(rungs)}
        for concept_index, concept_id, concept_plan in by_concept:
            optional = sorted(
                (
                    cell
                    for cell in concept_plan
                    if cell.variant > 1 and cell.rung in rungs
                ),
                key=lambda cell: (cell.variant, rung_order[cell.rung]),
            )
            if optional:
                candidates.append((concept_index, concept_id, optional))
        band_candidates.append(candidates)

    capacities = [
        sum(len(candidates) for _index, _concept_id, candidates in band)
        for band in band_candidates
    ]
    band_allocations = _allocate_optional_bands(remaining, capacities)
    selected_per_concept = {concept_index: 0 for concept_index, _concept_id, _plan in by_concept}

    for (band_name, _rungs), candidates, allocation in zip(
        _OPTIONAL_BANDS, band_candidates, band_allocations
    ):
        queues = {
            concept_index: list(concept_candidates)
            for concept_index, _concept_id, concept_candidates in candidates
        }
        concept_ids = {
            concept_index: concept_id for concept_index, concept_id, _candidates in candidates
        }
        selected_in_band = {concept_index: 0 for concept_index in queues}
        for _ in range(allocation):
            eligible = [concept_index for concept_index, queue in queues.items() if queue]
            if not eligible:  # defensive: Hamilton never allocates beyond band capacity
                raise WorksheetBudgetError(
                    f"worksheet band {band_name!r} exhausted before its allocation was filled"
                )
            concept_index = min(
                eligible,
                key=lambda index: (
                    selected_in_band[index],
                    selected_per_concept[index],
                    _stable_optional_rank(band_name, concept_ids[index]),
                    index,
                ),
            )
            cell = queues[concept_index].pop(0)
            selected.add((cell.concept_id, cell.rung, cell.variant))
            selected_in_band[concept_index] += 1
            selected_per_concept[concept_index] += 1

    return [
        cell
        for cell in full
        if (cell.concept_id, cell.rung, cell.variant) in selected
    ]


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

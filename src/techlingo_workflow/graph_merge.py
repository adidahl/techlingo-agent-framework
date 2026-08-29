"""Concept-graph merge with stable ids (ARCHITECTURE.md §4.1).

Per-source extractions (ConceptAtom lists from the A1 content packs) are merged
into the course-wide ConceptGraph. The invariant that everything downstream
depends on: **a published concept id never changes** — learner mastery and
telemetry key on it. So the merge MATCHES incoming atoms onto existing concepts
(id → normalized label → token similarity) instead of minting fresh ids, and
retires concepts that stop being extracted rather than deleting them.

Deterministic, no LLM. All thresholds are module constants so tests can pin
behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import ConceptAtom
from .workspace import Concept, ConceptGraph

# An incoming atom whose id already exists is treated as the SAME concept when
# its label/summary still resembles the stored one at least this much —
# otherwise the id collision is accidental and the atom gets a suffixed id.
SAME_ID_MIN_SIMILARITY = 0.20
# Cross-id match: distinct ids but effectively the same concept (re-extraction
# under a different slug, or the same concept surfacing in another source file).
CROSS_MATCH_MIN_SIMILARITY = 0.60

_WORD_RE = re.compile(r"[a-z0-9]+")

# Words too generic to signal concept identity on their own.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "that", "this", "it", "as", "by", "be", "can", "its",
}


def norm_label(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _tokens(*texts: str) -> set[str]:
    toks: set[str] = set()
    for t in texts:
        toks.update(w for w in _WORD_RE.findall(t.lower()) if w not in _STOPWORDS and len(w) > 1)
    return toks


def similarity(a_label: str, a_summary: str, b_label: str, b_summary: str) -> float:
    """Token Jaccard over label+summary. Label tokens counted twice so short
    labels ('LLM') aren't drowned out by long summaries."""
    ta = _tokens(a_label, a_label, a_summary)
    tb = _tokens(b_label, b_label, b_summary)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _free_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


@dataclass
class MergeResult:
    graph: ConceptGraph
    # per-source-file map: incoming atom id -> canonical graph id. Everything
    # that referenced the atom (exercise concept_id, confusable_with) must be
    # remapped through this.
    id_remap: dict[str, str] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)   # canonical ids reused
    created: list[str] = field(default_factory=list)   # new ids minted
    retired: list[str] = field(default_factory=list)   # ids retired this merge


def _match_existing(
    atom: ConceptAtom, graph: ConceptGraph, *, already_claimed: set[str]
) -> Concept | None:
    """Find the existing concept this atom is, or None. A concept can absorb at
    most one atom per merge pass (already_claimed) so two distinct incoming
    atoms can't collapse onto the same node."""
    by_id = graph.by_id()

    # 1) Same id, still recognizably the same thing.
    existing = by_id.get(atom.id)
    if existing is not None and existing.id not in already_claimed:
        if norm_label(existing.label) == norm_label(atom.label) or (
            similarity(existing.label, existing.summary, atom.label, atom.summary)
            >= SAME_ID_MIN_SIMILARITY
        ):
            return existing

    # 2) Exact normalized-label match under a different id.
    atom_norm = norm_label(atom.label)
    for c in graph.concepts:
        if c.id in already_claimed:
            continue
        if norm_label(c.label) == atom_norm:
            return c

    # 3) Best similarity above the cross-match threshold.
    best: Concept | None = None
    best_score = 0.0
    for c in graph.concepts:
        if c.id in already_claimed:
            continue
        score = similarity(c.label, c.summary, atom.label, atom.summary)
        if score > best_score:
            best, best_score = c, score
    if best is not None and best_score >= CROSS_MATCH_MIN_SIMILARITY:
        return best
    return None


def merge_source_concepts(
    graph: ConceptGraph,
    atoms_by_lesson: dict[str, list[ConceptAtom]],
    *,
    source_file: str,
    replaced_lesson_keys: set[str] | None = None,
) -> MergeResult:
    """Merge one source file's freshly extracted concepts into the graph.

    ``atoms_by_lesson`` maps the NEW lesson keys of this source's module(s) to
    their content packs. ``replaced_lesson_keys`` are the lesson keys this
    build replaces (the module's previous lessons) — their references are
    stripped first, and concepts owned by this source that are no longer
    extracted get retired.

    Mutates a working copy; returns the new graph + the id remap.
    """
    result = MergeResult(graph=ConceptGraph(concepts=[c.model_copy(deep=True) for c in graph.concepts]))
    working = result.graph
    replaced = replaced_lesson_keys or set()

    # Drop references from the lessons being replaced; fresh merge re-adds them.
    for c in working.concepts:
        if replaced:
            c.lessons = [lk for lk in c.lessons if lk not in replaced]

    claimed: set[str] = set()
    seen_atom_ids: set[str] = set()

    for lesson_key, atoms in atoms_by_lesson.items():
        for atom in atoms:
            if not atom.id or not atom.label:
                continue
            # The same atom id can legitimately appear in two lessons of one
            # pack; the first occurrence wins, later ones just gain the lesson ref.
            if atom.id in seen_atom_ids and atom.id in result.id_remap:
                canonical = working.by_id()[result.id_remap[atom.id]]
                if lesson_key not in canonical.lessons:
                    canonical.lessons.append(lesson_key)
                continue
            seen_atom_ids.add(atom.id)

            existing = _match_existing(atom, working, already_claimed=claimed)
            if existing is not None:
                claimed.add(existing.id)
                result.id_remap[atom.id] = existing.id
                result.matched.append(existing.id)
                existing.status = "active"
                if lesson_key not in existing.lessons:
                    existing.lessons.append(lesson_key)
                # A cross-source match keeps the first owner's wording, but a
                # legacy depthless node must not erase the incoming lesson's
                # authoritative worksheet classification.  Filling a gap is
                # safe across owners; conflicting known depths still retain
                # the first owner's stable graph value.
                if existing.depth is None and atom.depth is not None:
                    existing.depth = atom.depth
                # Refresh content only when this source OWNS the concept and a
                # human hasn't taken it over — cross-file matches keep the
                # first-seen wording (ARCHITECTURE.md §3.5).
                owns = (existing.source or {}).get("file") == source_file
                if owns and existing.provenance == "generated" and not existing.pinned:
                    existing.label = atom.label
                    existing.summary = atom.summary
                    existing.confusable_with = list(atom.confusable_with)
                    if atom.depth is not None:  # never clobber a known depth with a gap
                        existing.depth = atom.depth
            else:
                taken = {c.id for c in working.concepts}
                new_id = _free_id(atom.id, taken)
                claimed.add(new_id)
                result.id_remap[atom.id] = new_id
                result.created.append(new_id)
                working.concepts.append(
                    Concept(
                        id=new_id,
                        label=atom.label,
                        summary=atom.summary,
                        depth=atom.depth,
                        confusable_with=list(atom.confusable_with),
                        source={"file": source_file},
                        lessons=[lesson_key],
                    )
                )

    # Remap confusable_with of everything we just touched/created: the atom
    # pack references sibling ATOM ids, which may have landed on other
    # canonical ids. Unknown references are dropped (they never resolve).
    valid_ids = {c.id for c in working.concepts}
    touched = set(result.id_remap.values())
    for c in working.concepts:
        if c.id not in touched:
            continue
        remapped: list[str] = []
        for ref in c.confusable_with:
            target = result.id_remap.get(ref, ref)
            if target in valid_ids and target != c.id and target not in remapped:
                remapped.append(target)
        c.confusable_with = remapped

    # Retire concepts this source owns that were NOT re-extracted (and are not
    # protected). Retired ids keep their mastery history; they just stop
    # shipping.
    matched_or_created = set(result.id_remap.values())
    for c in working.concepts:
        owns = (c.source or {}).get("file") == source_file
        protected = c.pinned or c.provenance != "generated"
        if owns and not protected and c.id not in matched_or_created and c.status == "active":
            c.status = "retired"
            result.retired.append(c.id)

    return result

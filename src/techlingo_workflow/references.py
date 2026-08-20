"""Human-curated reference-session draft and approval operations.

Approval is intentionally explicit: loading a valid draft never upgrades its
status, and critic callers can select approved references with
``approved_references``.  File writes use a sibling temporary file followed by
``os.replace`` so an interrupted write cannot leave partial JSON.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .gauntlet_models import (
    ArtifactSnapshot,
    DimensionExpectation,
    QualityDimension,
    ReferenceApproval,
    ReferenceContext,
    ReferenceSession,
    ReferenceStatus,
    SourceExcerpt,
)


class ReferenceError(ValueError):
    """A reference lifecycle operation is unsafe or invalid."""


def create_reference_draft(
    *,
    reference_id: str,
    context: ReferenceContext,
    relevant_sources: list[SourceExcerpt],
    artifact: ArtifactSnapshot,
    annotations: list[str],
    expected_dimensions: Mapping[QualityDimension, DimensionExpectation] | None = None,
    known_weaknesses: list[str] | None = None,
    exceptions: list[str] | None = None,
) -> ReferenceSession:
    """Create a clearly marked candidate from an exact final artifact."""

    return ReferenceSession(
        reference_id=reference_id,
        status=ReferenceStatus.draft,
        context=context,
        relevant_sources=relevant_sources,
        final_ordered_questions=[item.model_copy(deep=True) for item in artifact.items],
        annotations=annotations,
        expected_dimensions=dict(expected_dimensions or {}),
        known_weaknesses=list(known_weaknesses or []),
        exceptions=list(exceptions or []),
    )


def promote_reference(
    draft: ReferenceSession,
    *,
    approved_by: str,
    approved_at: datetime | None = None,
    note: str | None = None,
) -> ReferenceSession:
    """Return a human-approved copy tied to the exact reviewed draft hash.

    ``approved_by`` must identify a human reviewer.  The function cannot verify
    organizational identity; callers remain responsible for authorization.
    """

    if draft.status is not ReferenceStatus.draft:
        raise ReferenceError(
            f"only a draft can be promoted; {draft.reference_id!r} is {draft.status.value!r}"
        )
    reviewer = approved_by.strip()
    if not reviewer:
        raise ReferenceError("approved_by must identify the human reviewer")
    timestamp = approved_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ReferenceError("approved_at must be timezone-aware")
    approval = ReferenceApproval(
        approved_by=reviewer,
        approved_at=timestamp,
        draft_content_hash=draft.content_hash(),
        note=note,
    )
    promoted = draft.model_dump(mode="python")
    promoted.update({"status": ReferenceStatus.approved, "approval": approval})
    return ReferenceSession.model_validate(promoted)


def load_reference(path: Path) -> ReferenceSession:
    try:
        return ReferenceSession.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceError(f"could not load reference {path}: {exc}") from exc


def write_reference(
    path: Path,
    reference: ReferenceSession,
    *,
    overwrite: bool = False,
) -> None:
    """Atomically write one validated reference JSON file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ReferenceError(f"reference already exists: {path}")

    text = reference.model_dump_json(indent=2) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        raise ReferenceError(f"could not write reference {path}: {exc}") from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def promote_reference_file(
    draft_path: Path,
    approved_path: Path,
    *,
    approved_by: str,
    approved_at: datetime | None = None,
    note: str | None = None,
    overwrite: bool = False,
) -> ReferenceSession:
    """Load a draft, approve it explicitly, and atomically write a new file."""

    if Path(draft_path).resolve() == Path(approved_path).resolve():
        raise ReferenceError(
            "approved_path must differ from draft_path so the reviewed candidate is retained"
        )
    approved = promote_reference(
        load_reference(Path(draft_path)),
        approved_by=approved_by,
        approved_at=approved_at,
        note=note,
    )
    write_reference(Path(approved_path), approved, overwrite=overwrite)
    return approved


def approved_references(
    references: Iterable[ReferenceSession],
) -> list[ReferenceSession]:
    """Select approved standards without ever treating drafts as approved."""

    return [
        reference.model_copy(deep=True)
        for reference in references
        if reference.status is ReferenceStatus.approved
    ]

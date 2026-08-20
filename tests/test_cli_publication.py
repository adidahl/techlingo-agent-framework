"""Fail-closed CLI behavior at the build/publication boundary."""

from __future__ import annotations

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from techlingo_workflow.cli_course import course_app
from techlingo_workflow.workspace import init_workspace


def test_build_rejects_unknown_only_selector_before_any_model_call() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_dir = root / "source"
        source_dir.mkdir()
        source = source_dir / "Lesson.md"
        source.write_text("# Grounded lesson\n", encoding="utf-8")
        workspace = init_workspace(
            root / "course",
            course_id="selector-test",
            title="Selector Test",
            source_files=[source],
        )

        result = CliRunner().invoke(
            course_app,
            ["build", str(workspace.root), "--only", "does-not-exist"],
        )

        assert result.exit_code != 0
        assert "Unknown --only source selector" in result.output

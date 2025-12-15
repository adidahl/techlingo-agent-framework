"""
TechLingo Agent Framework - Main Entry Point
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python main.py ...` without needing an editable install.
_SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(_SRC))

from techlingo_workflow.cli import app  # noqa: E402


def main() -> None:
    """CLI entrypoint."""
    app()

if __name__ == "__main__":
    main()


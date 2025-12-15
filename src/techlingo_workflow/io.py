from __future__ import annotations

import json
import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_run_dir(base_out_dir: str | Path) -> tuple[str, Path]:
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_id = f"run-{ts}"
    run_dir = ensure_dir(Path(base_out_dir) / run_id)
    ensure_dir(run_dir / "artifacts")
    return run_id, run_dir


def read_input_text(input_text: Optional[str], input_file: Optional[str]) -> str:
    if input_text and input_file:
        raise ValueError("Provide only one of input_text or input_file.")
    if input_text:
        return input_text
    if input_file:
        return Path(input_file).read_text(encoding="utf-8")
    raise ValueError("You must provide either input_text or input_file.")


def write_json(path: str | Path, data: Any) -> None:
    def _default(o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, Path):
            return str(o)
        return str(o)

    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_default),
        encoding="utf-8",
    )


def write_text(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}



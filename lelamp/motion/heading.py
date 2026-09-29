"""Persistent logical base heading; hardware calibration remains immutable."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _state_path(root: Path | None = None) -> Path:
    runtime_root = root or Path(__file__).resolve().parents[2]
    return runtime_root / os.getenv(
        "MOTION_HEADING_STATE_FILE", "runtime_state/motion_heading.json"
    )


def load_heading(root: Path | None = None) -> float:
    try:
        payload = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return float(payload.get("base_heading_degrees", 0.0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0.0


def save_heading(degrees: float, root: Path | None = None) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"base_heading_degrees": float(degrees)}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)

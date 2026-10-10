"""Dependency-free generation limits shared by runtime and offline eval."""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_TASK_MAX_TOKENS: dict[str, int] = {
    "diagnose": 768,
    "solve": 1024,
    "grounded_qa": 512,
    "practice": 1024,
}


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read the small integer subset needed by GPU-only evaluation envs."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def production_task_limits(env_file: str | Path | None = None) -> dict[str, int]:
    """Return the effective production limits without importing pydantic.

    The training environment intentionally has fewer backend dependencies than
    the API environment.  Reading only these four ``CL_*`` values here keeps
    offline report signatures aligned with pydantic-settings: process
    environment first, project ``.env`` second, then code defaults.
    """
    path = (Path(env_file) if env_file is not None
            else Path(__file__).resolve().parents[2] / ".env")
    dotenv = _dotenv_values(path)
    limits: dict[str, int] = {}
    for task, default in DEFAULT_TASK_MAX_TOKENS.items():
        key = f"CL_LLM_{task.upper()}_MAX_TOKENS"
        raw = os.environ.get(key, dotenv.get(key, str(default)))
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if value <= 0:
            raise ValueError(f"{key} must be greater than zero")
        limits[task] = value
    return limits

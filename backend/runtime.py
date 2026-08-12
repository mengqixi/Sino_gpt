from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def module_worker_command(module: str, *arguments: str | Path) -> list[str]:
    values = [str(argument) for argument in arguments]
    if is_frozen():
        return [sys.executable, "--sino-module-worker", module, *values]
    return [sys.executable, "-m", module, *values]


def prewarm_worker_command(feature: str, directory: str | Path) -> list[str]:
    if is_frozen():
        return [
            sys.executable,
            "--sino-prewarm-worker",
            feature,
            str(directory),
        ]
    return [
        sys.executable,
        "-m",
        "backend.services.prewarm_worker",
        feature,
        str(directory),
    ]


def cutout_worker_command(
    worker_path: str | Path,
    model_path: str | Path,
    input_path: str | Path,
    output_path: str | Path,
    *,
    python_executable: str | Path | None = None,
) -> list[str]:
    if is_frozen() and python_executable is None:
        return [
            sys.executable,
            "--sino-cutout-worker",
            str(model_path),
            str(input_path),
            str(output_path),
        ]
    return [
        str(python_executable or sys.executable),
        str(worker_path),
        str(model_path),
        str(input_path),
        str(output_path),
    ]

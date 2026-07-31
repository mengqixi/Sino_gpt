from __future__ import annotations

from typing import Any

from .heavy_worker_io import run_worker


def _handle(payload: dict[str, Any]) -> Any:
    from . import recolor_service as service

    operation = payload["operation"]
    if operation == "analyze":
        return service._analyze_recolor_masks(payload["image_path"])
    if operation == "select":
        return service._select_hardware_region(
            payload["image_path"],
            payload["protect_mask"],
            tuple(payload["box"]),
            payload["action"],
        )
    if operation == "preview":
        return service._preview_recolor(
            payload["image_path"],
            payload["target_color"],
            payload["subject_mask"],
            payload["protect_mask"],
        )
    if operation == "apply":
        return service._apply_recolor(
            payload["image_path"],
            payload["target_color"],
            payload["subject_mask"],
            payload["protect_mask"],
        )
    raise ValueError("Unsupported recolor worker operation")


if __name__ == "__main__":
    run_worker(_handle)

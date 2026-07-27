from __future__ import annotations

from typing import Any

from .heavy_worker_io import run_worker


def _handle(payload: dict[str, Any]) -> Any:
    from . import vip_organizer_service as service

    operation = payload["operation"]
    if operation == "preview":
        return service._render_previews(
            payload["session_id"],
            payload["slots"],
            payload["product_info"],
            payload["platform"],
            payload["target_folder"],
        )
    if operation == "preview_slot":
        return service._render_slot_preview(
            payload["session_id"],
            payload["slots"],
            payload["product_info"],
            payload["file_name"],
            payload["platform"],
            payload["target_folder"],
        )
    if operation == "export":
        return service._export_package(
            payload["session_id"],
            payload["slots"],
            payload["product_info"],
            payload["platform"],
        )
    raise ValueError("Unsupported organizer worker operation")


if __name__ == "__main__":
    run_worker(_handle)

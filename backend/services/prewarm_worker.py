from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable


PREWARM_IDLE_SECONDS = 180


def _feature_handler(feature: str) -> Callable[[dict[str, Any]], Any]:
    if feature == "cutout":
        from .cutout_pipeline_worker import _handle

        return _handle
    if feature == "organizer":
        from . import vip_organizer_service  # noqa: F401
        from .organizer_render_worker import _handle

        return _handle
    if feature == "recolor":
        from . import recolor_service  # noqa: F401
        from .recolor_worker import _handle

        return _handle
    raise ValueError("Unsupported prewarm feature")


def main() -> None:
    feature = sys.argv[1]
    directory = Path(sys.argv[2])
    handler = _feature_handler(feature)
    (directory / "ready").write_text(feature, encoding="utf-8")
    input_path = directory / "input.json"
    output_path = directory / "output.json"
    deadline = time.monotonic() + PREWARM_IDLE_SECONDS
    while not input_path.is_file():
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        response = {"ok": True, "result": handler(payload)}
    except Exception as exc:
        response = {
            "ok": False,
            "error": str(exc) or exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
            "traceback": traceback.format_exc(limit=8),
        }
    temporary = output_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(response, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, output_path)


if __name__ == "__main__":
    main()

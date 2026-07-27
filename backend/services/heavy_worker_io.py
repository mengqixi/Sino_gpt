from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable


def run_worker(handler: Callable[[dict[str, Any]], Any]) -> None:
    input_path, output_path = map(Path, sys.argv[1:3])
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

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .heavy_worker_io import run_worker
from .vip_organizer_service import _predict_product_matte, _prepared_product_cutout


def _handle(payload: dict[str, Any]) -> dict[str, int]:
    source_path = Path(payload["source_path"])
    transparent_path = Path(payload["transparent_path"])
    gray_path = Path(payload["gray_path"])

    with Image.open(source_path) as source_image:
        source = ImageOps.exif_transpose(source_image)
        source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        source = source.convert("RGBA")

    model_matte = _predict_product_matte(source)
    cutout = _prepared_product_cutout(source, model_matte=model_matte)
    transparent = Image.new("RGBA", (800, 800), (255, 255, 255, 0))
    rendered = ImageOps.contain(cutout, (704, 704), Image.Resampling.LANCZOS)
    transparent.alpha_composite(
        rendered,
        (
            (transparent.width - rendered.width) // 2,
            (transparent.height - rendered.height) // 2,
        ),
    )
    transparent.save(transparent_path, format="PNG", optimize=True)
    gray = Image.new("RGB", transparent.size, "#969895")
    gray.paste(transparent.convert("RGB"), (0, 0), transparent.getchannel("A"))
    gray.save(gray_path, format="PNG", optimize=True)
    return {"width": transparent.width, "height": transparent.height}


if __name__ == "__main__":
    run_worker(_handle)

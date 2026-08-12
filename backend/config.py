import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def _configured_data_dir() -> Path | None:
    value = os.environ.get("SINO_GPT_DATA_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


_RUNTIME_DATA_DIR = _configured_data_dir()
DATA_DIR = _RUNTIME_DATA_DIR or PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
UPLOAD_DIR = DATA_DIR / "uploads" if _RUNTIME_DATA_DIR else BASE_DIR / "uploads"
RESULT_DIR = DATA_DIR / "results" if _RUNTIME_DATA_DIR else BASE_DIR / "results"
PROMPT_DIR = BASE_DIR / "prompts"
ASSET_DIR = BASE_DIR / "assets"
PRODUCT_IMAGE_DATA_DIR = DATA_DIR / "product_images"
PRODUCT_IMAGE_INPUT_DIR = PRODUCT_IMAGE_DATA_DIR / "inputs"
PRODUCT_IMAGE_OUTPUT_DIR = PRODUCT_IMAGE_DATA_DIR / "outputs"

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

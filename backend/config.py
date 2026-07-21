from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
UPLOAD_DIR = BASE_DIR / "uploads"
RESULT_DIR = BASE_DIR / "results"
PROMPT_DIR = BASE_DIR / "prompts"
ASSET_DIR = BASE_DIR / "assets"
PRODUCT_IMAGE_DATA_DIR = DATA_DIR / "product_images"
PRODUCT_IMAGE_INPUT_DIR = PRODUCT_IMAGE_DATA_DIR / "inputs"
PRODUCT_IMAGE_OUTPUT_DIR = PRODUCT_IMAGE_DATA_DIR / "outputs"

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

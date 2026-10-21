import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = Path(os.getenv("MILAAN_UPLOAD_DIR", str(BASE_DIR / "uploads")))
DB_PATH = Path(os.getenv("MILAAN_DB_PATH", str(DATA_DIR / "milaan.db")))
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

DISCLAIMER = (
    "Potential discrepancies require independent verification. "
    "MILAAN does not guarantee ITC eligibility or recovery."
)

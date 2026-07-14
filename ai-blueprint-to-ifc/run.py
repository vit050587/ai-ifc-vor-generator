from pathlib import Path
from processor import Processor

BASE_DIR = Path("pages")
PDF_PATH = BASE_DIR / "0" / "1.pdf"

if not PDF_PATH.exists():
    raise FileNotFoundError(f"PDF не найден: {PDF_PATH}")

processor = Processor(PDF_PATH)
result = processor.process()

print(f"Найдено стен: {len(result['walls'])}")

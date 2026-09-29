# Point every test at a throwaway data file before any project module is imported.
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="price-watch-test-"))
os.environ["PW_DATA_FILE"] = str(TMP / "watchlist.json")
os.environ["NTFY_TOPIC"] = "test-topic"
os.environ["PW_BASE_URL"] = "http://localhost:5000"
os.environ["CT_STORE_ID"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

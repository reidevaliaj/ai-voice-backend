# tools/storage.py
import json
import time
from pathlib import Path
from typing import Dict, Any

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

def append_event(filename: str, payload: Dict[str, Any]) -> str:
    path = DATA_DIR / filename
    record = {"ts": int(time.time()), "payload": payload}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)
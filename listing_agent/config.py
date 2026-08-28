from __future__ import annotations

import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def load_searches(path: str | Path = "config/searches.json") -> dict:
    data = json.loads(Path(path).read_text())
    for source in ("ebay", "invaluable"):
        data[source] = [item for item in data.get(source, []) if item.get("enabled", True)]
    return data


def required_env(*names: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return values

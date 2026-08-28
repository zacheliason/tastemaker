from __future__ import annotations

import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def load_searches(path: str | Path = "config/searches.json") -> dict:
    data = json.loads(Path(path).read_text())
    if "sources" not in data or not isinstance(data["sources"], dict):
        raise RuntimeError("Configuration must define a sources object")
    return data


def configured_sources(config: dict, enabled_only: bool = True) -> list[tuple[str, dict]]:
    sources = config.get("sources", {})
    return [(name, settings) for name, settings in sources.items()
            if not enabled_only or settings.get("enabled", True)]


def enabled_searches(settings: dict) -> list[dict]:
    return [item for item in settings.get("searches", []) if item.get("enabled", True)]


def adapter_for(settings: dict):
    import importlib
    adapter = settings.get("adapter")
    if not adapter:
        raise RuntimeError("Enabled source is missing an adapter")
    return importlib.import_module(f"listing_agent.{adapter}")


def required_env(*names: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return values

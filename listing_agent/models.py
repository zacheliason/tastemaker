from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


@dataclass
class Listing:
    source: str
    search_id: str
    external_id: str
    title: str
    price: Decimal | None
    currency: str | None
    url: str
    image_urls: list[str] = field(default_factory=list)
    description: str = ""
    size_fields: dict[str, Any] = field(default_factory=dict)
    raw_data: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    price_usd: Decimal | None = None
    sale_end_at: datetime | None = None

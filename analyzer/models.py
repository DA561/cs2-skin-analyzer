from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Item(StrictModel):
    name: str = Field(min_length=1, max_length=240)
    market_hash_name: str | None = Field(default=None, max_length=240)
    wear: str | None = Field(default=None, max_length=50)
    version: Literal["normal", "stattrak", "souvenir", "unknown"] = "unknown"
    pattern: str | None = Field(default=None, max_length=80)
    rank: int | None = Field(default=None, ge=1, le=1000)
    screenshot_price: float | None = Field(default=None, gt=0)
    confidence: float = Field(default=1, ge=0, le=1)
    needs_review: bool = False


class ItemsRequest(StrictModel):
    items: list[Item] = Field(min_length=1, max_length=30)
    expected_count: int = Field(default=30, ge=1, le=30)
    origin: Literal["item_list", "chatgpt_vision", "api_vision"] = "item_list"

    @model_validator(mode="after")
    def count(self):
        if len(self.items) > self.expected_count:
            raise ValueError("expected_count must include all submitted items")
        return self


class Observation(StrictModel):
    value: float
    unit: str
    source: str
    endpoint: str
    field: str
    queried_at: datetime
    observed_at: datetime | None = None
    basis: str


class Point(StrictModel):
    at: datetime
    value: float = Field(ge=0)


class Series(StrictModel):
    points: list[Point]
    unit: str
    source: str
    endpoint: str
    queried_at: datetime
    basis: str


class Snapshot(StrictModel):
    fields: dict[str, Observation] = Field(default_factory=dict)
    series: dict[str, Series] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)

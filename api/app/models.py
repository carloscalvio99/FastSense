"""Contratos Pydantic de la API (OpenAPI 3.0)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

EventType = Literal["click", "cart", "purchase", "rating"]


class RecommendationItem(BaseModel):
    product_id: str
    name: str
    category: str
    price: float
    score: float
    rank: int


class RecommendationResponse(BaseModel):
    user_id: str
    items: list[RecommendationItem]
    source: Literal["cache", "svd", "fallback"]
    request_id: str
    latency_ms: float


class TrackInteractionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    product_id: str = Field(min_length=1, max_length=64)
    event_type: EventType
    rating: Optional[float] = Field(default=None, ge=1.0, le=5.0)
    session_id: Optional[str] = Field(default=None, max_length=64)
    timestamp: Optional[datetime] = None
    # Campo opcional de demostración: se hashea antes de salir del proceso.
    email: Optional[str] = Field(default=None, max_length=320)

    @field_validator("user_id", "product_id")
    @classmethod
    def _strip_ids(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("identificador vacío")
        return cleaned

    @model_validator(mode="after")
    def _rating_required_for_rating(self) -> "TrackInteractionRequest":
        if self.event_type == "rating" and self.rating is None:
            raise ValueError("event_type=rating requiere el campo rating (1.0-5.0)")
        return self


class TrackInteractionResponse(BaseModel):
    event_id: str
    status: Literal["accepted"]
    accepted_at: datetime


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    uptime_s: float
    service: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    redis: Literal["ok", "error"]
    rds: Literal["ok", "error"]
    model_loaded: bool
    model_version: str
    uptime_s: float

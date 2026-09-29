"""Schema definitions for Azure-to-AWS mapping knowledge base rules."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuleExample(BaseModel):
    """Concrete source-to-target mapping example for one rule."""

    model_config = ConfigDict(extra="forbid")

    source: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class KnowledgeBaseRule(BaseModel):
    """Normalized rule entry consumed by mapping and validation stages."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=3)
    source_service: str = Field(min_length=1)
    source_construct: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    target_construct: str = Field(min_length=1)
    property_map: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    examples: list[RuleExample] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    last_verified: date

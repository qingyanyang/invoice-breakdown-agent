"""Shared task and revision command data."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


GSTBasis = Literal["excluding_gst", "including_gst", "unknown"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    gst_basis: GSTBasis


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create"] = "create"
    target: Target | None = None


class UpdateTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    target: Target | None


class SourceDetails(BaseModel):
    filename: str
    size: int
    sha256: str


class TaskDetails(BaseModel):
    id: UUID
    operation: Literal["create"]
    status: Literal["awaiting_source", "source_uploaded"]
    revision: int
    target: Target | None
    source: SourceDetails | None

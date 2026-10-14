"""Pydantic schemas for the structured outputs requested from the claude CLI."""

from __future__ import annotations

from pydantic import BaseModel, Field

from leadgen.models import Department, Seniority


class TitleItem(BaseModel):
    raw_title: str
    department: Department
    seniority: Seniority
    canonical_title: str = Field(description="Short canonical English job title, e.g. 'Software Engineer'")


class TitleBatch(BaseModel):
    items: list[TitleItem]


class PersonCard(BaseModel):
    profile_path: str = Field(description="One of the allowed /in/<username>/ paths, exactly as given")
    full_name: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    degree: str | None = Field(default=None, description="1st, 2nd or 3rd if shown")


class PageExtraction(BaseModel):
    people: list[PersonCard]


class PingResult(BaseModel):
    ok: bool


class RelevanceItem(BaseModel):
    index: int = Field(description="The lead's index exactly as given in the prompt")
    relevant: bool = Field(description="True if this person is a plausible buyer/influencer for the offer")
    fit_score: int = Field(ge=0, le=100, description="0-100 fit to the offer")
    reason: str = Field(description="One short clause explaining the decision")


class RelevanceBatch(BaseModel):
    items: list[RelevanceItem]

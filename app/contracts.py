from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobContract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(min_length=1)
    source_url: str
    site: str = Field(min_length=1)
    captured_at: str
    description_text: str = Field(min_length=1)
    title: str | None = None
    company: str | None = None
    location: str | None = None
    employment_type: str | None = None
    salary_text: str | None = None
    extraction_mode: str = "raw"
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("captured_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("captured_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        return value


class EnvelopeContract(BaseModel):
    """Validate the envelope first; job validation is deliberately independent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[2]
    exported_at: str | None = None
    jobs: list[Any] = Field(min_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 2")
        return value

    @field_validator("exported_at")
    @classmethod
    def validate_exported_at(cls, value: str | None) -> str | None:
        if value is not None:
            return JobContract.validate_timestamp(value)
        return value

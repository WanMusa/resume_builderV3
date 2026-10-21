from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://resume_builder@localhost:5432/resume_builder"
    database_password: str | None = None
    api_token: str | None = None
    job_runs_api_token: str | None = None
    allow_unauthenticated: bool = False
    provider: Literal["mock", "openai"] = "mock"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    storage_backend: Literal["supabase"] = "supabase"
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str | None = None
    data_dir: Path = Path("data")
    inbox_dir: Path = Path("inbox/json")
    assets_dir: Path = Path("assets")
    prompts_dir: Path = Path("prompts")
    poll_seconds: float = Field(default=5, gt=0)
    lease_seconds: int = Field(default=600, ge=300)
    max_attempts: int = Field(default=3, gt=0)

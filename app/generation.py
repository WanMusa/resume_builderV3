"""Bounded, isolated generation. Fixed facts are never model-editable."""

import hashlib
import json
import re
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResumeVariables(StrictOutput):
    summary: Annotated[str, Field(min_length=1, max_length=1400)]
    expertise_ids: Annotated[list[str], Field(max_length=12)]
    achievement_ids: Annotated[list[str], Field(max_length=16)]


class Letter(StrictOutput):
    paragraphs: Annotated[list[Annotated[str, Field(min_length=1, max_length=1800)]],
                          Field(min_length=2, max_length=5)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=16)]


def validate_base(base: dict) -> None:
    """Fail before paid calls when source IDs or renderer-required facts are invalid."""
    if type(base.get("is_example")) is not bool:
        raise ValueError("Base resume requires an explicit boolean is_example")
    fixed = base["fixed"]
    if not isinstance(fixed["identity"]["name"], str) or not fixed["identity"]["name"].strip():
        raise ValueError("Base resume requires a candidate name")
    for role in fixed["experience"]:
        if not all(isinstance(role.get(k), str) and role[k].strip()
                   for k in ("id", "employer", "title", "dates")):
            raise ValueError("Base experience requires id, employer, title and dates")
    for group in (fixed["experience"], base["achievements"], base["expertise"]):
        ids = [r["id"] for r in group]
        if not all(isinstance(i, str) and i.strip() for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Base resume evidence IDs must be nonempty and unique within each section")
    role_ids = {r["id"] for r in fixed["experience"]}
    for record in [*base["achievements"], *base["expertise"]]:
        if not isinstance(record.get("text"), str) or not record["text"].strip():
            raise ValueError("Base resume evidence text must be nonempty")
    if not base["achievements"]:
        raise ValueError("At least one factual achievement is required")
    if any(r["experience_id"] not in role_ids for r in base["achievements"]):
        raise ValueError("Evidence references an unknown experience")
    if sum(bool(r.get("critical")) for r in base["achievements"]) > 16:
        raise ValueError("Output capacity cannot preserve more than 16 critical achievements")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def select_evidence(base: dict, job: dict, limit: int = 12) -> dict:
    """Rank whole records; always retain critical achievements and one per role.

    Never truncate achievement details. The limit is soft: preserving critical
    evidence and career coverage takes precedence over reducing tokens.
    """
    terms = set(re.findall(r"\w+", job["description_text"].lower()))
    records = base["achievements"]
    ranked = sorted(records, key=lambda r: (
        -len(terms & set(re.findall(r"\w+", canonical(r).lower()))), r["id"]))
    required = {r["id"] for r in records if r.get("critical")}
    for role in base["fixed"]["experience"]:
        match = next((r for r in ranked if r["experience_id"] == role["id"]), None)
        if match:
            required.add(match["id"])
    optional = [r for r in ranked if r["id"] not in required]
    chosen = required | {r["id"] for r in optional[:max(0, limit - len(required))]}
    return {
        "achievements": [r for r in records if r["id"] in chosen],
        "expertise": base["expertise"],
    }


def prepare(job: dict, settings) -> tuple[dict, dict, dict]:
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    validate_base(base)
    evidence = select_evidence(base, job)
    # No duplicate structured requirements or operational capture metadata.
    context = {"job": {k: job.get(k) for k in
                      ("title", "company", "location", "description_text")},
               "evidence": evidence}
    prompts = {p.stem: p.read_text() for p in settings.prompts_dir.glob("*.txt")}
    return base, context, prompts


class MockProvider:
    name: Literal["mock"] = "mock"

    def generate(self, kind: str, context: dict, prompts: dict, schema: type[BaseModel]):
        evidence = context["evidence"]
        if kind == "resume":
            result = {
                "summary": "Development preview — selected experience is listed below.",
                "expertise_ids": [r["id"] for r in evidence["expertise"]][:12],
                "achievement_ids": [r["id"] for r in evidence["achievements"]][:16],
            }
        else:
            result = {"paragraphs": [
                "Development preview — this is not an application-ready cover letter.",
                evidence["achievements"][0]["text"],
            ], "evidence_ids": [evidence["achievements"][0]["id"]]}
        return schema.model_validate(result), {}


class OpenAIProvider:
    name: Literal["openai"] = "openai"

    def __init__(self, settings):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for the openai provider")
        self.settings = settings

    def generate(self, kind: str, context: dict, prompts: dict, schema: type[BaseModel]):
        settings = self.settings
        # No tools, URL fetching, conversation history, or automatic paid retries.
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"******"},
                json={
                    "model": settings.openai_model,
                    "messages": [
                        {"role": "system", "content": prompts[f"{kind}_system"]},
                        {"role": "user", "content": prompts[f"{kind}_user"] + "\n" +
                         canonical(context)},
                    ],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": kind, "strict": True, "schema": schema.model_json_schema()}},
                    "max_completion_tokens": 2200,
                })
        if not response.is_success:
            # Never persist provider bodies: they may echo credentials or private input.
            raise RuntimeError(f"Generation provider returned HTTP {response.status_code}")
        payload = response.json()
        return (schema.model_validate_json(payload["choices"][0]["message"]["content"]),
                payload.get("usage", {}))


def get_provider(settings):
    return MockProvider() if settings.provider == "mock" else OpenAIProvider(settings)


def generate_package(job_payload: dict, settings) -> dict:
    base, context, prompts = prepare(job_payload, settings)
    if settings.provider != "mock" and base.get("is_example", True):
        raise ValueError("Replace example evidence and set is_example=false before live generation")
    evidence = context["evidence"]
    achievements = {r["id"]: r for r in evidence["achievements"]}
    expertise = {r["id"]: r for r in evidence["expertise"]}
    if not achievements:
        raise ValueError("At least one factual achievement is required")
    provider = get_provider(settings)
    resume, resume_usage = provider.generate("resume", context, prompts, ResumeVariables)
    letter, letter_usage = provider.generate("cover_letter", context, prompts, Letter)
    if (not set(resume.achievement_ids) <= achievements.keys()
            or not set(resume.expertise_ids) <= expertise.keys()
            or not set(letter.evidence_ids) <= achievements.keys()):
        raise ValueError("Provider referenced unknown or unselected evidence")
    if len(set(resume.achievement_ids)) != len(resume.achievement_ids):
        raise ValueError("Provider repeated achievement IDs")
    critical = {r["id"] for r in evidence["achievements"] if r.get("critical")}
    if not critical <= set(resume.achievement_ids):
        raise ValueError("Provider omitted critical achievements")
    role_ids = {r["id"] for r in base["fixed"]["experience"]}
    if any(r["experience_id"] not in role_ids for r in achievements.values()):
        raise ValueError("Evidence references an unknown experience")
    # Structural validation is not semantic fact verification. Live prose always
    # requires review; metrics and factual bullets remain verbatim source evidence.
    assembled = dict(base["fixed"])
    assembled.update(summary=resume.summary,
                     expertise=[expertise[i]["text"] for i in resume.expertise_ids],
                     achievements=[achievements[i] for i in resume.achievement_ids])
    return {
        "resume": assembled,
        "cover_letter": letter.model_dump(),
        "review_required": settings.provider != "mock",
        "provenance": {
            "provider": provider.name,
            "model": settings.openai_model if provider.name == "openai" else "mock",
            "base_hash": digest(base), "prompt_hash": digest(prompts),
            "input_hash": digest(context), "selected_evidence_ids": list(achievements),
            "usage": {"resume": resume_usage, "cover_letter": letter_usage},
            "is_example": base.get("is_example", True),
        },
    }

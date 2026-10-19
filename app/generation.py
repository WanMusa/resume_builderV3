"""Bounded, isolated generation. Fixed facts are never model-editable."""

import hashlib
import json
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class EvidenceValidationError(ValueError):
    MESSAGES = {
        "unknown_evidence": "Provider referenced unknown or unselected evidence",
        "repeated_achievements": "Provider repeated achievement IDs",
        "missing_critical": "Provider omitted critical achievements",
        "role_coverage": "Provider must cover every source role exactly once",
        "content_length": "Provider exceeded the concise resume or cover-letter word budget",
    }

    def __init__(self, reason: Literal["unknown_evidence", "repeated_achievements", "missing_critical",
                                     "role_coverage", "content_length"]):
        self.reason = reason
        super().__init__(self.MESSAGES[reason])


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Text = Annotated[str, Field(min_length=1, max_length=1800)]
EvidenceIDs = Annotated[list[str], Field(min_length=1, max_length=32)]


class Expertise(StrictOutput):
    area: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=400)]
    evidence_ids: EvidenceIDs


class Achievement(StrictOutput):
    highlight: Annotated[str, Field(min_length=1, max_length=160)]
    detail: Annotated[str, Field(min_length=1, max_length=600)]
    evidence_ids: EvidenceIDs


class Experience(StrictOutput):
    experience_id: str
    tailored_summary: Text
    tailored_achievements: Annotated[list[Achievement], Field(max_length=3)]


class ResumeVariables(StrictOutput):
    target_role_tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=60)]],
                                Field(min_length=1, max_length=3)]
    career_profile: Text
    areas_of_expertise: Annotated[list[Expertise], Field(max_length=8)]
    professional_experience: Annotated[list[Experience], Field(min_length=1)]


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
    if any(sum(r["experience_id"] == role["id"] and bool(r.get("critical"))
               for r in base["achievements"]) > 96
           for role in fixed["experience"]):
        raise ValueError("Output capacity cannot reference more than 96 critical achievements per role")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def prepare(job: dict, settings) -> tuple[dict, dict, dict]:
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    validate_base(base)
    # Supporting material informs wording; it is never automatically printed.
    context = {"job": {k: job.get(k) for k in
                      ("title", "company", "location", "description_text")},
               "background": {k: v for k, v in base["fixed"].items()
                              if k not in ("identity", "education", "education_records",
                                           "certifications", "references")},
               "evidence": {k: base[k] for k in ("achievements", "expertise")}}
    prompts = {p.stem: p.read_text() for p in settings.prompts_dir.glob("*.txt")}
    return base, context, prompts


class MockProvider:
    name: Literal["mock"] = "mock"

    def generate(self, kind: str, context: dict, prompts: dict, schema: type[BaseModel]):
        evidence = context["evidence"]
        if kind == "resume":
            result = {
                "target_role_tags": ["Development preview"],
                "career_profile": "Development preview, not an application-ready resume.",
                "areas_of_expertise": [
                    {"area": " ".join(r["text"].split()[:3]),
                     "description": " ".join(r["text"].split()[:17]), "evidence_ids": [r["id"]]}
                    for r in evidence["expertise"][:8]
                ],
                "professional_experience": [],
            }
            for role in context["background"]["experience"]:
                records = [r for r in evidence["achievements"] if r["experience_id"] == role["id"]]
                records = sorted(records, key=lambda r: not r.get("critical", False))[:96]
                result["professional_experience"].append({
                    "experience_id": role["id"],
                    "tailored_summary": "Development preview of this role's documented experience.",
                    "tailored_achievements": [
                        {"highlight": "Documented contribution.",
                         "detail": " ".join(records[start]["text"].split()[:30]),
                         "evidence_ids": [r["id"] for r in records[start:start + 32]]}
                        for start in range(0, len(records), 32)
                    ],
                })
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
        output_schema = schema.model_json_schema()
        achievement_ids = [r["id"] for r in context["evidence"]["achievements"]]
        if kind == "resume":
            definitions = output_schema["$defs"]
            definitions["Achievement"]["properties"]["evidence_ids"]["items"]["enum"] = achievement_ids
            expertise_ids = [r["id"] for r in context["evidence"]["expertise"]]
            if expertise_ids:
                definitions["Expertise"]["properties"]["evidence_ids"]["items"]["enum"] = expertise_ids
            else:
                output_schema["properties"]["areas_of_expertise"]["maxItems"] = 0
            definitions["Experience"]["properties"]["experience_id"]["enum"] = [
                r["id"] for r in context["background"]["experience"]
            ]
        else:
            output_schema["properties"]["evidence_ids"]["items"]["enum"] = achievement_ids
        # No tools, URL fetching, conversation history, or automatic paid retries.
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": "Bearer" + " " + settings.openai_api_key},
                json={
                    "model": settings.openai_model,
                    "messages": [
                        {"role": "system", "content": prompts[f"{kind}_system"]},
                        {"role": "user", "content": prompts[f"{kind}_user"] + "\n" +
                         canonical(context)},
                    ],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": kind, "strict": True, "schema": output_schema}},
                    "max_completion_tokens": 5000 if kind == "resume" else 1800,
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
    role_ids = [r["id"] for r in base["fixed"]["experience"]]
    returned_roles = [r.experience_id for r in resume.professional_experience]
    if len(returned_roles) != len(role_ids) or set(returned_roles) != set(role_ids):
        raise EvidenceValidationError("role_coverage")
    selected = []
    for role in resume.professional_experience:
        for achievement in role.tailored_achievements:
            if any(i not in achievements or achievements[i]["experience_id"] != role.experience_id
                   for i in achievement.evidence_ids):
                raise EvidenceValidationError("unknown_evidence")
            selected.extend(achievement.evidence_ids)
    if (any(not set(item.evidence_ids) <= expertise.keys() for item in resume.areas_of_expertise)
            or not set(letter.evidence_ids) <= achievements.keys()):
        raise EvidenceValidationError("unknown_evidence")
    if len(set(selected)) != len(selected):
        raise EvidenceValidationError("repeated_achievements")
    critical = {r["id"] for r in evidence["achievements"] if r.get("critical")}
    if not critical <= set(selected):
        raise EvidenceValidationError("missing_critical")
    budgets = [(resume.career_profile, 90)]
    budgets += [(" ".join(resume.target_role_tags), 18)]
    budgets += [(f"{r.area} {r.description}", 20) for r in resume.areas_of_expertise]
    for role in resume.professional_experience:
        budgets.append((role.tailored_summary, 60))
        budgets.extend((f"{a.highlight} {a.detail}", 35) for a in role.tailored_achievements)
    if (any(len(text.split()) > limit for text, limit in budgets)
            or sum(len(text.split()) for text, _ in budgets) > 900
            or sum(len(p.split()) for p in letter.paragraphs) > 350):
        raise EvidenceValidationError("content_length")
    # Citations establish provenance, not semantic truth: live prose needs review.
    tailored = {r.experience_id: r.model_dump() for r in resume.professional_experience}
    assembled = dict(base["fixed"])
    assembled.update(resume.model_dump(exclude={"professional_experience"}))
    assembled["experience"] = [
        {**role, **tailored[role["id"]]} for role in base["fixed"]["experience"]
    ]
    return {
        "format_version": 2,
        "source_resume": base,
        "resume": assembled,
        "cover_letter": letter.model_dump(),
        "review_required": settings.provider != "mock",
        "provenance": {
            "provider": provider.name,
            "model": settings.openai_model if provider.name == "openai" else "mock",
            "base_hash": digest(base), "prompt_hash": digest(prompts),
            "input_hash": digest(context), "selected_evidence_ids": selected,
            "usage": {"resume": resume_usage, "cover_letter": letter_usage},
            "is_example": base.get("is_example", True),
        },
    }

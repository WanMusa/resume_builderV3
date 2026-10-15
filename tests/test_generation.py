import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.generation import (MockProvider, OpenAIProvider, ResumeVariables,
                            generate_package, prepare, select_evidence, validate_base)
from app.rendering import render_package
from app.storage import LocalStorage, SupabaseStorage

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings():
    return SimpleNamespace(assets_dir=ROOT / "assets", prompts_dir=ROOT / "prompts",
                           provider="mock", openai_model="example-model",
                           openai_api_key=None)


@pytest.fixture
def job():
    return json.loads((ROOT / "examples/job-envelope.json").read_text())["jobs"][0]


def test_mock_package_fixed_facts_render_and_storage(settings, job, tmp_path):
    package = generate_package(job, settings)
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    assert package["resume"]["experience"] == base["fixed"]["experience"]
    assert package["resume"]["achievements"][0]["text"] == base["achievements"][0]["text"]
    assert package["provenance"]["usage"] == {"resume": {}, "cover_letter": {}}
    files = render_package(package, tmp_path / "rendered")
    assert {p.name for p in files} == {"package.json", "resume.md", "cover_letter.md"}
    assert "NOT FOR SUBMISSION" in files[1].read_text()
    storage = LocalStorage(tmp_path / "storage")
    for file in files:
        result = storage.upload(file, f"job-id/{file.name}")
        assert Path(result["local_path"]).read_bytes() == file.read_bytes()
    with pytest.raises(ValueError):
        storage.upload(files[0], "../escape")


def test_selection_retains_critical_and_each_role():
    base = {
        "fixed": {"experience": [{"id": "r1"}, {"id": "r2"}]},
        "expertise": [],
        "achievements": [
            {"id": "critical", "experience_id": "r1", "critical": True, "text": "Unmatched"},
            {"id": "matched", "experience_id": "r1", "text": "SQL"},
            {"id": "other-role", "experience_id": "r2", "text": "Unmatched",
             "details": "Important supporting details"},
        ],
    }
    result = select_evidence(base, {"description_text": "SQL"}, limit=1)
    assert {r["id"] for r in result["achievements"]} == {"critical", "matched", "other-role"}
    assert result["achievements"][-1]["details"] == "Important supporting details"


def test_prompt_context_avoids_duplicate_and_operational_data(settings, job):
    base, context, prompts = prepare({**job, "requirements": ["SQL"],
                                     "notes": "private note"}, settings)
    assert "requirements" not in context["job"]
    assert "notes" not in context["job"]
    assert "captured_at" not in context["job"]
    assert "identity" not in context["evidence"]
    assert "untrusted" in prompts["resume_system"]


def test_provider_rejects_unknown_ids(settings, job, monkeypatch):
    original = MockProvider.generate

    def invalid(self, kind, context, prompts, schema):
        result, usage = original(self, kind, context, prompts, schema)
        if kind == "resume":
            result.achievement_ids = ["invented"]
        return result, usage

    monkeypatch.setattr(MockProvider, "generate", invalid)
    with pytest.raises(ValueError, match="unknown"):
        generate_package(job, settings)


def test_live_requires_real_evidence(settings, job):
    settings.provider = "openai"
    with pytest.raises(ValueError, match="example evidence"):
        generate_package(job, settings)


def test_base_rejects_duplicate_and_unknown_ids(settings):
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    base["achievements"].append(dict(base["achievements"][0]))
    with pytest.raises(ValueError, match="unique"):
        validate_base(base)
    base["achievements"].pop()
    base["achievements"][0]["experience_id"] = "unknown"
    with pytest.raises(ValueError, match="unknown experience"):
        validate_base(base)


def test_openai_validates_response_without_network(settings, job, monkeypatch):
    settings.openai_api_key = "test-placeholder"
    settings.openai_base_url = "https://provider.example/v1"
    _, context, prompts = prepare(job, settings)
    calls = []

    def post(self, url, **kwargs):
        assert kwargs["headers"]["Authorization"].split() == ["Bearer", settings.openai_api_key]
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "summary": "Business analyst", "expertise_ids": ["skill-sql"],
            "achievement_ids": ["achievement-1"],
        })}}], "usage": {"prompt_tokens": 100}})

    monkeypatch.setattr(httpx.Client, "post", post)
    output, usage = OpenAIProvider(settings).generate("resume", context, prompts, ResumeVariables)
    assert output.achievement_ids == ["achievement-1"]
    assert usage["prompt_tokens"] == 100
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert len(calls[0]["messages"]) == 2


def test_supabase_failure_is_sanitized_and_retry_key_stable(tmp_path, monkeypatch):
    settings = SimpleNamespace(supabase_url="https://storage.example", supabase_key="test-only",
                               supabase_bucket="private-artifacts")
    file = tmp_path / "package.json"
    file.write_text("{}")
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(503, text="sensitive provider response")

    monkeypatch.setattr(httpx.Client, "post", post)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="HTTP 503") as error:
            SupabaseStorage(settings).upload(file, "job-uuid/package.json")
        assert "sensitive" not in str(error.value)
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["headers"]["x-upsert"] == "true"
    assert calls[0][1]["headers"]["Authorization"].split() == ["Bearer", settings.supabase_key]

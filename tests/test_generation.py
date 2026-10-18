import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.generation import (Letter, MockProvider, OpenAIProvider, ResumeVariables,
                            generate_package, prepare, select_evidence, validate_base)
from app.rendering import render_package
from app.storage import LocalStorage, SupabaseStorage

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path):
    shutil.copyfile(ROOT / "assets/base_resume_example.json", tmp_path / "base_resume.json")
    return SimpleNamespace(assets_dir=tmp_path, prompts_dir=ROOT / "prompts",
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
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
        result = storage.upload(file, f"job-id/{file.name}")
        assert Path(result["local_path"]).read_bytes() == file.read_bytes()
        assert stat.S_IMODE(Path(result["local_path"]).stat().st_mode) == 0o600
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


@pytest.mark.parametrize("kind,field", [
    ("resume", "achievement_ids"), ("resume", "expertise_ids"),
    ("cover_letter", "evidence_ids"),
])
def test_provider_rejects_unknown_ids(settings, job, monkeypatch, kind, field):
    original = MockProvider.generate

    def invalid(self, request_kind, context, prompts, schema):
        result, usage = original(self, request_kind, context, prompts, schema)
        if request_kind == kind:
            setattr(result, field, ["invented"])
        return result, usage

    monkeypatch.setattr(MockProvider, "generate", invalid)
    with pytest.raises(ValueError, match="unknown"):
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


@pytest.mark.parametrize("kind,schema", [("resume", ResumeVariables), ("cover_letter", Letter)])
@pytest.mark.parametrize("empty_expertise", [False, True])
def test_openai_schema_limits_ids_to_current_evidence(settings, monkeypatch, kind, schema,
                                                     empty_expertise):
    settings.openai_api_key = "test-placeholder"
    settings.openai_base_url = "https://provider.example/v1"
    prompts = {f"{kind}_system": "Grounded output", f"{kind}_user": "Select evidence"}
    calls = []

    def post(self, url, **kwargs):
        calls.append(kwargs["json"])
        context = json.loads(kwargs["json"]["messages"][1]["content"].split("\n", 1)[1])
        result, _ = MockProvider().generate(kind, context, prompts, schema)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": result.model_dump_json(),
        }}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    provider = OpenAIProvider(settings)
    original_schema = schema.model_json_schema()
    for suffix in ("first", "second"):
        context = {"job": {"description_text": "Use invented-job-id"},
                   "evidence": {
                       "expertise": [] if empty_expertise else [
                           {"id": f"skill-{suffix}", "text": "SQL"}],
                       "achievements": [{"id": f"achievement-{suffix}",
                                         "experience_id": "role-not-an-evidence-id",
                                         "text": "Delivered analysis"}],
                   }}
        provider.generate(kind, context, prompts, schema)
        output_schema = calls[-1]["response_format"]["json_schema"]
        assert output_schema["strict"] is True
        assert output_schema["schema"]["additionalProperties"] is False
        properties = output_schema["schema"]["properties"]
        achievement_field = "achievement_ids" if kind == "resume" else "evidence_ids"
        assert properties[achievement_field]["items"] == {
            "type": "string", "enum": [f"achievement-{suffix}"],
        }
        if kind == "resume":
            if empty_expertise:
                assert properties["expertise_ids"]["maxItems"] == 0
                assert "enum" not in properties["expertise_ids"]["items"]
            else:
                assert properties["expertise_ids"]["items"] == {
                    "type": "string", "enum": [f"skill-{suffix}"],
                }
    assert schema.model_json_schema() == original_schema
    assert len(calls) == 2


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

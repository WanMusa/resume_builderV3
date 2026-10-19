import json
import shutil
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from docx import Document
from pydantic import ValidationError

from app.generation import (Letter, MockProvider, OpenAIProvider, ResumeVariables,
                            generate_package, prepare, select_evidence, validate_base)
from app.rendering import render_package
from app.storage import DOCX_CONTENT_TYPE, SupabaseStorage, get_storage

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


def test_mock_package_fixed_facts_render_and_storage(settings, job, tmp_path, monkeypatch):
    package = generate_package(job, settings)
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    assert package["resume"]["experience"] == base["fixed"]["experience"]
    assert package["resume"]["achievements"][0]["text"] == base["achievements"][0]["text"]
    assert package["provenance"]["usage"] == {"resume": {}, "cover_letter": {}}
    files = render_package(package, ROOT / "assets", job)
    assert set(files) == {"resume.docx", "cover_letter.docx"}
    for content in files.values():
        assert content.startswith(b"PK")
        assert "NOT FOR SUBMISSION" in "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
    assert not (tmp_path / "rendered").exists()
    requests = []

    def post(self, url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.Client, "post", post)
    storage = SupabaseStorage(SimpleNamespace(supabase_url="https://storage.example",
                              supabase_key="test-only", supabase_bucket="private-artifacts"))
    for name, content in files.items():
        result = storage.upload(content, f"job-id/{name}")
        assert result["backend"] == "supabase" and result["local_path"] is None
        assert requests[-1][1]["content"] == content
        assert requests[-1][1]["headers"]["Content-Type"] == DOCX_CONTENT_TYPE
    assert len(requests) == 2
    with pytest.raises(ValueError, match="safe Word"):
        storage.upload(b"document", "../escape.docx")


def test_word_preserves_fixed_sections_and_v2_layout(settings, job):
    package = generate_package(job, settings)
    resume = package["resume"]
    resume["identity"].update(email="candidate@example.com", mobile="1234",
                              location="Example City", linkedin="https://example.com/profile")
    resume["experience"][0].update(location="Role City", summary="Fixed role summary",
                                    platform_description="Fixed platform")
    resume["education_records"] = [{"qualification": "Fixed qualification",
                                    "institution": "Fixed institution", "details": "Fixed details"}]
    resume["references"] = "Fixed references"
    resume["career_profile"] = "Original fixed profile"
    resume["selected_projects"] = [{"name": "Fixed project", "highlights": ["Fixed highlight"]}]
    files = render_package(package, ROOT / "assets", job)
    doc = Document(BytesIO(files["resume.docx"]))
    text = "\n".join(p.text for p in doc.paragraphs)
    for value in ("Fixed role summary", "Fixed platform", "Role City", "Fixed institution",
                  "Fixed references", "Original fixed profile", "Fixed highlight",
                  resume["experience"][0]["employer"], resume["experience"][0]["dates"],
                  resume["achievements"][0]["text"], resume["summary"]):
        assert value in text
    name = next(p for p in doc.paragraphs if p.text == resume["identity"]["name"])
    assert name.runs[0].font.name == "Lato"
    assert name.runs[0].font.size.pt == 18
    assert str(name.runs[0].font.color.rgb) == "365F91"
    template = Document(str(ROOT / "assets/templates/resume_template.docx"))
    assert doc.sections[0].left_margin == template.sections[0].left_margin
    assert doc.sections[0].page_width == template.sections[0].page_width
    targets = [rel.target_ref for rel in doc.part.rels.values() if rel.is_external]
    assert "mailto:candidate@example.com" in targets
    assert "https://example.com/profile" in targets
    letter = Document(BytesIO(files["cover_letter.docx"]))
    letter_text = "\n".join(p.text for p in letter.paragraphs)
    assert job["company"] in letter_text
    assert "Dear Hiring Manager," in letter_text
    assert all(p in letter_text for p in package["cover_letter"]["paragraphs"])
    assert any(r.font.name == "Aptos" for p in letter.paragraphs for r in p.runs)


def test_model_cannot_modify_fixed_fields():
    with pytest.raises(ValidationError):
        ResumeVariables.model_validate({"summary": "Draft", "expertise_ids": [],
                                        "achievement_ids": [], "identity": {"name": "Invented"}})


def test_final_storage_has_no_local_fallback():
    with pytest.raises(ValueError, match="local output is disabled"):
        get_storage(SimpleNamespace(storage_backend="local"))
    with pytest.raises(ValueError, match="configured"):
        get_storage(SimpleNamespace(storage_backend="supabase", supabase_url=None,
                                    supabase_key=None, supabase_bucket=None))


@pytest.mark.parametrize("public", [False, True, None])
def test_supabase_requires_private_bucket(monkeypatch, public):
    storage = SupabaseStorage(SimpleNamespace(supabase_url="https://storage.example",
                              supabase_key="test-only", supabase_bucket="private-artifacts"))
    monkeypatch.setattr(httpx.Client, "get",
                        lambda *args, **kwargs: httpx.Response(200, json={"public": public}))
    if public is False:
        storage.check_bucket()
    else:
        with pytest.raises(ValueError, match="private"):
            storage.check_bucket()


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
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(503, text="sensitive provider response")

    monkeypatch.setattr(httpx.Client, "post", post)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="HTTP 503") as error:
            SupabaseStorage(settings).upload(b"word-document", "job-uuid/resume.docx")
        assert "sensitive" not in str(error.value)
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["headers"]["x-upsert"] == "true"
    assert calls[0][1]["headers"]["Authorization"].split() == ["Bearer", settings.supabase_key]

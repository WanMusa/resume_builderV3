import copy
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
                            generate_package, prepare, validate_base)
from app.rendering import PackageFormatError, render_package
from app.storage import DOCX_CONTENT_TYPE, SupabaseStorage, get_storage

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path):
    shutil.copyfile(ROOT / "assets/base_resume_example.json", tmp_path / "base_resume.json")
    return SimpleNamespace(assets_dir=tmp_path, prompts_dir=ROOT / "prompts",
                           provider="mock", openai_model="example-model", openai_api_key=None)


@pytest.fixture
def job():
    return json.loads((ROOT / "examples/job-envelope.json").read_text())["jobs"][0]


def filenames(package, job):
    prefix = f'{package["resume"]["identity"]["name"]}-{job["title"]}-{job["company"]}'
    return [f"{prefix}-Resume.docx", f"{prefix}-Cover Letter.docx"]


def test_mock_package_fixed_facts_render_and_storage(settings, job, tmp_path, monkeypatch):
    original = (settings.assets_dir / "base_resume.json").read_bytes()
    package = generate_package(job, settings)
    base = json.loads(original)
    assert package["format_version"] == 2
    assert package["source_resume"] == base
    for key in ("identity", "education", "certifications"):
        assert package["resume"][key] == base["fixed"][key]
    for role, source in zip(package["resume"]["experience"], base["fixed"]["experience"]):
        assert all(role[key] == source[key] for key in source)
        assert role["tailored_summary"] and role["tailored_achievements"][0]["detail"]
    assert (settings.assets_dir / "base_resume.json").read_bytes() == original
    assert package["provenance"]["usage"] == {"resume": {}, "cover_letter": {}}
    files = render_package(package, ROOT / "assets", job)
    assert list(files) == filenames(package, job)
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
        result = storage.upload(content, name)
        assert result["object_key"] == name and result["local_path"] is None
        assert requests[-1][1]["content"] == content
        assert requests[-1][1]["headers"]["Content-Type"] == DOCX_CONTENT_TYPE
    assert len(requests) == 2


def test_word_renders_tailored_prose_not_source_dump(settings, job):
    package = generate_package(job, settings)
    resume = package["resume"]
    resume["identity"].update(email="candidate@example.com", mobile="1234",
                              location="Example City", linkedin="https://example.com/profile")
    role = resume["experience"][0]
    role.update(summary="SOURCE ROLE CONTEXT", platform_description="SOURCE PLATFORM CONTEXT",
                tailored_summary="Concise tailored role scope.",
                tailored_achievements=[{"highlight": "Improved reporting.",
                                       "detail": "Validated requirements with stakeholders.",
                                       "evidence_ids": ["PRIVATE-CITATION-ID"]}])
    resume["career_profile"] = "Concise tailored profile."
    resume["education_records"] = [{"qualification": "Fixed qualification",
                                    "institution": "Fixed institution", "period": "2020",
                                    "details": "Fixed education details"}]
    resume["education"] = ["DUPLICATE EDUCATION STRING"]
    resume["references"] = "Fixed references"
    resume["certifications"] = ["Fixed certification (2021)"]
    resume["selected_projects"] = [{"name": "SOURCE PROJECT CONTEXT"}]
    package["source_resume"]["fixed"]["career_profile"] = "SOURCE PROFILE CONTEXT"
    package["provenance"].update(provider="openai", is_example=False)
    package["review_required"] = True
    files = render_package(package, ROOT / "assets", job)
    doc = Document(BytesIO(files[filenames(package, job)[0]]))
    text = "\n".join(p.text for p in doc.paragraphs)
    for value in ("Concise tailored role scope.", "Improved reporting.",
                  "Validated requirements with stakeholders.", "Concise tailored profile.",
                  "Fixed qualification", "Fixed institution", "Fixed education details",
                  "Fixed references", "Fixed certification", role["employer"], role["dates"]):
        assert value in text
    for value in ("SOURCE", "DUPLICATE", "PRIVATE-CITATION-ID", "GENERATED DRAFT"):
        assert value not in text
    assert text.count("Fixed qualification") == 1
    name = next(p for p in doc.paragraphs if p.text == resume["identity"]["name"])
    assert name.runs[0].font.name == "Lato" and name.runs[0].font.size.pt == 18
    assert str(name.runs[0].font.color.rgb) == "365F91"
    bullet = next(p for p in doc.paragraphs if "Improved reporting." in p.text)
    assert bullet.runs[0].bold and not bullet.runs[1].bold
    assert bullet.paragraph_format.keep_together
    headings = [p for p in doc.paragraphs if p.text in (
        "CAREER PROFILE", "AREAS OF EXPERTISE/HIGHLIGHTS", "PROFESSIONAL EXPERIENCE",
        "EDUCATION", "REFERENCES")]
    assert len(headings) == 5
    assert all(p.paragraph_format.keep_with_next for p in headings)
    template = Document(str(ROOT / "assets/templates/resume_template.docx"))
    assert doc.sections[0].left_margin == template.sections[0].left_margin
    assert doc.sections[0].page_width == template.sections[0].page_width
    targets = [rel.target_ref for rel in doc.part.rels.values() if rel.is_external]
    assert "mailto:candidate@example.com" in targets and "https://example.com/profile" in targets
    letter = Document(BytesIO(files[filenames(package, job)[1]]))
    letter_text = "\n".join(p.text for p in letter.paragraphs)
    assert job["company"] in letter_text and "Dear Hiring Manager," in letter_text
    assert all(p in letter_text for p in package["cover_letter"]["paragraphs"])
    assert any(r.font.name == "Aptos" for p in letter.paragraphs for r in p.runs)


def test_filenames_follow_v2_without_directories(settings, job):
    package = generate_package(job, settings)
    package["resume"]["identity"]["name"] = "A/B: Candidate"
    files = render_package(package, ROOT / "assets",
                           {**job, "title": 'Senior: Analyst / AI', "company": 'A|B "Ltd"'})
    assert set(files) == {
        "AB Candidate-Senior Analyst  AI-AB Ltd-Resume.docx",
        "AB Candidate-Senior Analyst  AI-AB Ltd-Cover Letter.docx",
    }
    assert all("/" not in name and "\\" not in name for name in files)


def test_legacy_checkpoint_requires_explicit_regeneration():
    with pytest.raises(PackageFormatError, match="regenerate"):
        render_package({"resume": {"summary": "Old selection-only content"}}, ROOT / "assets")


def test_model_cannot_modify_fixed_fields(settings, job):
    _, context, prompts = prepare(job, settings)
    output, _ = MockProvider().generate("resume", context, prompts, ResumeVariables)
    with pytest.raises(ValidationError):
        ResumeVariables.model_validate({**output.model_dump(), "identity": {"name": "Invented"}})
    output_dict = output.model_dump()
    output_dict["professional_experience"][0]["employer"] = "Invented"
    with pytest.raises(ValidationError):
        ResumeVariables.model_validate(output_dict)


def test_full_context_and_fixed_role_order(settings, job, monkeypatch):
    path = settings.assets_dir / "base_resume.json"
    base = json.loads(path.read_text())
    base["fixed"]["experience"].append({"id": "role-2", "title": "Engineer",
                                        "employer": "Example", "dates": "2019-2022",
                                        "summary": "Long background context"})
    base["fixed"]["career_profile"] = "Original source profile"
    base["fixed"]["selected_projects"] = [{"name": "Project context", "details": "Complete details"}]
    base["achievements"].extend([
        {"id": f"evidence-{i}", "experience_id": "role-2", "text": f"Contribution {i}",
         "supporting_details": ["Detailed factual background"]}
        for i in range(20)
    ])
    path.write_text(json.dumps(base))
    _, context, prompts = prepare({**job, "notes": "private operator note"}, settings)
    assert context["evidence"]["achievements"] == base["achievements"]
    assert context["background"]["experience"] == base["fixed"]["experience"]
    assert context["background"]["selected_projects"] == base["fixed"]["selected_projects"]
    assert "identity" not in context["background"] and "notes" not in context["job"]
    assert "untrusted" in prompts["resume_system"]
    original = MockProvider.generate

    def reverse(self, kind, context, prompts, schema):
        output, usage = original(self, kind, context, prompts, schema)
        if kind == "resume":
            output.professional_experience.reverse()
        return output, usage

    monkeypatch.setattr(MockProvider, "generate", reverse)
    package = generate_package(job, settings)
    assert [r["id"] for r in package["resume"]["experience"]] == ["role-1", "role-2"]
    assert package["source_resume"] == base


@pytest.mark.parametrize("invalid,reason", [
    ("achievement", "unknown_evidence"), ("expertise", "unknown_evidence"),
    ("letter", "unknown_evidence"), ("role", "role_coverage"),
    ("duplicate-role", "role_coverage"), ("duplicate-achievement", "repeated_achievements"),
    ("missing-critical", "missing_critical"), ("long-profile", "content_length"),
    ("long-role", "content_length"), ("long-bullet", "content_length"),
    ("long-expertise", "content_length"), ("long-letter", "content_length"),
])
def test_rejects_invalid_or_overlong_prose(settings, job, monkeypatch, invalid, reason):
    original = MockProvider.generate

    def alter(self, kind, context, prompts, schema):
        result, usage = original(self, kind, context, prompts, schema)
        if kind == "resume":
            role = result.professional_experience[0]
            if invalid == "achievement":
                role.tailored_achievements[0].evidence_ids = ["invented"]
            elif invalid == "expertise":
                result.areas_of_expertise[0].evidence_ids = ["invented"]
            elif invalid == "role":
                role.experience_id = "invented"
            elif invalid == "duplicate-role":
                result.professional_experience.append(role)
            elif invalid == "duplicate-achievement":
                role.tailored_achievements.append(role.tailored_achievements[0])
            elif invalid == "missing-critical":
                role.tailored_achievements = []
            elif invalid == "long-profile":
                result.career_profile = "word " * 91
            elif invalid == "long-role":
                role.tailored_summary = "word " * 61
            elif invalid == "long-bullet":
                role.tailored_achievements[0].detail = "word " * 36
            elif invalid == "long-expertise":
                result.areas_of_expertise[0].description = "word " * 21
        elif invalid == "letter":
            result.evidence_ids = ["invented"]
        elif invalid == "long-letter":
            result.paragraphs = ["word " * 180, "word " * 180]
        return result, usage

    monkeypatch.setattr(MockProvider, "generate", alter)
    with pytest.raises(ValueError) as error:
        generate_package(job, settings)
    assert error.value.reason == reason


def test_base_rejects_duplicate_and_unknown_ids(settings):
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    base["achievements"].append(dict(base["achievements"][0]))
    with pytest.raises(ValueError, match="unique"):
        validate_base(base)
    base["achievements"].pop()
    base["achievements"][0]["experience_id"] = "unknown"
    with pytest.raises(ValueError, match="unknown experience"):
        validate_base(base)


@pytest.mark.parametrize("field", ["profile", "summary", "bullet", "expertise", "letter"])
@pytest.mark.parametrize("excess", [0, 1])
def test_word_limit_boundaries(settings, job, monkeypatch, field, excess):
    original = MockProvider.generate

    def boundary(self, kind, context, prompts, schema):
        result, usage = original(self, kind, context, prompts, schema)
        if kind == "resume":
            role = result.professional_experience[0]
            if field == "profile":
                result.career_profile = "word " * (90 + excess)
            elif field == "summary":
                role.tailored_summary = "word " * (60 + excess)
            elif field == "bullet":
                role.tailored_achievements[0].highlight = "Impact"
                role.tailored_achievements[0].detail = "word " * (34 + excess)
            elif field == "expertise":
                result.areas_of_expertise[0].area = "Analysis"
                result.areas_of_expertise[0].description = "word " * (19 + excess)
        elif field == "letter":
            result.paragraphs = ["word " * 175, "word " * (175 + excess)]
        return result, usage

    monkeypatch.setattr(MockProvider, "generate", boundary)
    if excess:
        with pytest.raises(ValueError) as error:
            generate_package(job, settings)
        assert error.value.reason == "content_length"
    else:
        assert generate_package(job, settings)["format_version"] == 2


@pytest.mark.parametrize("excess", [0, 1])
def test_combined_resume_word_budget(settings, job, monkeypatch, excess):
    path = settings.assets_dir / "base_resume.json"
    base = json.loads(path.read_text())
    base["fixed"]["experience"] = [
        {"id": f"role-{i}", "employer": "Example", "title": "Analyst", "dates": "2020-2021"}
        for i in range(5)
    ]
    base["expertise"] = [{"id": f"skill-{i}", "text": "Analysis"} for i in range(8)]
    base["achievements"] = [
        {"id": f"achievement-{i}-{j}", "experience_id": f"role-{i}", "text": "Delivered analysis"}
        for i in range(5) for j in range(2)
    ]
    path.write_text(json.dumps(base))
    original = MockProvider.generate

    def boundary(self, kind, context, prompts, schema):
        if kind != "resume":
            return original(self, kind, context, prompts, schema)
        result = {
            "target_role_tags": ["tag " * 6] * 3,
            "career_profile": "word " * 90,
            "areas_of_expertise": [
                {"area": "Analysis", "description": "word " * 19, "evidence_ids": [r["id"]]}
                for r in base["expertise"]
            ],
            "professional_experience": [
                {"experience_id": f"role-{i}", "tailored_summary": "word " * 60,
                 "tailored_achievements": [
                     {"highlight": "Impact",
                      "detail": "word " * ((16 + excess) if (i, j) == (4, 1) else 34),
                      "evidence_ids": [f"achievement-{i}-{j}"]}
                     for j in range(2)
                 ]}
                for i in range(5)
            ],
        }
        return schema.model_validate(result), {}

    monkeypatch.setattr(MockProvider, "generate", boundary)
    if excess:
        with pytest.raises(ValueError) as error:
            generate_package(job, settings)
        assert error.value.reason == "content_length"
    else:
        assert generate_package(job, settings)["format_version"] == 2


def test_achievement_cannot_be_moved_to_another_role(settings, job, monkeypatch):
    path = settings.assets_dir / "base_resume.json"
    base = json.loads(path.read_text())
    base["fixed"]["experience"].append({
        "id": "role-2", "employer": "Another employer", "title": "Analyst", "dates": "2019"})
    path.write_text(json.dumps(base))
    original = MockProvider.generate

    def cross_role(self, kind, context, prompts, schema):
        result, usage = original(self, kind, context, prompts, schema)
        if kind == "resume":
            result.professional_experience[1].tailored_achievements = (
                result.professional_experience[0].tailored_achievements)
            result.professional_experience[0].tailored_achievements = []
        return result, usage

    monkeypatch.setattr(MockProvider, "generate", cross_role)
    with pytest.raises(ValueError) as error:
        generate_package(job, settings)
    assert error.value.reason == "unknown_evidence"


def test_lengthy_source_is_retained_without_expanding_mock_output(settings, job):
    path = settings.assets_dir / "base_resume.json"
    base = json.loads(path.read_text())
    base["achievements"].extend([
        {"id": f"additional-{i}", "experience_id": "role-1", "text": "Delivered factual analysis",
         "supporting_details": ["Complete background context"], "critical": i == 149}
        for i in range(150)
    ])
    path.write_text(json.dumps(base))
    package = generate_package(job, settings)
    assert package["source_resume"] == base
    assert len(package["resume"]["experience"][0]["tailored_achievements"]) == 3
    assert "additional-149" in package["provenance"]["selected_evidence_ids"]


@pytest.mark.parametrize("kind,schema", [("resume", ResumeVariables), ("cover_letter", Letter)])
@pytest.mark.parametrize("empty_expertise", [False, True])
def test_openai_schema_and_prose_response(settings, job, monkeypatch, kind, schema, empty_expertise):
    settings.openai_api_key = "test-placeholder"
    settings.openai_base_url = "https://provider.example/v1"
    _, context, prompts = prepare(job, settings)
    if empty_expertise:
        context["evidence"]["expertise"] = []
    calls = []

    def post(self, url, **kwargs):
        assert kwargs["headers"]["Authorization"].split() == ["Bearer", settings.openai_api_key]
        calls.append(kwargs["json"])
        request_context = json.loads(kwargs["json"]["messages"][1]["content"].rsplit("\n", 1)[1])
        result, _ = MockProvider().generate(kind, request_context, prompts, schema)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": result.model_dump_json()}}], "usage": {"prompt_tokens": 100}})

    monkeypatch.setattr(httpx.Client, "post", post)
    original_schema = copy.deepcopy(schema.model_json_schema())
    for suffix in ("first", "second"):
        context["evidence"]["achievements"][0]["id"] = f"evidence-{suffix}"
        output, usage = OpenAIProvider(settings).generate(kind, context, prompts, schema)
        assert usage["prompt_tokens"] == 100
        contract = calls[-1]["response_format"]["json_schema"]
        assert contract["strict"] and not contract["schema"]["additionalProperties"]
        if kind == "resume":
            definitions = contract["schema"]["$defs"]
            assert definitions["Achievement"]["properties"]["evidence_ids"]["items"]["enum"] == [
                f"evidence-{suffix}"]
            assert definitions["Experience"]["properties"]["experience_id"]["enum"] == ["role-1"]
            assert output.professional_experience[0].tailored_achievements[0].detail
            if empty_expertise:
                assert contract["schema"]["properties"]["areas_of_expertise"]["maxItems"] == 0
            else:
                assert definitions["Expertise"]["properties"]["evidence_ids"]["items"]["enum"] == [
                    r["id"] for r in context["evidence"]["expertise"]]
        else:
            assert contract["schema"]["properties"]["evidence_ids"]["items"]["enum"] == [
                f"evidence-{suffix}"]
    assert schema.model_json_schema() == original_schema and len(calls) == 2


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


@pytest.mark.parametrize("key", ["folder/resume.docx", "../escape.docx", r"folder\resume.docx",
                                "resume.md", "bad\x00.docx"])
def test_supabase_rejects_subdirectories_and_unsafe_keys(key):
    storage = SupabaseStorage(SimpleNamespace(supabase_url="https://storage.example",
                              supabase_key="test-only", supabase_bucket="private-artifacts"))
    with pytest.raises(ValueError, match="safe Word"):
        storage.upload(b"document", key)


def test_supabase_failure_is_sanitized_and_overwrite_key_stable(monkeypatch):
    settings = SimpleNamespace(supabase_url="https://storage.example", supabase_key="test-only",
                               supabase_bucket="private-artifacts")
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(503, text="sensitive provider response")

    monkeypatch.setattr(httpx.Client, "post", post)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="HTTP 503") as error:
            SupabaseStorage(settings).upload(b"word-document", "Candidate-Role-Company-Resume.docx")
        assert "sensitive" not in str(error.value)
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["headers"]["x-upsert"] == "true"

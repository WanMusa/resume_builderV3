import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import init_db
from app.main import MAX_UPLOAD_BYTES, create_app
from app.models import Artifact, Batch, GeneratedDocument, Job

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer" + " " + "test-token"}
JOB_RUNS_HEADERS = {"Authorization": "Bearer" + " " + "runs-test-token"}


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/api.db",
                        api_token="test-token", job_runs_api_token="runs-test-token",
                        supabase_url="https://storage.example", supabase_key="storage-test-key",
                        supabase_bucket="private-artifacts", data_dir=tmp_path)
    app = create_app(settings)
    init_db(app.state.engine)
    with TestClient(app) as client:
        yield client


def test_health_auth_and_upload_visibility(client):
    assert client.get("/health").status_code == 200
    assert client.get("/jobs").status_code == 401
    headers = HEADERS
    assert client.get("/jobs", headers={"Authorization": "Bearer" + " " + "wrong"}).status_code == 401
    payload = json.loads((ROOT / "examples/job-envelope.json").read_text())
    response = client.post("/batches", json=payload, headers=headers)
    assert response.status_code == 201
    batch = response.json()
    assert len(batch["job_ids"]) == 1
    duplicate = client.post("/batches", json=payload, headers=headers)
    assert duplicate.json()["id"] == batch["id"]
    job_id = batch["job_ids"][0]
    detail = client.get(f"/jobs/{job_id}", headers=headers).json()
    assert detail["status"] == "queued"
    assert detail["input"]["description_text"] == payload["jobs"][0]["description_text"]
    assert client.get("/jobs?status=completed", headers=headers).json() == []
    assert client.get("/batches", headers=headers).json()[0]["id"] == batch["id"]
    assert client.post(f"/jobs/{job_id}/retry", headers=headers).status_code == 409
    assert client.get("/jobs/missing", headers=headers).status_code == 404
    assert client.get("/jobs?limit=0", headers=headers).status_code == 422


def test_upload_errors_and_size_limit(client):
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert client.post("/batches", content="{", headers=headers).status_code == 422
    assert client.post("/batches", json={"schema_version": 2}, headers=headers).status_code == 422
    assert client.post("/batches", content="x" * (MAX_UPLOAD_BYTES + 1),
                       headers=headers).status_code == 413
    assert client.post("/batches", content="{}", headers=HEADERS
                       ).status_code == 415


def test_api_refuses_implicit_unauthenticated_mode():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        create_app(Settings(_env_file=None, api_token=None, allow_unauthenticated=False))


@pytest.mark.parametrize("job_runs_token", [None, "test-token"])
def test_job_runs_requires_a_distinct_configured_token(tmp_path, job_runs_token):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/token.db",
                        api_token="test-token", job_runs_api_token=job_runs_token,
                        data_dir=tmp_path)
    app = create_app(settings)
    init_db(app.state.engine)
    with TestClient(app) as client:
        assert client.get("/job-runs", headers=JOB_RUNS_HEADERS).status_code == 503


def test_rerender_api_requires_auth_checkpoint_and_terminal_job(client):
    payload = json.loads((ROOT / "examples/job-envelope.json").read_text())
    batch = client.post("/batches", json=payload, headers=HEADERS).json()
    job_id = batch["job_ids"][0]
    route = f"/jobs/{job_id}/rerender"
    assert client.post(route).status_code == 401
    assert client.post("/jobs/missing/rerender", headers=HEADERS).status_code == 404
    assert client.post(route, headers=HEADERS).status_code == 409
    from sqlalchemy.orm import Session

    with Session(client.app.state.engine) as session:
        session.get(Job, job_id).status = "needs_review"
        session.add(GeneratedDocument(job_id=job_id, package={"saved": "unchanged"}))
        session.add(Artifact(job_id=job_id, kind="resume", filename="resume.md",
                             backend="local", object_key="old/resume.md",
                             local_path="/app/data/old/resume.md"))
        session.commit()
    response = client.post(route, headers=HEADERS)
    assert response.status_code == 202 and response.json()["status"] == "queued"
    detail = client.get(f"/jobs/{job_id}", headers=HEADERS).json()
    assert detail["generated"] == {"saved": "unchanged"} and detail["artifacts"] == []
    assert client.post(route, headers=HEADERS).status_code == 409


def test_regenerate_requires_explicit_paid_confirmation_and_keeps_checkpoint(client):
    from sqlalchemy.orm import Session

    payload = json.loads((ROOT / "examples/job-envelope.json").read_text())
    job_id = client.post("/batches", json=payload, headers=HEADERS).json()["job_ids"][0]
    route = f"/jobs/{job_id}/regenerate"
    assert client.post(route).status_code == 401
    assert client.post(route, headers=HEADERS).status_code == 400
    confirmed = "?confirm_paid_generation=true"
    assert client.post("/jobs/missing/regenerate" + confirmed, headers=HEADERS).status_code == 404
    assert client.post(route + confirmed, headers=HEADERS).status_code == 409
    with Session(client.app.state.engine) as session:
        session.get(Job, job_id).status = "needs_review"
        session.add(GeneratedDocument(job_id=job_id, package={"saved": "original"}))
        session.commit()
    response = client.post(route + confirmed, headers=HEADERS)
    assert response.status_code == 202 and response.json()["status"] == "queued"
    detail = client.get(f"/jobs/{job_id}", headers=HEADERS).json()
    assert detail["generated"] == {"saved": "original", "regeneration_requested": True}
    assert client.post(route + confirmed, headers=HEADERS).status_code == 409
    assert client.post(f"/jobs/{job_id}/rerender", headers=HEADERS).status_code == 409


def test_job_runs_token_is_separate_and_list_exposes_only_run_summary(client):
    payload = json.loads((ROOT / "examples/job-envelope.json").read_text())
    job_id = client.post("/batches", json=payload, headers=HEADERS).json()["job_ids"][0]

    assert client.get("/job-runs").status_code == 401
    assert client.get("/job-runs", headers=HEADERS).status_code == 401
    response = client.get("/job-runs", headers=JOB_RUNS_HEADERS)

    assert response.status_code == 200
    assert response.json() == [{
        "id": job_id,
        "title": payload["jobs"][0]["title"],
        "company": payload["jobs"][0]["company"],
        "status": "queued",
        "attempts": 0,
        "error": None,
        "created_at": response.json()[0]["created_at"],
        "updated_at": response.json()[0]["updated_at"],
        "resume_available": False,
        "can_regenerate": False,
        "can_retry": False,
    }]
    assert "description_text" not in response.json()[0]
    assert client.get("/jobs", headers=JOB_RUNS_HEADERS).status_code == 401


def test_job_runs_lists_only_latest_fifty(client):
    from sqlalchemy.orm import Session

    now = datetime.now(timezone.utc)
    with Session(client.app.state.engine) as session:
        batch = Batch(content_hash="a" * 64, source="test")
        session.add(batch)
        session.flush()
        for index in range(55):
            session.add(Job(
                external_id=f"run-{index}", batch_id=batch.id,
                content_hash=f"{index:064d}", payload={"title": f"Run {index}"},
                created_at=now + timedelta(seconds=index),
            ))
        session.commit()

    runs = client.get("/job-runs", headers=JOB_RUNS_HEADERS).json()
    assert len(runs) == 50
    assert runs[0]["title"] == "Run 54"
    assert runs[-1]["title"] == "Run 5"


def test_job_runs_retry_regenerate_and_signed_resume_link(client, monkeypatch):
    from sqlalchemy.orm import Session

    payload = json.loads((ROOT / "examples/job-envelope.json").read_text())
    job_id = client.post("/batches", json=payload, headers=HEADERS).json()["job_ids"][0]
    with Session(client.app.state.engine) as session:
        job = session.get(Job, job_id)
        job.status = "failed"
        session.commit()

    listed = client.get("/job-runs", headers=JOB_RUNS_HEADERS).json()[0]
    assert listed["can_retry"] is True and listed["can_regenerate"] is False
    route = f"/job-runs/{job_id}"
    assert client.post(f"{route}/retry", headers=HEADERS).status_code == 401
    retried = client.post(f"{route}/retry", headers=JOB_RUNS_HEADERS)
    assert retried.status_code == 202 and retried.json()["status"] == "queued"

    with Session(client.app.state.engine) as session:
        job = session.get(Job, job_id)
        job.status = "needs_review"
        session.add(GeneratedDocument(job_id=job_id, package={"saved": "checkpoint"}))
        session.add(Artifact(job_id=job_id, kind="resume", filename="Candidate-Role-Company-Resume.docx",
                             backend="supabase", bucket="private-artifacts",
                             object_key="Candidate-Role-Company-Resume.docx"))
        session.commit()

    monkeypatch.setattr(
        "app.storage.SupabaseStorage.signed_download_url",
        lambda self, key, expires_in: f"https://storage.example/signed/{key}?ttl={expires_in}",
    )
    listed = client.get("/job-runs", headers=JOB_RUNS_HEADERS).json()[0]
    assert listed["resume_available"] is True and listed["can_regenerate"] is True
    assert client.get(f"{route}/resume-url", headers=HEADERS).status_code == 401
    download = client.get(f"{route}/resume-url", headers=JOB_RUNS_HEADERS)
    assert download.status_code == 200
    assert download.json() == {
        "url": "https://storage.example/signed/Candidate-Role-Company-Resume.docx?ttl=300",
        "expires_in": 300,
    }
    assert client.post(f"{route}/regenerate", headers=JOB_RUNS_HEADERS).status_code == 400
    regenerated = client.post(f"{route}/regenerate?confirm_paid_generation=true",
                              headers=JOB_RUNS_HEADERS)
    assert regenerated.status_code == 202 and regenerated.json()["status"] == "queued"

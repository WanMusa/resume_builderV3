import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import init_db
from app.main import MAX_UPLOAD_BYTES, create_app
from app.models import Artifact, GeneratedDocument, Job

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer" + " " + "test-token"}


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/api.db",
                        api_token="test-token", data_dir=tmp_path)
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

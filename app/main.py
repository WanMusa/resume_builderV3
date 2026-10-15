import json
import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.importer import EnvelopeValidationError, import_envelope
from app.models import Batch, Job
from app.worker import retry_job

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if not settings.api_token and not settings.allow_unauthenticated:
        raise RuntimeError("Set API_TOKEN, or explicitly enable ALLOW_UNAUTHENTICATED for local dev")
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="Resume Builder V3", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.session_factory = factory
    app.state.engine = engine

    def authenticated(request: Request):
        if settings.api_token:
            expected = "Bearer" + " " + settings.api_token
            if not secrets.compare_digest(request.headers.get("authorization", "").encode(),
                                          expected.encode()):
                raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    def session():
        with factory() as db:
            yield db

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.get("/health")
    def health(db=Depends(session)):
        db.execute(text("SELECT 1"))
        return {"status": "ok"}

    router = APIRouter(dependencies=[Depends(authenticated)])

    def batch_view(batch):
        return {"id": batch.id, "status": batch.status, "source": batch.source,
                "created_at": batch.created_at, "errors": batch.errors,
                "job_ids": [job.id for job in batch.jobs]}

    def job_view(job):
        return {"id": job.id, "external_id": job.external_id, "batch_id": job.batch_id,
                "title": job.payload.get("title"), "company": job.payload.get("company"),
                "status": job.status, "attempts": job.attempts, "error": job.error,
                "created_at": job.created_at, "updated_at": job.updated_at,
                "completed_at": job.completed_at}

    @router.post("/batches", status_code=201)
    async def upload(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Send the JSON envelope as application/json")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "JSON envelope exceeds 2 MiB")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid JSON envelope") from None

        def persist():
            with factory() as db:
                try:
                    batch = import_envelope(db, payload, "http")
                    db.commit()
                    return jsonable_encoder(batch_view(batch))
                except EnvelopeValidationError as exc:
                    raise HTTPException(422, detail=exc.errors) from None

        return await run_in_threadpool(persist)

    @router.get("/batches")
    def batches(limit: int = 50, offset: int = 0, db=Depends(session)):
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(422, "limit must be 1–200 and offset nonnegative")
        return [batch_view(b) for b in db.scalars(
            select(Batch).order_by(Batch.created_at.desc()).offset(offset).limit(limit))]

    @router.get("/batches/{batch_id}")
    def batch_detail(batch_id: str, db=Depends(session)):
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise HTTPException(404, "Batch not found")
        return batch_view(batch)

    @router.get("/jobs")
    def jobs(status: str | None = None, limit: int = 50, offset: int = 0, db=Depends(session)):
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(422, "limit must be 1–200 and offset nonnegative")
        query = select(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit)
        if status:
            query = query.where(Job.status == status)
        return [job_view(j) for j in db.scalars(query)]

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str, db=Depends(session)):
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return {
            **job_view(job), "input": job.payload,
            "generated": job.document.package if job.document else None,
            "artifacts": [{"id": a.id, "kind": a.kind, "backend": a.backend,
                           "bucket": a.bucket, "object_key": a.object_key,
                           "local_path": a.local_path} for a in job.artifacts],
        }

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, db=Depends(session)):
        try:
            job = retry_job(db, job_id)
        except LookupError:
            raise HTTPException(404, "Job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        db.commit()
        return job_view(job)

    app.include_router(router)
    return app

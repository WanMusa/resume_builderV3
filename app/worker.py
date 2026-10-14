from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from app.models import Artifact, GeneratedDocument, Job, new_id, utcnow


ACTIVE_STATES = ("generating", "rendered", "uploaded")


class LostLease(RuntimeError):
    pass


def _eligible(now):
    return or_(
        and_(Job.status == "queued", or_(Job.lease_until.is_(None), Job.lease_until <= now)),
        and_(Job.status.in_(ACTIVE_STATES), Job.lease_until <= now),
    )


def claim_job(session_factory, settings) -> dict | None:
    """Claim in a short transaction; PostgreSQL workers skip locked candidates."""
    with session_factory() as session:
        now = utcnow()
        session.execute(update(Job).execution_options(synchronize_session=False).where(
            _eligible(now), Job.attempts >= settings.max_attempts,
        ).values(status="failed", error="Maximum attempts exhausted", lease_token=None, lease_until=None))
        candidate = session.scalar(
            select(Job).where(_eligible(now), Job.attempts < settings.max_attempts)
            .order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True)
        )
        if candidate is None:
            session.commit()
            return None
        token = new_id()
        result = session.execute(update(Job).execution_options(synchronize_session=False).where(
            Job.id == candidate.id, _eligible(now), Job.attempts < settings.max_attempts,
        ).values(
            status="generating", attempts=Job.attempts + 1, error=None,
            lease_token=token, lease_until=now + timedelta(seconds=settings.lease_seconds),
            updated_at=now,
        ))
        if result.rowcount != 1:
            session.rollback()
            return None
        job_id, payload = candidate.id, candidate.payload
        session.commit()
        return {"id": job_id, "payload": payload, "token": token}


def _fence(session, job_id, token, settings, **values):
    now = utcnow()
    values.update(updated_at=now, lease_until=now + timedelta(seconds=settings.lease_seconds))
    result = session.execute(update(Job).execution_options(synchronize_session=False).where(
        Job.id == job_id, Job.lease_token == token, Job.lease_until > now,
        Job.status.in_(ACTIVE_STATES),
    ).values(**values))
    if result.rowcount != 1:
        raise LostLease("Worker no longer owns the job lease")


def retry_job(session, job_id: str) -> Job:
    """Retry terminal failures without discarding an existing generated checkpoint."""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError("Job not found")
    result = session.execute(update(Job).where(
        Job.id == job_id, Job.status.in_(("failed", "needs_review")),
    ).values(status="queued", attempts=0, error=None, lease_until=None, lease_token=None, completed_at=None))
    if result.rowcount != 1:
        raise ValueError("Only failed or needs_review jobs can be retried")
    session.refresh(job)
    return job


def process_one(session_factory, settings) -> bool:
    # Lazy imports allow intake and API use without renderer/provider initialization.
    from app.generation import generate_package
    from app.rendering import render_package
    from app.storage import get_storage

    claimed = claim_job(session_factory, settings)
    if claimed is None:
        return False
    job_id, token = claimed["id"], claimed["token"]
    try:
        with session_factory() as session:
            document = session.scalar(select(GeneratedDocument).where(GeneratedDocument.job_id == job_id))
            package = document.package if document else None
        if package is None:
            package = generate_package(claimed["payload"], settings)
            if not isinstance(package, dict) or not all(key in package for key in ("resume", "cover_letter", "provenance")):
                raise ValueError("Generated package requires resume, cover_letter, and provenance")
            with session_factory() as session:
                _fence(session, job_id, token, settings)
                session.add(GeneratedDocument(job_id=job_id, package=package))
                session.commit()

        # Lease-specific paths prevent an expired renderer overwriting its successor.
        output_dir = Path(settings.data_dir) / job_id / token
        output_dir.mkdir(parents=True, exist_ok=True)
        with session_factory() as session:
            _fence(session, job_id, token, settings)
            session.commit()
        paths = render_package(package, output_dir)
        if not paths:
            raise ValueError("Renderer produced no artifacts")
        with session_factory() as session:
            _fence(session, job_id, token, settings, status="rendered")
            existing = {artifact.filename for artifact in session.scalars(select(Artifact).where(Artifact.job_id == job_id))}
            session.commit()
        storage = get_storage(settings)
        for raw_path in paths:
            path = Path(raw_path)
            if path.name in existing:
                continue
            with session_factory() as session:
                _fence(session, job_id, token, settings)
                session.commit()
            metadata = storage.upload(path, f"{job_id}/{token}/{path.name}")
            with session_factory() as session:
                _fence(session, job_id, token, settings)
                session.add(Artifact(
                    job_id=job_id, kind=path.stem, filename=path.name,
                    backend=metadata["backend"], bucket=metadata.get("bucket"),
                    object_key=metadata["object_key"], local_path=metadata.get("local_path"),
                ))
                session.commit()
        with session_factory() as session:
            _fence(session, job_id, token, settings, status="uploaded")
            session.commit()
        with session_factory() as session:
            final_status = "needs_review" if package.get("review_required") else "completed"
            _fence(session, job_id, token, settings, status=final_status, completed_at=utcnow())
            session.execute(update(Job).where(Job.id == job_id, Job.lease_token == token).values(
                lease_until=None, lease_token=None,
            ))
            session.commit()
    except LostLease:
        pass
    except Exception as exc:
        with session_factory() as session:
            try:
                _fence(session, job_id, token, settings)
                job = session.get(Job, job_id)
                job.status = "failed" if job.attempts >= settings.max_attempts else "queued"
                job.error = f"{type(exc).__name__}: {exc}"
                job.lease_until = None
                job.lease_token = None
                session.commit()
            except LostLease:
                session.rollback()
    return True

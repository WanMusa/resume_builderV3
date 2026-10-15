import hashlib
import json
import unicodedata
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.contracts import EnvelopeContract, JobContract
from app.models import Batch, BatchJob, Job, JobInput, new_id, utcnow


class EnvelopeValidationError(ValueError):
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(json.dumps(errors, ensure_ascii=False))


def _errors(exc: ValidationError) -> list:
    return exc.errors(include_url=False, include_context=False, include_input=False)


def _normalize(value):
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).split())
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: item if key == "id" else _normalize(item) for key, item in value.items()}
    return value


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(_normalize(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _insert_ignore(session, model, values, columns):
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=columns))


def import_envelope(session, payload: dict, source: str) -> Batch:
    """Import inside the caller's transaction; the caller commits or rolls back."""
    try:
        envelope = EnvelopeContract.model_validate(payload)
    except ValidationError as exc:
        raise EnvelopeValidationError(_errors(exc)) from exc
    valid, errors = [], []
    for index, raw in enumerate(envelope.jobs):
        try:
            valid.append(JobContract.model_validate(raw).model_dump())
        except ValidationError as exc:
            errors.append({"index": index, "errors": _errors(exc)})
    if not valid:
        raise EnvelopeValidationError(errors)

    content_hash = _hash(envelope.model_dump())
    batch_id = new_id()
    _insert_ignore(session, Batch, {
        "id": batch_id, "content_hash": content_hash, "source": source,
        "status": "partial" if errors else "accepted", "errors": errors, "created_at": utcnow(),
    }, ["content_hash"])
    batch = session.scalar(select(Batch).where(Batch.content_hash == content_hash))
    if batch.id != batch_id:
        return batch
    # Consistent uniqueness-key ordering avoids cross-batch insertion deadlocks.
    revisions = []
    for job in valid:
        # Capture timestamps describe intake, not a new version of the job content.
        revision = {key: value for key, value in job.items() if key not in {"id", "captured_at"}}
        revisions.append((job["id"], _hash(revision), job))
    for external_id, revision_hash, job in sorted(revisions, key=lambda item: (item[0], item[1])):
        _insert_ignore(session, Job, {
            "id": new_id(), "external_id": external_id, "batch_id": batch.id,
            "content_hash": revision_hash, "payload": job, "status": "queued",
            "attempts": 0, "created_at": utcnow(), "updated_at": utcnow(),
        }, ["external_id", "content_hash"])
        job_id = session.scalar(select(Job.id).where(
            Job.external_id == external_id, Job.content_hash == revision_hash,
        ))
        _insert_ignore(session, JobInput, {
            "id": new_id(), "job_id": job_id, "payload": job, "created_at": utcnow(),
        }, ["job_id"])
        _insert_ignore(session, BatchJob, {
            "batch_id": batch.id, "job_id": job_id,
        }, ["batch_id", "job_id"])
    session.flush()
    return batch


def scan_inbox(session_factory, settings) -> list[dict]:
    """Only consume *.json; producers must atomically rename completed *.part files.

    Database commits precede archival. A crash between these steps is safe because
    the next scan deduplicates the committed batch.
    """
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    archive, quarantine = inbox / "archive", inbox / "quarantine"
    archive.mkdir(exist_ok=True)
    quarantine.mkdir(exist_ok=True)
    results = []
    for path in sorted(inbox.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            with session_factory() as session:
                batch = import_envelope(session, payload, str(path))
                session.commit()
                result = {"file": str(path), "batch_id": batch.id, "status": batch.status, "errors": batch.errors}
            destination = archive / path.name
        except (EnvelopeValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors = exc.errors if isinstance(exc, EnvelopeValidationError) else [
                {"message": "Invalid JSON file" if isinstance(exc, json.JSONDecodeError) else "JSON file is not valid UTF-8"}
            ]
            result = {"file": str(path), "status": "quarantined", "errors": errors}
            destination = quarantine / path.name
        except OSError:
            continue
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}-{new_id()}{destination.suffix}")
        try:
            path.rename(destination)
        except FileNotFoundError:
            continue
        if result["status"] == "quarantined":
            destination.with_suffix(".errors.json").write_text(
                json.dumps(result["errors"], ensure_ascii=False, indent=2), encoding="utf-8",
            )
        results.append(result)
    return results

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    errors: Mapped[list] = mapped_column(JSON_TYPE, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    jobs: Mapped[list["Job"]] = relationship(secondary="batch_jobs", back_populates="batches")


class BatchJob(Base):
    __tablename__ = "batch_jobs"

    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("external_id", "content_hash", name="uq_job_revision"),
        Index("ix_jobs_claim", "status", "lease_until", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(Text)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    batch: Mapped[Batch] = relationship()
    batches: Mapped[list[Batch]] = relationship(secondary="batch_jobs", back_populates="jobs")
    document: Mapped["GeneratedDocument | None"] = relationship(back_populates="job", uselist=False)
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="job")


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    package: Mapped[dict] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job: Mapped[Job] = relationship(back_populates="document")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("job_id", "filename", name="uq_job_artifact"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    kind: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(String(24))
    bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job: Mapped[Job] = relationship(back_populates="artifacts")

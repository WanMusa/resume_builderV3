from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Base


def make_engine(settings: Settings):
    kwargs = {"pool_pre_ping": True}
    backend = make_url(settings.database_url).get_backend_name()
    if backend == "postgresql" and settings.database_password:
        kwargs["connect_args"] = {"password": settings.database_password}
    if backend == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(settings.database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)

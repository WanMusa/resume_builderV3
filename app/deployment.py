"""Secret-free, non-inference deployment checks run inside the application image."""

import json
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import make_engine
from app.generation import validate_base
from app.models import Base


def check_configuration(settings: Settings) -> None:
    if settings.allow_unauthenticated or not settings.api_token or not settings.api_token.strip():
        raise ValueError("Production requires API_TOKEN and ALLOW_UNAUTHENTICATED=false")
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql" or not url.username or not url.database:
        raise ValueError("Production requires a dedicated PostgreSQL role and database")
    if not settings.database_password and not url.password:
        raise ValueError("Set the dedicated DATABASE_PASSWORD")
    if settings.storage_backend == "supabase" and not all(
        (settings.supabase_url, settings.supabase_key, settings.supabase_bucket)
    ):
        raise ValueError("Configure Supabase storage credentials and bucket")
    for name, expected in (("data_dir", "/app/data"), ("inbox_dir", "/app/inbox/json"),
                           ("assets_dir", "/app/assets"), ("prompts_dir", "/app/prompts")):
        if getattr(settings, name) != Path(expected):
            raise ValueError("Deployment paths must match the persistent Compose mounts")


def check_files(settings: Settings) -> None:
    base = json.loads((settings.assets_dir / "base_resume.json").read_text())
    validate_base(base)
    for kind in ("resume", "cover_letter"):
        for suffix in ("system", "user"):
            if not (settings.prompts_dir / f"{kind}_{suffix}.txt").read_text().strip():
                raise ValueError("Required prompt is empty")
    # Check as the image's real UID, including existing nested output/archive dirs.
    for root in (settings.data_dir, settings.inbox_dir.parent):
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("Provision bind directories for UID 10001")
        for directory, dirs, files in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs + files):
                raise ValueError("Runtime directories must not contain symlinks")
            if any(not os.access(os.path.join(directory, name), os.R_OK | os.W_OK | os.X_OK) for name in dirs):
                raise ValueError("Runtime directories must be readable/writable by UID 10001")
            with tempfile.TemporaryFile(dir=directory):
                pass
            # Existing inputs are read then renamed; outputs use new lease paths.
            # Git's deployment-user-owned .gitkeep files need no write access.
            if any(not os.access(os.path.join(directory, name), os.R_OK) for name in files):
                raise ValueError("Existing runtime files must be readable by UID 10001")
    if not settings.inbox_dir.is_dir():
        raise ValueError("Provision the JSON inbox directory")


def check_database(settings: Settings, ready: bool = False) -> None:
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            # Enforce role isolation and schema ownership without provisioning infra.
            row = connection.execute(text("""
                SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                       r.rolreplication, r.rolbypassrls,
                       d.datdba = r.oid,
                       has_schema_privilege(current_user, current_schema(), 'CREATE'),
                       has_schema_privilege(current_user, current_schema(), 'USAGE')
                FROM pg_roles r JOIN pg_database d ON d.datname = current_database()
                WHERE r.rolname = current_user
            """)).one()
            if any(row[:5]) or not all(row[5:]):
                raise ValueError("Dedicated non-admin database owner with schema access required")
            inspector = inspect(connection)
            for table in Base.metadata.sorted_tables:
                if not inspector.has_table(table.name):
                    if ready:
                        raise ValueError("Application schema is not bootstrapped")
                    continue
                permissions = connection.execute(text("""
                    SELECT bool_and(has_table_privilege(current_user, :table, privilege))
                    FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p(privilege)
                """), {"table": table.name}).scalar_one()
                if not permissions:
                    raise ValueError("Application tables require read/write privileges")
                connection.execute(select(table).limit(0))
    finally:
        engine.dispose()


def main() -> int:
    try:
        settings = Settings()
        check_configuration(settings)
        check_files(settings)
        check_database(settings, ready=sys.argv[1:] == ["--ready"])
    except Exception:
        # Validation/driver exceptions can include env values or connection URLs.
        print("Deployment check failed; privately verify configuration, files and dedicated database permissions.", file=sys.stderr)
        return 1
    print("Deployment checks passed (no inference or imports performed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

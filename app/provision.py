"""Emit dedicated app database bootstrap SQL for the local PostgreSQL administrator."""

import sys

from psycopg import sql
from sqlalchemy.engine import make_url

from app.config import Settings


def bootstrap_sql(settings: Settings) -> str:
    url = make_url(settings.database_url)
    if (url.get_backend_name() != "postgresql" or url.host != "postgres"
            or url.username != "resume_builder" or url.database != "resume_builder"
            or url.port not in (None, 5432)):
        raise ValueError("Automatic provisioning requires the dedicated resume_builder database on postgres")
    password = settings.database_password or url.password
    if not password:
        raise ValueError("DATABASE_PASSWORD is required for database provisioning")
    return r"""
\set ON_ERROR_STOP on
SET standard_conforming_strings = on;
DO $bootstrap$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'resume_builder'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Refusing to adopt a privileged application role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_database
        WHERE datname = 'resume_builder'
        AND pg_get_userbyid(datdba) <> 'resume_builder'
    ) THEN
        RAISE EXCEPTION 'Refusing to adopt a database owned by another role';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resume_builder') THEN
        CREATE ROLE resume_builder LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$bootstrap$;
""" + sql.SQL("ALTER ROLE resume_builder WITH LOGIN PASSWORD {};\n").format(
        sql.Literal(password)
    ).as_string() + r"""
SELECT 'CREATE DATABASE resume_builder OWNER resume_builder'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'resume_builder')
\gexec
\connect resume_builder
GRANT USAGE, CREATE ON SCHEMA public TO resume_builder;
"""


if __name__ == "__main__":
    try:
        statement = bootstrap_sql(Settings())
    except Exception:
        print("Database bootstrap requires dedicated resume_builder settings and DATABASE_PASSWORD.",
              file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(statement)

#!/bin/sh
# Runs once when the postgres volume is first created (docker-entrypoint-initdb.d).
# The OWNER role ($POSTGRES_USER) runs migrations; the app connects as APP_DB_USER, a
# NON-superuser without BYPASSRLS, so row-level security is enforced (CLAUDE.md section 2).
# Mirrors Azure Database for PostgreSQL Flexible Server, where the admin is not a superuser
# and `vector` must be allow-listed (azure.extensions = VECTOR).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS vector;
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
    CREATE ROLE "${APP_DB_USER}" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '${APP_DB_PASSWORD}';
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO "${APP_DB_USER}";
GRANT USAGE ON SCHEMA public TO "${APP_DB_USER}";
-- Tables / sequences the owner creates later (Alembic) are granted automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE "$POSTGRES_USER" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "${APP_DB_USER}";
ALTER DEFAULT PRIVILEGES FOR ROLE "$POSTGRES_USER" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO "${APP_DB_USER}";
SQL

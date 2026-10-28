"""Row-level security helpers shared by every migration that creates a tenant table."""

from __future__ import annotations

from typing import Protocol

# One statement per entry: asyncpg executes migrations as prepared statements.
HELPER_FUNCTIONS_SQL = (
    """CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
    LANGUAGE sql STABLE AS $$
        SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
    $$""",
    """CREATE OR REPLACE FUNCTION app_is_system() RETURNS boolean
    LANGUAGE sql STABLE AS $$
        SELECT coalesce(current_setting('app.system', true), '') = 'on'
    $$""",
)

DROP_HELPER_FUNCTIONS_SQL = (
    "DROP FUNCTION IF EXISTS app_is_system()",
    "DROP FUNCTION IF EXISTS app_current_user_id()",
)


class _Op(Protocol):
    def execute(self, sqltext: str) -> object: ...


def tenant_rls_sql(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""CREATE POLICY tenant_isolation ON {table}
            USING (user_id = app_current_user_id() OR app_is_system())
            WITH CHECK (user_id = app_current_user_id() OR app_is_system())""",
    ]


def enable_tenant_rls(op: _Op, table: str) -> None:
    for statement in tenant_rls_sql(table):
        op.execute(statement)


def disable_tenant_rls(op: _Op, table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

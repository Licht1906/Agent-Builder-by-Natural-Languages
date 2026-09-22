"""uc1 ar data surface

Revision ID: a4c2c2363959
Revises: 4d604eb640fb
Create Date: 2026-08-14 09:05:00.000000

UC1-14 (#115), spec.md §9/§10.4, AC-6/AC-7: applies
`docs/usecase_spec/uc1-ar-assistant/schema.sql` for real — the DDL was already
written (§12.1's package layout names it as a first-class artifact, not
scratch work), so this migration executes that file verbatim rather than
re-deriving an equivalent set of `op.create_table`/`op.execute` calls that
could drift from the file everyone actually reads and reviews. `scripts/verify_uc1_data_surface.py`
is what turns the file's own manual-verification comment (§10.4's `SET LOCAL
ROLE agt_ar_assistant_ro; SELECT * FROM fin_invoice;`) into something CI runs
after every migration.

`schema.sql` is written to be safely re-run (`CREATE TABLE IF NOT EXISTS`,
`CREATE OR REPLACE VIEW`, `DROP POLICY IF EXISTS` before `CREATE POLICY`,
a `DO $$ ... $$` guard around the role's `CREATE ROLE`), so executing it
verbatim as a single migration step is safe.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c2c2363959"
down_revision: str | Sequence[str] | None = "4d604eb640fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `alembic/versions/<this file>` -> repo root is three `.parent`s up.
_SCHEMA_SQL = (
    Path(__file__).resolve().parents[2] / "docs" / "usecase_spec" / "uc1-ar-assistant" / "schema.sql"
)


def upgrade() -> None:
    op.execute(_SCHEMA_SQL.read_text())


def downgrade() -> None:
    """Reverse every object `schema.sql` creates, in dependency order.

    Hand-written rather than derived from the file: raw SQL executed via
    `op.execute` has no reflection Alembic can autogenerate a reversal from,
    so this is the one place in this migration that has to know `schema.sql`'s
    contents rather than just replaying them.
    """
    op.execute("DROP VIEW IF EXISTS v_audit_integrity")
    op.execute("DROP TABLE IF EXISTS audit_log")  # trigger + both rules go with it
    op.execute("DROP FUNCTION IF EXISTS audit_chain()")

    op.execute("DROP VIEW IF EXISTS v_customer_safe")
    op.execute("DROP VIEW IF EXISTS v_ar_open")

    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agt_ar_assistant_ro")
    op.execute("REVOKE ALL ON SCHEMA public FROM agt_ar_assistant_ro")
    op.execute("DROP ROLE IF EXISTS agt_ar_assistant_ro")

    op.execute("DROP TABLE IF EXISTS fin_invoice")
    # `crm_sales_assignment`'s RLS policy (`p_assignment_scope`) reads
    # `app_scope()`/`app_principal_id()`/`app_department()` — the table (and
    # with it, the policy) has to go before the functions it depends on.
    op.execute("DROP TABLE IF EXISTS crm_sales_assignment")

    op.execute("DROP FUNCTION IF EXISTS app_scope()")
    op.execute("DROP FUNCTION IF EXISTS app_department()")
    op.execute("DROP FUNCTION IF EXISTS app_principal_id()")

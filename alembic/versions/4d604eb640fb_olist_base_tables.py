"""olist base tables

Revision ID: 4d604eb640fb
Revises: 9602748a105a
Create Date: 2026-08-14 09:00:00.000000

UC1-14 (#115), spec.md §9: `docs/usecase_spec/uc1-ar-assistant/schema.sql` builds
the AR assistant's data surface (`fin_invoice`, `crm_sales_assignment`, the
least-privilege views) on top of the Olist e-commerce dataset's `orders` and
`customers` tables — schema.sql's own FKs (`fin_invoice.order_id REFERENCES
orders`, `crm_sales_assignment.customer_id REFERENCES customers`) assume they
already exist. This repo has never had them; this migration adds the minimal
faithful subset of the real Olist schema (same table/column names as the
public dataset) that schema.sql and the demo seed script (UC1-15, #116)
actually touch — not the full ~9-table Olist dataset, which nothing here reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d604eb640fb"
down_revision: str | Sequence[str] | None = "9602748a105a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("customer_unique_id", sa.Text(), nullable=False),
        sa.Column("customer_zip_code_prefix", sa.Text(), nullable=True),
        sa.Column("customer_city", sa.Text(), nullable=False),
        sa.Column("customer_state", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("customer_id"),
    )

    op.create_table(
        "orders",
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("order_status", sa.Text(), nullable=False),
        sa.Column("order_purchase_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_delivered_carrier_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_delivered_customer_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_estimated_delivery_date", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("order_id"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.customer_id"]),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_table("orders")
    op.drop_table("customers")

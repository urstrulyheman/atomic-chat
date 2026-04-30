"""ledger idempotency key

Revision ID: 20260429_0017
Revises: 20260429_0016
Create Date: 2026-04-29 00:17:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0017"
down_revision = "20260429_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledger_transactions", sa.Column("idempotency_key", sa.String(length=120), nullable=True))
    op.create_index(
        "uq_ledger_transactions_from_type_idempotency_key",
        "ledger_transactions",
        ["from_wallet_id", "transaction_type", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_ledger_transactions_from_type_idempotency_key", table_name="ledger_transactions")
    op.drop_column("ledger_transactions", "idempotency_key")

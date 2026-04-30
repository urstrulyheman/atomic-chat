"""settlement hashes

Revision ID: 20260429_0011
Revises: 20260429_0010
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260429_0011"
down_revision: Union[str, Sequence[str], None] = "20260429_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

uuid_type = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "settlement_hashes",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("settlement_date", sa.Date(), nullable=False, unique=True),
        sa.Column("ledger_hash", sa.String(64), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(30), nullable=False, server_default="generated"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_settlement_hashes_status_created_at", "settlement_hashes", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_settlement_hashes_status_created_at", table_name="settlement_hashes")
    op.drop_table("settlement_hashes")

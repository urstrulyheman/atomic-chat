"""idempotency keys

Revision ID: 20260428_0002
Revises: 20260428_0001
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260428_0002"
down_revision: Union[str, Sequence[str], None] = "20260428_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("idempotency_key", sa.String(120), nullable=True))
    op.create_index("uq_messages_sender_idempotency_key", "messages", ["sender_id", "idempotency_key"], unique=True)
    op.add_column("payment_orders", sa.Column("gateway_payment_id", sa.String(255), nullable=True))
    op.create_index("uq_payment_orders_gateway_payment_id", "payment_orders", ["gateway_payment_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_payment_orders_gateway_payment_id", table_name="payment_orders")
    op.drop_column("payment_orders", "gateway_payment_id")
    op.drop_index("uq_messages_sender_idempotency_key", table_name="messages")
    op.drop_column("messages", "idempotency_key")

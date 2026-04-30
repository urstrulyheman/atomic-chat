"""unique payment gateway order

Revision ID: 20260429_0014
Revises: 20260429_0013
Create Date: 2026-04-29 00:14:00.000000
"""

from alembic import op


revision = "20260429_0014"
down_revision = "20260429_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_payment_orders_gateway_order_id", "payment_orders", ["gateway_order_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_payment_orders_gateway_order_id", table_name="payment_orders")

"""ledger check constraints

Revision ID: 20260428_0006
Revises: 20260428_0005
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260428_0006"
down_revision: Union[str, Sequence[str], None] = "20260428_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


constraints = [
    ("wallets", "ck_wallets_purchased_balance_non_negative", "purchased_balance >= 0"),
    ("wallets", "ck_wallets_earned_balance_non_negative", "earned_balance >= 0"),
    ("wallets", "ck_wallets_locked_balance_non_negative", "locked_balance >= 0"),
    ("wallets", "ck_wallets_gas_paid_total_non_negative", "gas_paid_total >= 0"),
    ("wallets", "ck_wallets_reward_earned_total_non_negative", "reward_earned_total >= 0"),
    ("messages", "ck_messages_coin_cost_non_negative", "coin_cost >= 0"),
    ("ledger_transactions", "ck_ledger_transactions_gross_amount_positive", "gross_amount > 0"),
    ("ledger_transactions", "ck_ledger_transactions_platform_gas_non_negative", "platform_gas >= 0"),
    ("ledger_transactions", "ck_ledger_transactions_receiver_reward_non_negative", "receiver_reward >= 0"),
    ("ledger_transactions", "ck_ledger_transactions_reserve_amount_non_negative", "reserve_amount >= 0"),
    ("wallet_entries", "ck_wallet_entries_entry_type_valid", "entry_type IN ('DEBIT', 'CREDIT')"),
    ("wallet_entries", "ck_wallet_entries_amount_positive", "amount > 0"),
    ("payment_orders", "ck_payment_orders_amount_positive", "amount > 0"),
    ("payment_orders", "ck_payment_orders_coins_to_credit_positive", "coins_to_credit > 0"),
    ("reward_events", "ck_reward_events_base_reward_non_negative", "base_reward >= 0"),
    ("reward_events", "ck_reward_events_final_reward_non_negative", "final_reward >= 0"),
    ("reward_events", "ck_reward_events_trust_multiplier_non_negative", "trust_multiplier >= 0"),
    ("reward_events", "ck_reward_events_fraud_multiplier_non_negative", "fraud_multiplier >= 0"),
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, constraint_name, condition in constraints:
        op.create_check_constraint(constraint_name, table_name, condition)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, constraint_name, _condition in reversed(constraints):
        op.drop_constraint(constraint_name, table_name, type_="check")

# Orca Chat Coin MVP Backend

FastAPI prototype for the Orca Chat Coin economy loop:

- Phone OTP login with dev OTP `123456`
- User profile and automatic wallet creation
- Welcome bonus coin credit
- 1-to-1 conversations and paid messages
- Sender coin deduction from purchased balance first, then earned balance
- Receiver locked reward
- Platform gas and reserve wallets
- Razorpay order creation, webhook handling, and dev capture route
- Wallet balance, ledger entries, transaction history
- Basic fraud flags for self-messaging, velocity, duplicate content, inactive receivers
- Admin metrics for users, messages, payments, gas, locked coins, fraud
- WebSocket endpoint at `/ws/chat?token=JWT`
- Paid-message wallet rows are locked with `SELECT ... FOR UPDATE` on PostgreSQL before debits and rewards

## Run Locally

```bash
cd orca-chat-backend
cp .env.example .env
docker compose up --build
```

API docs:

```text
http://localhost:8000/docs
```

## Local Dev Without Docker

```bash
cd orca-chat-backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Without a `.env`, the app uses SQLite at `./orca_chat_dev.db`.

## Migrations

Alembic is configured for production schema changes:

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe change"
```

For production deploys, run migrations explicitly before starting the API and keep automatic table creation disabled:

```env
ENV=production
AUTO_CREATE_TABLES=false
JWT_SECRET=replace_with_a_32_plus_character_random_secret
CORS_ORIGINS=https://app.yourdomain.com
ALLOWED_HOSTS=api.yourdomain.com
OTP_PROVIDER=msg91
```

For local MVP speed, omit that setting or set `AUTO_CREATE_TABLES=true` to let the API create tables on startup.

## OTP Provider

Development uses the built-in `dev` provider, which always returns OTP `123456`:

```env
OTP_PROVIDER=dev
```

Production should switch to a real SMS provider through the provider abstraction in `app/modules/auth/otp_provider.py`:

```env
OTP_PROVIDER=msg91
MSG91_AUTH_KEY=your_key
MSG91_TEMPLATE_ID=your_template
MSG91_SENDER_ID=ORCACH
```

The MSG91 class is intentionally a network integration point in this MVP; wire the provider API call there before production launch.

## Demo Flow

1. `POST /auth/send-otp`
2. `POST /auth/verify-otp` with OTP `123456`
3. Repeat for a second user
4. `POST /chats`
5. `POST /chats/{chat_id}/messages`
6. `GET /wallet/balance` for both users
7. `GET /admin/metrics`
8. `POST /payments/razorpay/order`
9. `POST /payments/dev/capture` for local demos

## Notes

This MVP intentionally uses an internal ledger only. Public tokens, withdrawals, mining, groups, and voice/video are out of scope until the economy loop is validated.

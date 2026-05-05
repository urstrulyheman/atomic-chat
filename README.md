# Orca Chat Coin MVP

Orca Chat Coin is a WhatsApp-like chat prototype with an internal coin economy. Users can log in by phone OTP, create a profile, chat 1-to-1, recharge a wallet, spend Orca Coins on paid messages, earn locked rewards for valid received messages, and let the platform collect gas fees.

The MVP proves one core question:

Can a chat app create a micro-economy where users spend, earn, and the platform collects gas fees?

## Repository Structure

```text
Atomic-chat/
+-- orca-chat-backend/   FastAPI backend, ledger, auth, wallet, chat, payments, admin
+-- orca-chat-ui/        React/Vite MVP UI wired to the backend
+-- README.md            Project-level build log and production roadmap
```

## MVP Scope

Built or scaffolded in this repo:

- Phone OTP login with dev OTP flow
- User profile creation and update
- Wallet creation with welcome bonus
- Wallet balances: purchased, earned, locked, spendable
- 1-to-1 conversations
- WebSocket real-time chat endpoint
- Token-unit paid message spend flow
- Receiver reward and reward lock period
- Platform gas and reserve accounting
- Double-entry ledger entries
- Razorpay order creation, webhook handling, and local dev capture
- Payment history and wallet transaction history
- Basic fraud rules for velocity, duplicates, blocked users, reports, and reward caps
- Admin APIs for users, wallets, payments, messages, ledger, fraud, sessions, OTP, settlements
- Daily settlement hash generation
- React UI for login, contacts, chat, wallet, recharge, transactions, and admin metrics

## Tech Stack

- Backend: FastAPI
- Database: PostgreSQL-ready SQLAlchemy models and Alembic migrations
- Local dev database: SQLite fallback
- Realtime: FastAPI WebSocket
- Payments: Razorpay integration path plus dev capture route
- Frontend: React + Vite
- Tests: Pytest MVP flow coverage

## Run Backend

```bash
cd orca-chat-backend
cp .env.example .env
docker compose up --build
```

API docs:

```text
http://localhost:8000/docs
```

Local Python run:

```bash
cd orca-chat-backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run Frontend

```bash
cd orca-chat-ui
npm install
npm run dev -- --port 5173
```

Open:

```text
http://127.0.0.1:5173
```

## Demo Flow

1. User A logs in with phone OTP.
2. User A gets welcome bonus Orca Coins.
3. User B logs in.
4. User A discovers User B.
5. User A starts a 1-to-1 chat.
6. User A sends a paid message.
7. User A wallet balance decreases.
8. User B earns locked rewards.
9. Platform gas wallet increases.
10. Admin dashboard metrics update.
11. User A creates a Razorpay recharge order.
12. Local dev capture credits purchased coins.

Investor demo message:

```text
Orca Chat turns communication into an economy.
```

## Build Chat Log

This is the condensed working log from the MVP build and hardening conversation.

### Initial Build

- Created FastAPI backend structure for auth, users, chat, wallet, payments, rewards, fraud, and admin modules.
- Created SQLAlchemy models for users, wallets, conversations, messages, ledger transactions, wallet entries, payment orders, reward events, fraud events, auth sessions, reports, blocks, audit logs, and settlement hashes.
- Added Alembic migrations for initial schema and later production hardening changes.
- Added React/Vite UI based on the WhatsApp-style prototype direction.
- Wired UI to backend login, profile, contacts, chat, wallet, recharge, transactions, and admin metric flows.
- Added Docker Compose for API, PostgreSQL, and Redis.

### Auth Hardening

- Implemented phone OTP send and verify.
- Added user and wallet creation on login.
- Added JWT access tokens with auth sessions and logout revocation.
- Added device tracking and account-per-device limits.
- Added OTP resend cooldown, max verify attempts, phone velocity limits, and IP send limits.
- Added admin visibility into OTP challenges without exposing OTP codes.
- Added production config guard to reject dev OTP provider.

### Wallet And Ledger Hardening

- Implemented spend priority: purchased balance first, then earned unlocked balance.
- Prevented spending locked balance.
- Added double-entry wallet entries for debits and credits.
- Added wallet freeze behavior.
- Added P2P wallet transfer with platform gas.
- Added transfer idempotency keys and mismatch rejection.
- Added row locking for wallet mutation paths on PostgreSQL.
- Added ledger constraints for positive amounts and valid entry types.
- Added wallet history filters, pagination, direction metadata, signed amounts, and blank-filter rejection.
- Added ledger audit and balance reconciliation style checks.

### Payment Hardening

- Added Razorpay order creation.
- Added local dev capture for demos.
- Added webhook signature verification.
- Added webhook max body size protection.
- Added idempotent payment crediting.
- Added amount/currency mismatch rejection.
- Added failed-payment webhook handling.
- Prevented failed payment orders from being captured later.
- Prevented failed webhook events from downgrading successful orders.
- Added payment history filters.
- Added admin payment filters and manual fail endpoint.
- Added production config guard for Razorpay key ID, key secret, and webhook secret.

### Chat Hardening

- Added conversations and direct chat membership.
- Added token-unit paid message sending with wallet deduction, receiver reward, gas, and reserve split based on message cost.
- Added message idempotency.
- Added max message content length.
- Added content normalization and duplicate-content fraud hashing.
- Added message velocity limits.
- Added daily free quota fallback to paid messages.
- Added delivery and read receipt timestamps.
- Added WebSocket auth and malformed event handling.
- Added blocked-user checks across chat and transfer paths.
- Added message reports and spam/fraud handling.

### Fraud And Admin Hardening

- Added fraud events with severity/status.
- Added reward caps and reward lock events.
- Added report resolution and fraud event resolution flows.
- Added admin audit logs for sensitive actions.
- Added self-lockout protections for admin actions.
- Added admin user filters, user detail, sessions, OTP challenge filters, wallet inventory, payment filters, message filters, ledger filters, reward filters, report filters, fraud filters, and audit log filters.
- Added settlement hash generation and settlement filters.
- Hardened all obvious trimmed-query filter paths to reject blank values and normalize casing where appropriate.

### API And Deployment Hardening

- Added health and readiness endpoints.
- Added request ID propagation.
- Added access logging.
- Added security headers.
- Added optional HSTS for production.
- Added TrustedHost and CORS production validation.
- Added global request body size guard.
- Added `AUTO_CREATE_TABLES` production guard so migrations are required in production.
- Added `.gitignore` to exclude databases, cache folders, build output, and dependencies.

### Data Validation Hardening

- Normalized signup name and username.
- Rejected duplicate usernames.
- Normalized and deduplicated email.
- Validated avatar URL scheme.
- Hardened public user search so blank whitespace cannot enumerate users.
- Hardened wallet transaction filters.
- Hardened admin settlement hash filters to require 64-character hex hashes.
- Hardened profile display names so whitespace-only names cannot be saved.

## Verification History

Latest backend verification before pushing:

```text
python -m pytest
120 passed
```

Migration smoke checks were also run:

```text
DATABASE_URL=sqlite:///:memory: alembic upgrade head
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/orca_chat alembic upgrade head --sql
```

Both migration checks passed.

## Production Pending Tasks

### Phase 1: Backend Production Hardening

- Replace dev OTP provider with a real MSG91/Twilio integration.
- Add Redis-backed OTP/session/rate-limit storage.
- Add centralized error response format.
- Add structured JSON logging.
- Add request tracing across HTTP, WebSocket, payments, wallet, and admin flows.
- Add API versioning such as `/api/v1`.
- Replace simple admin role with scoped RBAC permissions.

### Phase 2: Payments Production

- Test real Razorpay Orders API path with a mocked client.
- Add frontend Razorpay Checkout signature verification.
- Persist raw webhook events for audit and replay protection.
- Add refund flow.
- Add payment reconciliation worker.
- Add failed/pending payment retry handling.
- Add invoice or receipt generation.

### Phase 3: Wallet And Ledger Production

- Enforce immutable settled ledger rows.
- Add admin CSV exports for ledger and wallet entries.
- Add daily settlement verification endpoint.
- Run settlement generation as a scheduled worker.
- Add wallet balance reconciliation job.
- Add negative-balance and mismatch alerting.
- Add detailed platform/reserve wallet dashboards.
- Run reward unlock as a scheduled worker.

### Phase 4: Chat Production

- Add real message encryption and key management.
- Move from offset pagination to cursor pagination for messages.
- Add Redis pub/sub for multi-instance WebSocket scaling.
- Add offline notification queue.
- Add push notifications.
- Add archive/delete conversation behavior.
- Add attachment/media rules if media is in MVP beta.

### Phase 5: Fraud And Abuse

- Add device fingerprint model/table.
- Add IP/device/user velocity scoring history.
- Add fraud score history.
- Add admin wallet freeze action directly from fraud event.
- Add spam report escalation.
- Add suspicious reward graph detection.
- Add fraud dashboard summaries by day, user, device, and IP.

### Phase 6: Admin Dashboard UI

- Build admin user table UI.
- Build wallet inventory UI.
- Build payment monitoring UI.
- Build ledger explorer UI.
- Build fraud and report moderation UI.
- Build audit log viewer UI.
- Add CSV export buttons.
- Add bulk admin actions.

### Phase 7: Frontend Production

- Polish OTP login and profile onboarding.
- Add loading, empty, and error states everywhere.
- Add robust WebSocket reconnect behavior.
- Add Razorpay Checkout in frontend.
- Add read receipt and delivery receipt UI states.
- Add wallet history filters in UI.
- Add responsive mobile polish.
- Add frontend tests.

### Phase 8: DevOps And Deployment

- Harden production Dockerfile.
- Add separate dev, staging, and production env templates.
- Add CI pipeline for lint, tests, and migration checks.
- Add migration deployment process.
- Add HTTPS/reverse proxy config.
- Add secrets management.
- Add Postgres backups.
- Add monitoring, alerts, and Sentry/OpenTelemetry.
- Add load testing for chat, wallet, and payment paths.

### Phase 9: Security And Compliance

- Add privacy policy and terms.
- Define data retention policy.
- Mask PII in logs.
- Review admin access audit policy.
- Review payment compliance.
- Prepare KYC flow if withdrawals are added later.
- Add moderation policy.
- Run penetration testing before public launch.

### Phase 10: Beta Launch

- Seed staging environment.
- Run internal beta with 20 to 50 users.
- Test OTP, chat, recharge, paid messages, rewards, and admin metrics.
- Track recharge conversion.
- Track message volume and fraud attempts.
- Tune pricing, reward lock, gas fee, and free quota.
- Prepare investor demo.

## Recommended Next Build Order

1. Real Razorpay frontend checkout and backend verification.
2. Real OTP provider integration.
3. Redis-backed WebSocket and presence scaling.
4. Admin dashboard UI.
5. Production deployment pipeline.

## Current GitHub Push

Initial push was made to:

```text
https://github.com/urstrulyheman/atomic-chat
```

Initial commit:

```text
9d2f28c Initial Orca Chat Coin MVP
```

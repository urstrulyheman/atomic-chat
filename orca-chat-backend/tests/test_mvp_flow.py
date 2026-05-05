import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app.config import Settings, settings
from app.models import (
    Conversation,
    ConversationMember,
    FraudEvent,
    LedgerTransaction,
    Message,
    OtpChallenge,
    PaymentOrder,
    RewardEvent,
    User,
    Wallet,
    WalletEntry,
)
from app.database import Base, engine
from app.database import SessionLocal
from app.main import app
from app.utils.time import as_utc, utc_now


client = TestClient(app)


def setup_module():
    Base.metadata.create_all(bind=engine)


def login(phone: str, name: str):
    client.post("/auth/send-otp", json={"phone": phone})
    response = client.post("/auth/verify-otp", json={"phone": phone, "otp": "123456", "name": name})
    assert response.status_code == 200
    return response.json()


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def make_admin(phone: str = "+919999999999"):
    admin = login(phone, "Admin User")
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == phone).one()
        user.role = "admin"
        db.commit()
    return admin


def test_health_and_readiness_endpoints():
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["checks"]["database"] == "ok"


def test_request_id_header_is_added_and_validated():
    generated = client.get("/health")
    supplied = client.get("/health", headers={"X-Request-ID": "orca-test-request-123"})
    rejected = client.get("/health", headers={"X-Request-ID": "bad value"})

    assert generated.status_code == 200
    assert generated.headers["X-Request-ID"]
    assert supplied.headers["X-Request-ID"] == "orca-test-request-123"
    assert rejected.headers["X-Request-ID"] != "bad value"
    assert len(rejected.headers["X-Request-ID"]) >= 8


def test_access_log_includes_request_id_status_and_latency(caplog):
    caplog.set_level(logging.INFO, logger="orca_chat.api")

    response = client.get("/health", headers={"X-Request-ID": "orca-log-test-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "orca-log-test-123"
    assert float(response.headers["X-Response-Time-ms"]) >= 0
    assert any(
        "request_completed request_id=orca-log-test-123 method=GET path=/health status_code=200" in record.message
        for record in caplog.records
    )


def test_security_headers_are_added_to_responses():
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"


def test_oversized_http_request_body_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_bytes", 16)

    response = client.post(
        "/auth/send-otp",
        json={"phone": "+919000000171", "padding": "this body is intentionally too large"},
        headers={"X-Request-ID": "orca-large-body-123"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body too large"
    assert response.headers["X-Request-ID"] == "orca-large-body-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_paid_message_economy_flow():
    user_a = login("+919000000001", "User A")
    user_b = login("+919000000002", "User B")

    chat = client.post(
        "/chats",
        json={"receiver_id": user_b["user"]["id"]},
        headers=auth(user_a["access_token"]),
    )
    assert chat.status_code == 200

    sent = client.post(
        f"/chats/{chat.json()['id']}/messages",
        json={"receiver_id": user_b["user"]["id"], "content": "Hello paid world"},
        headers=auth(user_a["access_token"]),
    )
    assert sent.status_code == 200
    assert sent.json()["coin_cost"] == "1.000000"

    balance_a = client.get("/wallet/balance", headers=auth(user_a["access_token"])).json()
    balance_b = client.get("/wallet/balance", headers=auth(user_b["access_token"])).json()

    assert balance_a["spendable_balance"] == "19.000000"
    assert balance_b["locked_balance"] == "0.650000"

    admin = make_admin("+919999999001")
    metrics = client.get("/admin/metrics", headers=auth(admin["access_token"])).json()
    assert metrics["chat"]["total_messages"] == 1
    assert metrics["wallet"]["total_gas_collected"] == 0.25


def test_paid_message_cost_scales_by_billing_tokens():
    sender = login("+919000000173", "Token Cost Sender")
    receiver = login("+919000000174", "Token Cost Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    content = "x" * 81

    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": content},
        headers=auth(sender["access_token"]),
    )
    sender_balance = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    sender_history = client.get("/wallet/transactions", headers=auth(sender["access_token"])).json()

    assert sent.status_code == 200
    assert sent.json()["coin_cost"] == "2.000000"
    assert sender_balance["spendable_balance"] == "18.000000"
    assert receiver_balance["locked_balance"] == "1.300000"
    transaction = next(row for row in sender_history if row["id"] == sent.json()["transaction_id"])
    assert transaction["gross_amount"] == "2.000000"
    assert transaction["platform_gas"] == "0.500000"
    assert transaction["receiver_reward"] == "1.300000"
    assert transaction["reserve_amount"] == "0.200000"
    assert transaction["metadata"]["pricing_model"] == "token_units"
    assert transaction["metadata"]["billing_token_count"] == 21
    assert transaction["metadata"]["billing_units"] == 2


def test_admin_metrics_support_date_windows():
    sender = login("+919000000117", "Metrics Window Sender")
    receiver = login("+919000000118", "Metrics Window Receiver")
    admin = make_admin("+919999999023")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Windowed metrics"},
        headers=auth(sender["access_token"]),
    )

    current_window = client.get("/admin/metrics?start_date=2000-01-01&end_date=2999-12-31", headers=auth(admin["access_token"]))
    future_window = client.get("/admin/metrics?start_date=2999-01-01&end_date=2999-01-02", headers=auth(admin["access_token"]))
    invalid_window = client.get("/admin/metrics?start_date=2026-04-30&end_date=2026-04-29", headers=auth(admin["access_token"]))

    assert sent.status_code == 200
    assert current_window.status_code == 200
    assert current_window.json()["window"] == {"start_date": "2000-01-01", "end_date": "2999-12-31"}
    assert current_window.json()["chat"]["total_messages"] >= 1
    assert current_window.json()["wallet"]["total_gas_collected"] >= 0.25
    assert future_window.status_code == 200
    assert future_window.json()["chat"]["total_messages"] == 0
    assert future_window.json()["payments"]["recharge_revenue"] == 0
    assert invalid_window.status_code == 400
    assert invalid_window.json()["detail"] == "start_date must be before or equal to end_date"


def test_logout_revokes_current_access_token():
    user = login("+919000000029", "Logout User")
    headers = auth(user["access_token"])

    before_logout = client.get("/auth/me", headers=headers)
    assert before_logout.status_code == 200

    logout = client.post("/auth/logout", headers=headers)
    assert logout.status_code == 200

    after_logout = client.get("/auth/me", headers=headers)
    assert after_logout.status_code == 401


def test_websocket_rejects_revoked_access_token():
    user = login("+919000000049", "WS Revoked User")
    headers = auth(user["access_token"])

    logout = client.post("/auth/logout", headers=headers)

    assert logout.status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/chat?token={user['access_token']}"):
            pass
    assert exc.value.code == 4401


def test_websocket_rechecks_token_before_message_send():
    sender = login("+919000000050", "WS Sender")
    receiver = login("+919000000051", "WS Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        logout = client.post("/auth/logout", headers=auth(sender["access_token"]))
        assert logout.status_code == 200

        websocket.send_json(
            {
                "type": "message.send",
                "payload": {
                    "chat_id": chat["id"],
                    "receiver_id": receiver["user"]["id"],
                    "content": "This should not send",
                },
            }
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_json()
        assert exc.value.code == 4401


def test_websocket_returns_errors_for_invalid_message_payloads():
    sender = login("+919000000052", "WS Invalid Sender")
    receiver = login("+919000000053", "WS Invalid Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        websocket.send_json(
            {
                "type": "message.send",
                "payload": {"chat_id": "not-a-uuid", "receiver_id": receiver["user"]["id"], "content": "bad"},
            }
        )
        bad_uuid = websocket.receive_json()

        websocket.send_json(
            {
                "type": "message.send",
                "payload": {"chat_id": chat["id"], "receiver_id": receiver["user"]["id"], "content": "   "},
            }
        )
        empty_content = websocket.receive_json()

    assert bad_uuid == {"type": "error", "payload": {"detail": "Invalid message.send payload"}}
    assert empty_content == {"type": "error", "payload": {"detail": "Message content is required"}}


def test_websocket_trims_message_content_before_storage():
    sender = login("+919000009901", "WS Trim Sender")
    receiver = login("+919000009902", "WS Trim Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        websocket.send_json(
            {
                "type": "message.send",
                "payload": {
                    "chat_id": chat["id"],
                    "receiver_id": receiver["user"]["id"],
                    "content": "  WebSocket hello  ",
                },
            }
        )
        response = websocket.receive_json()

    assert response["type"] == "message.sent"
    assert response["payload"]["content"] == "WebSocket hello"


def test_websocket_rejects_oversized_message_content(monkeypatch):
    sender = login("+919000000054", "WS Large Sender")
    receiver = login("+919000000055", "WS Large Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    monkeypatch.setattr(settings, "message_max_content_length", 8)

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        websocket.send_json(
            {
                "type": "message.send",
                "payload": {
                    "chat_id": chat["id"],
                    "receiver_id": receiver["user"]["id"],
                    "content": "this is too long",
                },
            }
        )
        response = websocket.receive_json()

    assert response == {"type": "error", "payload": {"detail": "Message content too large"}}


def test_websocket_rejects_invalid_idempotency_key():
    sender = login("+919000000060", "WS Bad Idempotency Sender")
    receiver = login("+919000000061", "WS Bad Idempotency Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        websocket.send_json(
            {
                "type": "message.send",
                "payload": {
                    "chat_id": chat["id"],
                    "receiver_id": receiver["user"]["id"],
                    "content": "Bad key",
                    "idempotency_key": "x" * 121,
                },
            }
        )
        response = websocket.receive_json()

    assert response == {"type": "error", "payload": {"detail": "Idempotency key too large"}}


def test_websocket_rejects_invalid_idempotency_key_format():
    sender = login("+919000000062", "WS Invalid Idempotency Sender")
    receiver = login("+919000000063", "WS Invalid Idempotency Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    with client.websocket_connect(f"/ws/chat?token={sender['access_token']}") as websocket:
        websocket.send_json(
            {
                "type": "message.send",
                "payload": {
                    "chat_id": chat["id"],
                    "receiver_id": receiver["user"]["id"],
                    "content": "Bad key",
                    "idempotency_key": "bad key!",
                },
            }
        )
        response = websocket.receive_json()

    assert response == {"type": "error", "payload": {"detail": "Idempotency key has invalid format"}}


def test_auth_sessions_track_device_and_can_revoke_other_session():
    phone = "+919000000030"
    client.post("/auth/send-otp", json={"phone": phone})
    first = client.post(
        "/auth/verify-otp",
        json={"phone": phone, "otp": "123456", "name": "Session User", "device_label": "Pixel 8"},
        headers={"User-Agent": "OrcaAndroid/1.0"},
    ).json()

    client.post("/auth/send-otp", json={"phone": phone})
    second = client.post(
        "/auth/verify-otp",
        json={"phone": phone, "otp": "123456", "device_label": "iPhone 15"},
        headers={"User-Agent": "OrcaiOS/1.0"},
    ).json()

    sessions = client.get("/auth/sessions", headers=auth(first["access_token"]))
    assert sessions.status_code == 200
    session_rows = sessions.json()
    labels = {row["device_label"] for row in session_rows}
    assert {"Pixel 8", "iPhone 15"}.issubset(labels)
    assert any(row["user_agent"] == "OrcaAndroid/1.0" for row in session_rows)

    second_session_id = next(row["id"] for row in session_rows if row["device_label"] == "iPhone 15")
    revoked = client.post(f"/auth/sessions/{second_session_id}/revoke", headers=auth(first["access_token"]))
    assert revoked.status_code == 200

    second_me = client.get("/auth/me", headers=auth(second["access_token"]))
    assert second_me.status_code == 401

    first_me = client.get("/auth/me", headers=auth(first["access_token"]))
    assert first_me.status_code == 200


def test_admin_can_filter_auth_sessions_by_device_ip_and_status():
    phone = "+919000000124"
    client.post("/auth/send-otp", json={"phone": phone})
    user = client.post(
        "/auth/verify-otp",
        json={"phone": phone, "otp": "123456", "name": "Admin Session User", "device_label": "Support Device"},
        headers={"User-Agent": "OrcaSupportTest/1.0"},
    ).json()
    admin = make_admin("+919999999124")
    today = utc_now().date().isoformat()

    active = client.get(
        (
            f"/admin/sessions?status=active&user_id={user['user']['id']}"
            f"&device_label=%20Support%20Device%20&start_date={today}&end_date={today}"
            f"&last_seen_start_date={today}&last_seen_end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    assert active.status_code == 200
    rows = active.json()
    assert len(rows) == 1
    session = rows[0]
    assert session["user"]["phone"] == phone
    assert session["device_label"] == "Support Device"
    assert session["user_agent"] == "OrcaSupportTest/1.0"
    assert session["ip_address"]
    assert session["jti"]

    by_ip_and_jti = client.get(
        f"/admin/sessions?ip_address=%20{session['ip_address']}%20&jti=%20{session['jti']}%20",
        headers=auth(admin["access_token"]),
    )
    assert by_ip_and_jti.status_code == 200
    assert [row["id"] for row in by_ip_and_jti.json()] == [session["id"]]

    revoked = client.post(f"/admin/sessions/{session['id']}/revoke", headers=auth(admin["access_token"]))
    assert revoked.status_code == 200
    revoked_session = revoked.json()
    assert revoked_session["id"] == session["id"]
    assert revoked_session["status"] == "revoked"
    assert revoked_session["revoked_at"]

    user_me = client.get("/auth/me", headers=auth(user["access_token"]))
    assert user_me.status_code == 401

    revoked_rows = client.get(
        f"/admin/sessions?status=revoked&user_id={user['user']['id']}",
        headers=auth(admin["access_token"]),
    )
    assert revoked_rows.status_code == 200
    assert any(row["id"] == session["id"] and row["revoked_at"] for row in revoked_rows.json())

    audit_log = client.get(
        f"/admin/audit-logs?action=auth_session.revoke&target_id={session['id']}",
        headers=auth(admin["access_token"]),
    )
    assert audit_log.status_code == 200
    assert audit_log.json()[0]["metadata"]["user_id"] == user["user"]["id"]

    bad_window = client.get(
        "/admin/sessions?start_date=2026-04-30&end_date=2026-04-29",
        headers=auth(admin["access_token"]),
    )
    blank_device_label = client.get("/admin/sessions?device_label=%20%20%20", headers=auth(admin["access_token"]))
    blank_ip_address = client.get("/admin/sessions?ip_address=%20%20%20", headers=auth(admin["access_token"]))
    blank_jti = client.get("/admin/sessions?jti=%20%20%20", headers=auth(admin["access_token"]))
    assert bad_window.status_code == 400
    assert blank_device_label.status_code == 422
    assert blank_device_label.json()["detail"] == "device_label cannot be blank"
    assert blank_ip_address.status_code == 422
    assert blank_ip_address.json()["detail"] == "ip_address cannot be blank"
    assert blank_jti.status_code == 422
    assert blank_jti.json()["detail"] == "jti cannot be blank"


def test_new_account_creation_is_limited_per_device(monkeypatch):
    monkeypatch.setattr(settings, "auth_max_accounts_per_device", 2)
    device_label = "Shared Test Device Limit"

    for phone in ("+919000000121", "+919000000122"):
        client.post("/auth/send-otp", json={"phone": phone})
        created = client.post(
            "/auth/verify-otp",
            json={"phone": phone, "otp": "123456", "name": "Device User", "device_label": device_label},
        )
        assert created.status_code == 200

    client.post("/auth/send-otp", json={"phone": "+919000000123"})
    third = client.post(
        "/auth/verify-otp",
        json={"phone": "+919000000123", "otp": "123456", "name": "Blocked Device User", "device_label": device_label},
    )
    client.post("/auth/send-otp", json={"phone": "+919000000121"})
    existing_login = client.post(
        "/auth/verify-otp",
        json={"phone": "+919000000121", "otp": "123456", "device_label": device_label},
    )

    assert third.status_code == 429
    assert third.json()["detail"] == "Too many accounts registered from this device"
    assert existing_login.status_code == 200


def test_otp_send_is_rate_limited_by_ip(monkeypatch):
    with SessionLocal() as db:
        existing_sends = db.query(OtpChallenge).filter(OtpChallenge.ip_address == "testclient").count()
    monkeypatch.setattr(settings, "otp_max_ip_sends_per_hour", existing_sends + 2)

    first = client.post("/auth/send-otp", json={"phone": "+919000000044"})
    second = client.post("/auth/send-otp", json={"phone": "+919000000045"})
    third = client.post("/auth/send-otp", json={"phone": "+919000000046"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert "network" in third.json()["detail"].lower()


def test_phone_auth_normalizes_and_rejects_invalid_numbers():
    phone = "+919000000166"

    sent = client.post("/auth/send-otp", json={"phone": f"  {phone}  "})
    verified = client.post("/auth/verify-otp", json={"phone": f" {phone} ", "otp": "123456", "name": "Phone Format User"})
    invalid_send = client.post("/auth/send-otp", json={"phone": "9000000166"})
    invalid_verify = client.post("/auth/verify-otp", json={"phone": "+0123456789", "otp": "123456"})
    too_long = client.post("/auth/send-otp", json={"phone": "+919000000166999999"})

    assert sent.status_code == 200
    assert verified.status_code == 200
    assert verified.json()["user"]["phone"] == phone
    assert invalid_send.status_code == 422
    assert invalid_verify.status_code == 422
    assert too_long.status_code == 422


def test_otp_verify_rejects_malformed_codes_without_counting_attempts():
    phone = "+919000000167"
    sent = client.post("/auth/send-otp", json={"phone": phone})

    alpha_code = client.post("/auth/verify-otp", json={"phone": phone, "otp": "12a456"})
    short_code = client.post("/auth/verify-otp", json={"phone": phone, "otp": "12345"})
    padded_code = client.post("/auth/verify-otp", json={"phone": phone, "otp": " 123456 "})

    with SessionLocal() as db:
        challenge = db.query(OtpChallenge).filter(OtpChallenge.phone == phone).one()

    assert sent.status_code == 200
    assert alpha_code.status_code == 422
    assert short_code.status_code == 422
    assert padded_code.status_code == 422
    assert challenge.attempts == 0


def test_signup_profile_fields_are_normalized_and_username_conflicts_are_rejected():
    first_phone = "+919000000168"
    second_phone = "+919000000169"
    invalid_phone = "+919000000170"

    client.post("/auth/send-otp", json={"phone": first_phone})
    first = client.post(
        "/auth/verify-otp",
        json={"phone": first_phone, "otp": "123456", "name": "  Signup Name  ", "username": "Signup_User"},
    )
    client.post("/auth/send-otp", json={"phone": second_phone})
    duplicate = client.post(
        "/auth/verify-otp",
        json={"phone": second_phone, "otp": "123456", "name": "Second Signup", "username": "signup_user"},
    )
    client.post("/auth/send-otp", json={"phone": invalid_phone})
    invalid_username = client.post(
        "/auth/verify-otp",
        json={"phone": invalid_phone, "otp": "123456", "username": "bad name!"},
    )

    with SessionLocal() as db:
        invalid_challenge = db.query(OtpChallenge).filter(OtpChallenge.phone == invalid_phone).one()

    assert first.status_code == 200
    assert first.json()["user"]["name"] == "Signup Name"
    assert first.json()["user"]["username"] == "signup_user"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Username is already taken"
    assert invalid_username.status_code == 422
    assert invalid_challenge.attempts == 0


def test_admin_can_inspect_otp_challenges_without_secret_codes():
    phone = "+919000000161"
    admin = make_admin("+919999999038")
    today = utc_now().date().isoformat()

    sent = client.post("/auth/send-otp", json={"phone": phone})
    first_bad_attempt = client.post("/auth/verify-otp", json={"phone": phone, "otp": "000000"})
    second_bad_attempt = client.post("/auth/verify-otp", json={"phone": phone, "otp": "111111"})

    rows = client.get(
        (
            f"/admin/otp-challenges?phone=%20%2B{phone.lstrip('+')}%20&ip_address=%20testclient%20&status=pending"
            f"&min_attempts=2&max_attempts=2&min_send_count=1&max_send_count=1"
            f"&start_date={today}&end_date={today}&expires_start_date={today}&expires_end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    invalid_attempt_window = client.get(
        "/admin/otp-challenges?min_attempts=5&max_attempts=1",
        headers=auth(admin["access_token"]),
    )
    invalid_send_window = client.get(
        "/admin/otp-challenges?min_send_count=5&max_send_count=1",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/otp-challenges?status=used", headers=auth(admin["access_token"]))
    blank_phone = client.get("/admin/otp-challenges?phone=%20%20%20", headers=auth(admin["access_token"]))
    blank_ip_address = client.get("/admin/otp-challenges?ip_address=%20%20%20", headers=auth(admin["access_token"]))

    assert sent.status_code == 200
    assert first_bad_attempt.status_code == 400
    assert second_bad_attempt.status_code == 400
    assert rows.status_code == 200
    assert len(rows.json()) == 1
    row = rows.json()[0]
    assert row["phone"] == phone
    assert row["ip_address"] == "testclient"
    assert row["status"] == "pending"
    assert row["attempts"] == 2
    assert row["send_count"] == 1
    assert "otp_code" not in row
    expired = client.post(f"/admin/otp-challenges/{row['id']}/expire", headers=auth(admin["access_token"]))
    verify_after_expire = client.post("/auth/verify-otp", json={"phone": phone, "otp": "123456"})
    audit_log = client.get(
        f"/admin/audit-logs?action=otp_challenge.expire&target_id={row['id']}",
        headers=auth(admin["access_token"]),
    )

    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert "otp_code" not in expired.json()
    assert verify_after_expire.status_code == 400
    assert verify_after_expire.json()["detail"] == "Invalid OTP"
    assert audit_log.status_code == 200
    assert audit_log.json()[0]["metadata"]["phone"] == phone
    assert audit_log.json()[0]["metadata"]["previous_status"] == "pending"
    assert invalid_attempt_window.status_code == 400
    assert invalid_send_window.status_code == 400
    assert invalid_status.status_code == 422
    assert blank_phone.status_code == 422
    assert blank_phone.json()["detail"] == "phone cannot be blank"
    assert blank_ip_address.status_code == 422
    assert blank_ip_address.json()["detail"] == "ip_address cannot be blank"


def test_user_search_returns_public_profile_only():
    seeker = login("+919000000037", "Search Seeker")
    target = login("+919000000038", "Needle Person")
    update = client.patch(
        "/users/me",
        json={"username": "needle_user", "email": "private@example.com"},
        headers=auth(target["access_token"]),
    )
    assert update.status_code == 200

    response = client.get("/users/search?q=%20NEEDLE%20", headers=auth(seeker["access_token"]))
    blank = client.get("/users/search?q=%20%20%20", headers=auth(seeker["access_token"]))
    assert response.status_code == 200
    assert blank.status_code == 422
    assert blank.json()["detail"] == "q must contain at least 2 non-whitespace characters"
    rows = response.json()
    assert any(row["id"] == target["user"]["id"] for row in rows)
    assert "phone" not in rows[0]
    assert "email" not in rows[0]
    assert "role" not in rows[0]


def test_profile_update_rejects_duplicate_and_invalid_username():
    first = login("+919000000068", "Profile First")
    second = login("+919000000069", "Profile Second")

    first_update = client.patch(
        "/users/me",
        json={"username": "Taken_Name"},
        headers=auth(first["access_token"]),
    )
    duplicate = client.patch(
        "/users/me",
        json={"username": "taken_name"},
        headers=auth(second["access_token"]),
    )
    invalid = client.patch(
        "/users/me",
        json={"username": "bad name!"},
        headers=auth(second["access_token"]),
    )

    assert first_update.status_code == 200
    assert first_update.json()["username"] == "taken_name"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Username is already taken"
    assert invalid.status_code == 422


def test_profile_update_normalizes_name_and_rejects_blank_name():
    user = login("+919000000172", "Name User")

    valid = client.patch(
        "/users/me",
        json={"name": "  Display Name  "},
        headers=auth(user["access_token"]),
    )
    blank = client.patch(
        "/users/me",
        json={"name": "   "},
        headers=auth(user["access_token"]),
    )

    assert valid.status_code == 200
    assert valid.json()["name"] == "Display Name"
    assert blank.status_code == 422


def test_profile_update_normalizes_and_deduplicates_email():
    first = login("+919000000070", "Email First")
    second = login("+919000000071", "Email Second")

    first_update = client.patch(
        "/users/me",
        json={"email": "  Person@Example.COM  "},
        headers=auth(first["access_token"]),
    )
    duplicate = client.patch(
        "/users/me",
        json={"email": "person@example.com"},
        headers=auth(second["access_token"]),
    )
    invalid = client.patch(
        "/users/me",
        json={"email": "not-an-email"},
        headers=auth(second["access_token"]),
    )

    assert first_update.status_code == 200
    assert first_update.json()["email"] == "person@example.com"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Email is already taken"
    assert invalid.status_code == 422


def test_users_email_unique_index_blocks_direct_duplicates():
    with SessionLocal() as db:
        db.add_all(
            [
                User(phone="+919000000073", email="unique-db@example.com", name="Unique Email One"),
                User(phone="+919000000074", email="unique-db@example.com", name="Unique Email Two"),
            ]
        )

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_profile_update_validates_avatar_url_scheme():
    user = login("+919000000072", "Avatar User")

    valid = client.patch(
        "/users/me",
        json={"avatar_url": "  https://cdn.example.com/avatar.png  "},
        headers=auth(user["access_token"]),
    )
    invalid = client.patch(
        "/users/me",
        json={"avatar_url": "javascript:alert(1)"},
        headers=auth(user["access_token"]),
    )

    assert valid.status_code == 200
    assert valid.json()["avatar_url"] == "https://cdn.example.com/avatar.png"
    assert invalid.status_code == 422


def test_user_search_supports_bounded_pagination():
    seeker = login("+919000000040", "Search Pager")
    first = login("+919000000041", "Page Target One")
    second = login("+919000000042", "Page Target Two")

    first_page = client.get("/users/search?q=Page%20Target&limit=1", headers=auth(seeker["access_token"]))
    second_page = client.get("/users/search?q=Page%20Target&limit=1&offset=1", headers=auth(seeker["access_token"]))
    invalid_page = client.get("/users/search?q=Page%20Target&limit=1000", headers=auth(seeker["access_token"]))

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert len(first_page.json()) == 1
    assert len(second_page.json()) == 1
    returned_ids = {first_page.json()[0]["id"], second_page.json()[0]["id"]}
    assert {first["user"]["id"], second["user"]["id"]}.issubset(returned_ids)
    assert invalid_page.status_code == 422


def test_public_directory_can_be_disabled(monkeypatch):
    user = login("+919000000039", "Directory Disabled User")
    monkeypatch.setattr(settings, "enable_public_user_directory", False)

    response = client.get("/users", headers=auth(user["access_token"]))

    assert response.status_code == 403


def test_wallet_transaction_pagination_rejects_invalid_limit():
    user = login("+919000000043", "Invalid Limit User")

    response = client.get("/wallet/transactions?limit=0", headers=auth(user["access_token"]))

    assert response.status_code == 422


def test_wallet_transactions_include_direction_and_metadata():
    sender = login("+919000000084", "Wallet History Sender")
    receiver = login("+919000000085", "Wallet History Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Wallet history context"},
        headers=auth(sender["access_token"]),
    )

    sender_history = client.get("/wallet/transactions", headers=auth(sender["access_token"]))
    receiver_history = client.get("/wallet/transactions", headers=auth(receiver["access_token"]))

    assert sent.status_code == 200
    assert sender_history.status_code == 200
    assert receiver_history.status_code == 200
    sender_txn = sender_history.json()[0]
    receiver_txn = receiver_history.json()[0]
    assert sender_txn["id"] == sent.json()["transaction_id"]
    assert sender_txn["direction"] == "outgoing"
    assert sender_txn["metadata"]["fraud_risk"] is False
    assert receiver_txn["id"] == sent.json()["transaction_id"]
    assert receiver_txn["direction"] == "incoming"
    assert receiver_txn["receiver_reward"] == "0.650000"


def test_wallet_entries_include_direction_and_signed_amount():
    sender = login("+919000000086", "Wallet Entry Sender")
    receiver = login("+919000000087", "Wallet Entry Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Wallet entry context"},
        headers=auth(sender["access_token"]),
    )

    sender_entries = client.get("/wallet/entries", headers=auth(sender["access_token"]))
    receiver_entries = client.get("/wallet/entries", headers=auth(receiver["access_token"]))

    assert sent.status_code == 200
    assert sender_entries.status_code == 200
    assert receiver_entries.status_code == 200
    sender_debit = next(entry for entry in sender_entries.json() if entry["transaction_id"] == sent.json()["transaction_id"])
    receiver_credit = next(entry for entry in receiver_entries.json() if entry["transaction_id"] == sent.json()["transaction_id"])
    assert sender_debit["entry_type"] == "DEBIT"
    assert sender_debit["direction"] == "outgoing"
    assert sender_debit["signed_amount"] == "-1.000000"
    assert receiver_credit["entry_type"] == "CREDIT"
    assert receiver_credit["direction"] == "incoming"
    assert receiver_credit["signed_amount"] == "0.650000"
    assert receiver_credit["balance_type"] == "locked"


def test_wallet_history_filters_transactions_and_entries():
    sender = login("+919000000134", "Wallet Filter Sender")
    receiver = login("+919000000135", "Wallet Filter Receiver")
    today = utc_now().date().isoformat()

    transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "4.000000", "note": "filter pass"},
        headers=auth(sender["access_token"]),
    )
    outgoing_transfers = client.get(
        f"/wallet/transactions?transaction_type=%20WALLET_TRANSFER%20&direction=outgoing&start_date={today}&end_date={today}",
        headers=auth(sender["access_token"]),
    )
    incoming_transfers = client.get(
        "/wallet/transactions?transaction_type=wallet_transfer&direction=incoming",
        headers=auth(receiver["access_token"]),
    )
    debit_entries = client.get(
        "/wallet/entries?entry_type=DEBIT&balance_type=%20PURCHASED%20",
        headers=auth(sender["access_token"]),
    )
    credit_entries = client.get(
        "/wallet/entries?entry_type=CREDIT&balance_type=purchased",
        headers=auth(receiver["access_token"]),
    )
    invalid_direction = client.get(
        "/wallet/transactions?direction=sideways",
        headers=auth(sender["access_token"]),
    )
    invalid_entry_type = client.get(
        "/wallet/entries?entry_type=MOVE",
        headers=auth(sender["access_token"]),
    )
    invalid_window = client.get(
        "/wallet/transactions?start_date=2026-04-30&end_date=2026-04-29",
        headers=auth(sender["access_token"]),
    )
    blank_transaction_type = client.get(
        "/wallet/transactions?transaction_type=%20%20%20",
        headers=auth(sender["access_token"]),
    )
    blank_balance_type = client.get(
        "/wallet/entries?balance_type=%20%20%20",
        headers=auth(sender["access_token"]),
    )

    assert transfer.status_code == 200
    assert outgoing_transfers.status_code == 200
    assert [row["id"] for row in outgoing_transfers.json()] == [transfer.json()["id"]]
    assert incoming_transfers.status_code == 200
    assert [row["id"] for row in incoming_transfers.json()] == [transfer.json()["id"]]
    assert incoming_transfers.json()[0]["direction"] == "incoming"
    assert debit_entries.status_code == 200
    assert any(
        entry["transaction_id"] == transfer.json()["id"]
        and entry["entry_type"] == "DEBIT"
        and entry["balance_type"] == "purchased"
        for entry in debit_entries.json()
    )
    assert credit_entries.status_code == 200
    assert any(
        entry["transaction_id"] == transfer.json()["id"]
        and entry["entry_type"] == "CREDIT"
        and entry["balance_type"] == "purchased"
        for entry in credit_entries.json()
    )
    assert invalid_direction.status_code == 422
    assert invalid_entry_type.status_code == 422
    assert invalid_window.status_code == 400
    assert blank_transaction_type.status_code == 422
    assert blank_transaction_type.json()["detail"] == "transaction_type cannot be blank"
    assert blank_balance_type.status_code == 422
    assert blank_balance_type.json()["detail"] == "balance_type cannot be blank"


def test_wallet_transfer_moves_spendable_coins_and_collects_gas():
    sender = login("+919000000112", "Transfer Sender")
    receiver = login("+919000000113", "Transfer Receiver")

    transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "10.000000", "note": "beta split"},
        headers=auth(sender["access_token"]),
    )
    sender_balance = client.get("/wallet/balance", headers=auth(sender["access_token"]))
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"]))
    sender_history = client.get("/wallet/transactions", headers=auth(sender["access_token"]))
    receiver_history = client.get("/wallet/transactions", headers=auth(receiver["access_token"]))
    admin = make_admin("+919999999020")
    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"]))

    assert transfer.status_code == 200
    assert transfer.json()["transaction_type"] == "wallet_transfer"
    assert transfer.json()["direction"] == "outgoing"
    assert transfer.json()["gross_amount"] == "10.000000"
    assert transfer.json()["platform_gas"] == "0.200000"
    assert transfer.json()["metadata"]["note"] == "beta split"
    assert sender_balance.json()["purchased_balance"] == "10.000000"
    assert sender_balance.json()["gas_paid_total"] == "0.200000"
    assert receiver_balance.json()["purchased_balance"] == "29.800000"
    assert sender_history.json()[0]["id"] == transfer.json()["id"]
    assert receiver_history.json()[0]["id"] == transfer.json()["id"]
    assert receiver_history.json()[0]["direction"] == "incoming"
    assert audit.json()["imbalanced_count"] == 0


def test_wallet_transfer_rejects_invalid_amounts_before_ledger_work():
    sender = login("+919000000172", "Invalid Transfer Sender")
    receiver = login("+919000000173", "Invalid Transfer Receiver")
    with SessionLocal() as db:
        before_count = db.query(LedgerTransaction).count()

    zero = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "0.000000"},
        headers=auth(sender["access_token"]),
    )
    negative = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "-1.000000"},
        headers=auth(sender["access_token"]),
    )
    too_precise = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.0000001"},
        headers=auth(sender["access_token"]),
    )
    too_many_digits = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1000000000000000000.000000"},
        headers=auth(sender["access_token"]),
    )

    with SessionLocal() as db:
        after_count = db.query(LedgerTransaction).count()

    assert zero.status_code == 422
    assert negative.status_code == 422
    assert too_precise.status_code == 422
    assert too_many_digits.status_code == 422
    assert after_count == before_count


def test_wallet_transfer_normalizes_blank_and_spaced_notes():
    sender = login("+919000000174", "Transfer Note Sender")
    receiver = login("+919000000175", "Transfer Note Receiver")

    blank_note = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.000000", "note": "   "},
        headers=auth(sender["access_token"]),
    )
    spaced_note = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.000000", "note": "  trimmed note  "},
        headers=auth(sender["access_token"]),
    )

    assert blank_note.status_code == 200
    assert blank_note.json()["metadata"]["note"] is None
    assert spaced_note.status_code == 200
    assert spaced_note.json()["metadata"]["note"] == "trimmed note"


def test_wallet_transfer_idempotency_prevents_double_send():
    sender = login("+919000000127", "Transfer Retry Sender")
    receiver = login("+919000000128", "Transfer Retry Receiver")
    headers = {**auth(sender["access_token"]), "X-Idempotency-Key": "transfer_retry_001"}

    first = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "5.000000", "note": "retry once"},
        headers=headers,
    )
    second = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "5.000000", "note": "retry once"},
        headers=headers,
    )
    sender_balance = client.get("/wallet/balance", headers=auth(sender["access_token"]))
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert sender_balance.json()["purchased_balance"] == "15.000000"
    assert receiver_balance.json()["purchased_balance"] == "24.900000"


def test_admin_ledger_transactions_support_reconciliation_filters():
    sender = login("+919000000144", "Ledger Filter Sender")
    receiver = login("+919000000145", "Ledger Filter Receiver")
    admin = make_admin("+919999999031")
    today = utc_now().date().isoformat()
    headers = {**auth(sender["access_token"]), "X-Idempotency-Key": "ledger_filter_001"}

    transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "4.000000", "note": "ledger filter"},
        headers=headers,
    )
    sender_wallet_id = transfer.json()["from_wallet_id"]
    receiver_wallet_id = transfer.json()["to_wallet_id"]
    outgoing = client.get(
        (
            "/admin/ledger/transactions"
            f"?transaction_type=%20WALLET_TRANSFER%20&status=settled&wallet_id={sender_wallet_id}&direction=outgoing"
            f"&from_wallet_id={sender_wallet_id}&to_wallet_id={receiver_wallet_id}&idempotency_key=%20ledger_filter_001%20"
            f"&min_gross_amount=4&max_gross_amount=4&min_platform_gas=0.08&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    incoming = client.get(
        f"/admin/ledger/transactions?wallet_id={receiver_wallet_id}&direction=incoming",
        headers=auth(admin["access_token"]),
    )
    invalid_amount_window = client.get(
        "/admin/ledger/transactions?min_gross_amount=10&max_gross_amount=1",
        headers=auth(admin["access_token"]),
    )
    missing_wallet_direction = client.get(
        "/admin/ledger/transactions?direction=outgoing",
        headers=auth(admin["access_token"]),
    )
    blank_transaction_type = client.get(
        "/admin/ledger/transactions?transaction_type=%20%20%20",
        headers=auth(admin["access_token"]),
    )
    blank_idempotency_key = client.get(
        "/admin/ledger/transactions?idempotency_key=%20%20%20",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/ledger/transactions?status=void", headers=auth(admin["access_token"]))

    assert transfer.status_code == 200
    assert outgoing.status_code == 200
    assert len(outgoing.json()) == 1
    row = outgoing.json()[0]
    assert row["id"] == transfer.json()["id"]
    assert row["transaction_type"] == "wallet_transfer"
    assert row["direction"] == "outgoing"
    assert row["gross_amount"] == "4.000000"
    assert row["platform_gas"] == "0.080000"
    assert row["metadata"]["note"] == "ledger filter"
    assert incoming.status_code == 200
    assert any(row["id"] == transfer.json()["id"] and row["direction"] == "incoming" for row in incoming.json())
    assert invalid_amount_window.status_code == 400
    assert missing_wallet_direction.status_code == 400
    assert blank_transaction_type.status_code == 422
    assert blank_transaction_type.json()["detail"] == "transaction_type cannot be blank"
    assert blank_idempotency_key.status_code == 422
    assert blank_idempotency_key.json()["detail"] == "idempotency_key cannot be blank"
    assert invalid_status.status_code == 422


def test_admin_ledger_entries_support_reconciliation_filters():
    sender = login("+919000000159", "Entry Filter Sender")
    receiver = login("+919000000160", "Entry Filter Receiver")
    admin = make_admin("+919999999037")
    today = utc_now().date().isoformat()

    transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "6.000000", "note": "entry filter"},
        headers=auth(sender["access_token"]),
    )
    transaction_id = transfer.json()["id"]
    sender_wallet_id = transfer.json()["from_wallet_id"]
    receiver_wallet_id = transfer.json()["to_wallet_id"]

    sender_debit = client.get(
        (
            f"/admin/ledger/entries?transaction_id={transaction_id}&wallet_id={sender_wallet_id}"
            f"&entry_type=DEBIT&balance_type=%20PURCHASED%20&min_amount=6&max_amount=6&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    receiver_credit = client.get(
        (
            f"/admin/ledger/entries?transaction_id={transaction_id}&wallet_id={receiver_wallet_id}"
            "&entry_type=CREDIT&balance_type=purchased&min_amount=5.88&max_amount=5.88"
        ),
        headers=auth(admin["access_token"]),
    )
    invalid_amount_window = client.get(
        "/admin/ledger/entries?min_amount=10&max_amount=1",
        headers=auth(admin["access_token"]),
    )
    invalid_entry_type = client.get("/admin/ledger/entries?entry_type=MOVE", headers=auth(admin["access_token"]))
    blank_balance_type = client.get(
        "/admin/ledger/entries?balance_type=%20%20%20",
        headers=auth(admin["access_token"]),
    )

    assert transfer.status_code == 200
    assert sender_debit.status_code == 200
    assert len(sender_debit.json()) == 1
    debit_row = sender_debit.json()[0]
    assert debit_row["transaction_id"] == transaction_id
    assert debit_row["wallet_id"] == sender_wallet_id
    assert debit_row["wallet"]["user"]["phone"] == sender["user"]["phone"]
    assert debit_row["direction"] == "debit"
    assert debit_row["amount"] == "6.000000"
    assert debit_row["signed_amount"] == "-6.000000"
    assert receiver_credit.status_code == 200
    assert len(receiver_credit.json()) == 1
    credit_row = receiver_credit.json()[0]
    assert credit_row["wallet"]["user"]["phone"] == receiver["user"]["phone"]
    assert credit_row["direction"] == "credit"
    assert credit_row["amount"] == "5.880000"
    assert credit_row["signed_amount"] == "5.880000"
    assert invalid_amount_window.status_code == 400
    assert invalid_entry_type.status_code == 422
    assert blank_balance_type.status_code == 422
    assert blank_balance_type.json()["detail"] == "balance_type cannot be blank"


def test_wallet_transfer_idempotency_rejects_key_reuse_with_different_payload():
    sender = login("+919000000131", "Transfer Conflict Sender")
    receiver = login("+919000000132", "Transfer Conflict Receiver")
    headers = {**auth(sender["access_token"]), "X-Idempotency-Key": "transfer_conflict_001"}

    first = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "5.000000", "note": "first payload"},
        headers=headers,
    )
    conflict = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "6.000000", "note": "first payload"},
        headers=headers,
    )
    note_conflict = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "5.000000", "note": "changed payload"},
        headers=headers,
    )
    sender_balance = client.get("/wallet/balance", headers=auth(sender["access_token"]))
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"]))

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency key was already used for a different transfer"
    assert note_conflict.status_code == 409
    assert sender_balance.json()["purchased_balance"] == "15.000000"
    assert receiver_balance.json()["purchased_balance"] == "24.900000"


def test_wallet_transfer_rejects_invalid_idempotency_key():
    sender = login("+919000000129", "Transfer Bad Key Sender")
    receiver = login("+919000000130", "Transfer Bad Key Receiver")

    invalid = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.000000"},
        headers={**auth(sender["access_token"]), "X-Idempotency-Key": "bad key!"},
    )
    blank = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.000000"},
        headers={**auth(sender["access_token"]), "X-Idempotency-Key": "   "},
    )

    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Idempotency key has invalid format"
    assert blank.status_code == 400
    assert blank.json()["detail"] == "Idempotency key cannot be blank"


def test_wallet_transfer_rejects_blocked_and_self_transfers():
    sender = login("+919000000114", "Blocked Transfer Sender")
    receiver = login("+919000000115", "Blocked Transfer Receiver")

    self_transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": sender["user"]["id"], "amount": "1.000000"},
        headers=auth(sender["access_token"]),
    )
    block = client.post(f"/users/{receiver['user']['id']}/block", headers=auth(sender["access_token"]))
    blocked_transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "1.000000"},
        headers=auth(sender["access_token"]),
    )

    assert self_transfer.status_code == 400
    assert self_transfer.json()["detail"] == "Cannot transfer coins to yourself"
    assert block.status_code == 200
    assert blocked_transfer.status_code == 403
    assert blocked_transfer.json()["detail"] == "Transfer is blocked"


def test_dev_recharge_capture_credits_purchased_balance():
    user = login("+919000000003", "Recharge User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    )
    assert order.status_code == 200

    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order.json()["gateway_order_id"]},
        headers=auth(user["access_token"]),
    )
    assert capture.status_code == 200
    assert capture.json()["status"] == "credited"

    balance = client.get("/wallet/balance", headers=auth(user["access_token"])).json()
    assert balance["purchased_balance"] == "120.000000"


def test_paid_message_idempotency_prevents_double_charge():
    user_a = login("+919000000004", "Retry Sender")
    user_b = login("+919000000005", "Retry Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": user_b["user"]["id"]},
        headers=auth(user_a["access_token"]),
    ).json()

    headers = {**auth(user_a["access_token"]), "X-Idempotency-Key": "msg_retry_001"}
    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": user_b["user"]["id"], "content": "Retry-safe message"},
        headers=headers,
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": user_b["user"]["id"], "content": "Retry-safe message"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    balance_a = client.get("/wallet/balance", headers=auth(user_a["access_token"])).json()
    balance_b = client.get("/wallet/balance", headers=auth(user_b["access_token"])).json()
    assert balance_a["spendable_balance"] == "19.000000"
    assert balance_b["locked_balance"] == "0.650000"


def test_paid_message_rewards_are_capped_per_receiver_day(monkeypatch):
    monkeypatch.setattr(settings, "new_user_daily_reward_cap", Decimal("1.000000"))
    sender = login("+919000000119", "Reward Cap Sender")
    receiver = login("+919000000120", "Reward Cap Receiver")
    admin = make_admin("+919999999024")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Reward cap one"},
        headers=auth(sender["access_token"]),
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Reward cap two"},
        headers=auth(sender["access_token"]),
    )
    third = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Reward cap three"},
        headers=auth(sender["access_token"]),
    )
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"]))
    receiver_history = client.get("/wallet/transactions", headers=auth(receiver["access_token"]))
    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert receiver_balance.json()["locked_balance"] == "1.000000"
    transactions = {row["id"]: row for row in receiver_history.json()}
    assert transactions[first.json()["transaction_id"]]["receiver_reward"] == "0.650000"
    assert transactions[first.json()["transaction_id"]]["reserve_amount"] == "0.100000"
    assert transactions[second.json()["transaction_id"]]["receiver_reward"] == "0.350000"
    assert transactions[second.json()["transaction_id"]]["reserve_amount"] == "0.400000"
    assert transactions[second.json()["transaction_id"]]["metadata"]["reward_cap_applied"] is True
    assert transactions[third.json()["transaction_id"]]["receiver_reward"] == "0.000000"
    assert transactions[third.json()["transaction_id"]]["reserve_amount"] == "0.750000"
    assert transactions[third.json()["transaction_id"]]["metadata"]["reward_cap_applied"] is True
    assert audit.json()["imbalanced_count"] == 0


def test_duplicate_message_content_uses_hash_for_fraud_reward_suppression():
    sender = login("+919000000133", "Duplicate Content Sender")
    receiver = login("+919000000134", "Duplicate Content Receiver")
    admin = make_admin("+919999999026")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    sent = []
    for _ in range(4):
        response = client.post(
            f"/chats/{chat['id']}/messages",
            json={"receiver_id": receiver["user"]["id"], "content": "  Repeated   Promo  "},
            headers=auth(sender["access_token"]),
        )
        assert response.status_code == 200
        sent.append(response.json())

    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"]))
    receiver_history = client.get("/wallet/transactions", headers=auth(receiver["access_token"]))
    fraud_events = client.get("/admin/fraud", headers=auth(admin["access_token"]))
    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"]))

    transactions = {row["id"]: row for row in receiver_history.json()}
    fourth = transactions[sent[3]["transaction_id"]]
    assert receiver_balance.json()["locked_balance"] == "1.950000"
    assert fourth["receiver_reward"] == "0.000000"
    assert fourth["reserve_amount"] == "0.750000"
    assert fourth["metadata"]["fraud_risk"] is True
    assert fourth["metadata"]["fraud_reason"] == "duplicate_content"
    assert any(event["event_type"] == "duplicate_content" for event in fraud_events.json())
    assert audit.json()["imbalanced_count"] == 0


def test_http_message_rejects_oversized_content():
    sender = login("+919000000056", "HTTP Large Sender")
    receiver = login("+919000000057", "HTTP Large Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    response = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "x" * 4001},
        headers=auth(sender["access_token"]),
    )

    assert response.status_code == 422


def test_http_message_rejects_blank_content_before_ledger_mutation():
    sender = login("+919000000135", "Blank Message Sender")
    receiver = login("+919000000136", "Blank Message Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    before_balance = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()
    with SessionLocal() as db:
        before_transactions = db.query(LedgerTransaction).count()

    response = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "   "},
        headers=auth(sender["access_token"]),
    )

    after_balance = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()
    with SessionLocal() as db:
        after_transactions = db.query(LedgerTransaction).count()

    assert response.status_code == 422
    assert after_balance == before_balance
    assert after_transactions == before_transactions


def test_http_message_trims_content_before_storage_and_hashing():
    sender = login("+919000000137", "Trim Message Sender")
    receiver = login("+919000000138", "Trim Message Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    response = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "  Trimmed hello  "},
        headers=auth(sender["access_token"]),
    )

    assert response.status_code == 200
    assert response.json()["content"] == "Trimmed hello"


def test_http_message_rejects_invalid_idempotency_key():
    sender = login("+919000000058", "HTTP Bad Idempotency Sender")
    receiver = login("+919000000059", "HTTP Bad Idempotency Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    oversized = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Bad key"},
        headers={**auth(sender["access_token"]), "X-Idempotency-Key": "x" * 121},
    )
    blank = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Blank key"},
        headers={**auth(sender["access_token"]), "X-Idempotency-Key": "   "},
    )
    invalid = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Invalid key"},
        headers={**auth(sender["access_token"]), "X-Idempotency-Key": "bad key!"},
    )

    assert oversized.status_code == 400
    assert oversized.json()["detail"] == "Idempotency key too large"
    assert blank.status_code == 400
    assert blank.json()["detail"] == "Idempotency key cannot be blank"
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Idempotency key has invalid format"


def test_message_velocity_limit_blocks_before_wallet_deduction(monkeypatch):
    monkeypatch.setattr(settings, "message_max_sends_per_minute", 1)
    sender = login("+919000000075", "Velocity Sender")
    receiver = login("+919000000076", "Velocity Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Velocity one"},
        headers=auth(sender["access_token"]),
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Velocity two"},
        headers=auth(sender["access_token"]),
    )

    balance = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "Message rate limit exceeded"
    assert balance["spendable_balance"] == "19.000000"


def test_blocked_receiver_cannot_be_chat_target_or_receive_messages():
    admin = make_admin("+919999999998")
    sender = login("+919000000077", "Blocked Target Sender")
    receiver = login("+919000000078", "Blocked Target Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    blocked = client.post(f"/admin/users/{receiver['user']['id']}/block", headers=auth(admin["access_token"]))
    send_after_block = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Should not send"},
        headers=auth(sender["access_token"]),
    )
    new_chat_after_block = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    )
    balance = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()

    assert blocked.status_code == 200
    assert send_after_block.status_code == 403
    assert send_after_block.json()["detail"] == "Receiver is not active"
    assert new_chat_after_block.status_code == 403
    assert new_chat_after_block.json()["detail"] == "Receiver is not active"
    assert balance["spendable_balance"] == "20.000000"


def test_cannot_create_chat_with_self():
    user = login("+919000000091", "Self Chat User")

    response = client.post(
        "/chats",
        json={"receiver_id": user["user"]["id"]},
        headers=auth(user["access_token"]),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot create a chat with yourself"


def test_cannot_send_message_to_self_even_with_legacy_conversation():
    user = login("+919000000092", "Self Send User")
    with SessionLocal() as db:
        user_row = db.query(User).filter(User.id == user["user"]["id"]).one()
        conversation = Conversation(created_by=user_row.id)
        db.add(conversation)
        db.flush()
        db.add(ConversationMember(conversation_id=conversation.id, user_id=user_row.id))
        db.commit()
        conversation_id = str(conversation.id)

    response = client.post(
        f"/chats/{conversation_id}/messages",
        json={"receiver_id": user["user"]["id"], "content": "No self-send"},
        headers=auth(user["access_token"]),
    )
    balance = client.get("/wallet/balance", headers=auth(user["access_token"])).json()

    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot send a message to yourself"
    assert balance["spendable_balance"] == "20.000000"


def test_direct_chat_reuses_oldest_existing_duplicate():
    sender = login("+919000000093", "Duplicate Chat Sender")
    receiver = login("+919000000094", "Duplicate Chat Receiver")
    with SessionLocal() as db:
        sender_row = db.query(User).filter(User.id == sender["user"]["id"]).one()
        receiver_row = db.query(User).filter(User.id == receiver["user"]["id"]).one()
        older = Conversation(created_by=sender_row.id, created_at=utc_now() - timedelta(days=1))
        newer = Conversation(created_by=receiver_row.id, created_at=utc_now())
        db.add_all([older, newer])
        db.flush()
        db.add_all(
            [
                ConversationMember(conversation_id=older.id, user_id=sender_row.id),
                ConversationMember(conversation_id=older.id, user_id=receiver_row.id),
                ConversationMember(conversation_id=newer.id, user_id=sender_row.id),
                ConversationMember(conversation_id=newer.id, user_id=receiver_row.id),
            ]
        )
        db.commit()
        older_id = str(older.id)

    response = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    )

    assert response.status_code == 200
    assert response.json()["id"] == older_id


def test_only_receiver_can_mark_message_read():
    sender = login("+919000000064", "Read Sender")
    receiver = login("+919000000065", "Read Receiver")
    outsider = login("+919000000066", "Read Outsider")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Please mark read"},
        headers=auth(sender["access_token"]),
    ).json()

    sender_denied = client.post(f"/messages/{sent['id']}/read", headers=auth(sender["access_token"]))
    outsider_denied = client.post(f"/messages/{sent['id']}/read", headers=auth(outsider["access_token"]))
    receiver_allowed = client.post(f"/messages/{sent['id']}/read", headers=auth(receiver["access_token"]))
    messages = client.get(f"/chats/{chat['id']}/messages", headers=auth(receiver["access_token"])).json()

    assert sender_denied.status_code == 403
    assert outsider_denied.status_code == 403
    assert receiver_allowed.status_code == 200
    assert receiver_allowed.json()["delivered_at"] is not None
    assert receiver_allowed.json()["read_at"] is not None
    assert messages[0]["status"] == "read"
    assert messages[0]["delivered_at"] is not None
    assert messages[0]["read_at"] is not None


def test_chat_list_includes_last_message_preview_and_unread_count():
    sender = login("+919000000125", "Inbox Preview Sender")
    receiver = login("+919000000126", "Inbox Preview Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "First inbox preview"},
        headers=auth(sender["access_token"]),
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Latest inbox preview"},
        headers=auth(sender["access_token"]),
    )

    receiver_chats = client.get("/chats", headers=auth(receiver["access_token"]))
    sender_chats = client.get("/chats", headers=auth(sender["access_token"]))
    client.post(f"/messages/{first.json()['id']}/read", headers=auth(receiver["access_token"]))
    receiver_chats_after_read = client.get("/chats", headers=auth(receiver["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 200
    assert receiver_chats.status_code == 200
    receiver_row = next(row for row in receiver_chats.json() if row["id"] == chat["id"])
    sender_row = next(row for row in sender_chats.json() if row["id"] == chat["id"])
    after_read_row = next(row for row in receiver_chats_after_read.json() if row["id"] == chat["id"])
    assert receiver_row["last_message"]["id"] == second.json()["id"]
    assert receiver_row["last_message"]["content"] == "Latest inbox preview"
    assert receiver_row["unread_count"] == 2
    assert sender_row["unread_count"] == 0
    assert after_read_row["unread_count"] == 1


def test_receiver_can_mark_whole_conversation_read():
    sender = login("+919000000135", "Bulk Read Sender")
    receiver = login("+919000000136", "Bulk Read Receiver")
    outsider = login("+919000000137", "Bulk Read Outsider")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Bulk read one"},
        headers=auth(sender["access_token"]),
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Bulk read two"},
        headers=auth(sender["access_token"]),
    )

    outsider_denied = client.post(f"/chats/{chat['id']}/read", headers=auth(outsider["access_token"]))
    read_all = client.post(f"/chats/{chat['id']}/read", headers=auth(receiver["access_token"]))
    read_again = client.post(f"/chats/{chat['id']}/read", headers=auth(receiver["access_token"]))
    receiver_chats = client.get("/chats", headers=auth(receiver["access_token"]))
    messages = client.get(f"/chats/{chat['id']}/messages", headers=auth(receiver["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 200
    assert outsider_denied.status_code == 403
    assert read_all.status_code == 200
    assert read_all.json()["read_count"] == 2
    assert read_all.json()["read_at"] is not None
    assert read_again.status_code == 200
    assert read_again.json() == {"status": "ok", "read_count": 0, "read_at": None}
    receiver_row = next(row for row in receiver_chats.json() if row["id"] == chat["id"])
    assert receiver_row["unread_count"] == 0
    assert all(row["status"] == "read" for row in messages.json())
    assert all(row["read_at"] is not None for row in messages.json())


def test_receiver_can_mark_message_delivered_once():
    sender = login("+919000000097", "Delivery Sender")
    receiver = login("+919000000098", "Delivery Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Please mark delivered"},
        headers=auth(sender["access_token"]),
    ).json()

    sender_denied = client.post(f"/messages/{sent['id']}/delivered", headers=auth(sender["access_token"]))
    first = client.post(f"/messages/{sent['id']}/delivered", headers=auth(receiver["access_token"]))
    second = client.post(f"/messages/{sent['id']}/delivered", headers=auth(receiver["access_token"]))
    messages = client.get(f"/chats/{chat['id']}/messages", headers=auth(receiver["access_token"])).json()

    assert sender_denied.status_code == 403
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["delivered_at"] is not None
    assert first.json()["read_at"] is None
    assert second.json()["delivered_at"] == first.json()["delivered_at"]
    assert messages[0]["status"] == "delivered"
    assert messages[0]["delivered_at"] == first.json()["delivered_at"]
    assert messages[0]["read_at"] is None


def test_chat_messages_reject_non_member_access():
    sender = login("+919000000088", "Private Chat Sender")
    receiver = login("+919000000089", "Private Chat Receiver")
    outsider = login("+919000000090", "Private Chat Outsider")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    allowed = client.get(f"/chats/{chat['id']}/messages", headers=auth(sender["access_token"]))
    denied = client.get(f"/chats/{chat['id']}/messages", headers=auth(outsider["access_token"]))

    assert allowed.status_code == 200
    assert allowed.json() == []
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Conversation access denied"


def test_mark_read_returns_404_for_missing_message():
    user = login("+919000000067", "Missing Read User")

    response = client.post("/messages/00000000-0000-0000-0000-000000000000/read", headers=auth(user["access_token"]))

    assert response.status_code == 404


def test_free_quota_message_does_not_charge_or_reward():
    sender = login("+919000000031", "Free Sender")
    receiver = login("+919000000032", "Free Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Free quota hello", "use_free_quota": True},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 200
    assert sent.json()["coin_cost"] == "0.000000"
    assert sent.json()["transaction_id"] is None

    balance_sender = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()
    balance_receiver = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    assert balance_sender["spendable_balance"] == "20.000000"
    assert balance_receiver["locked_balance"] == "0.000000"


def test_free_quota_falls_back_to_paid_after_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "new_user_daily_free_messages", 1)
    sender = login("+919000000033", "Quota Sender")
    receiver = login("+919000000034", "Quota Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    first = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Free one", "use_free_quota": True},
        headers=auth(sender["access_token"]),
    )
    second = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Paid fallback", "use_free_quota": True},
        headers=auth(sender["access_token"]),
    )

    assert first.status_code == 200
    assert first.json()["coin_cost"] == "0.000000"
    assert second.status_code == 200
    assert second.json()["coin_cost"] == "1.000000"

    balance_sender = client.get("/wallet/balance", headers=auth(sender["access_token"])).json()
    balance_receiver = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    assert balance_sender["spendable_balance"] == "19.000000"
    assert balance_receiver["locked_balance"] == "0.650000"


def test_admin_messages_support_metadata_filters():
    sender = login("+919000000148", "Admin Message Sender")
    receiver = login("+919000000149", "Admin Message Receiver")
    admin = make_admin("+919999999033")
    today = utc_now().date().isoformat()
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    paid = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Admin message paid"},
        headers=auth(sender["access_token"]),
    )
    free = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Admin message free", "use_free_quota": True},
        headers=auth(sender["access_token"]),
    )
    read = client.post(f"/messages/{paid.json()['id']}/read", headers=auth(receiver["access_token"]))

    with SessionLocal() as db:
        paid_message = db.get(Message, paid.json()["id"])
        paid_hash = paid_message.content_hash

    paid_rows = client.get(
        (
            "/admin/messages"
            f"?conversation_id={chat['id']}&sender_id={sender['user']['id']}&receiver_id={receiver['user']['id']}"
            f"&message_type=%20TEXT%20&status=read&transaction_id={paid.json()['transaction_id']}"
            f"&content_hash=%20{paid_hash.upper()}%20"
            f"&min_coin_cost=1&max_coin_cost=1&has_transaction=true&delivered=true&read=true"
            f"&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    free_rows = client.get(
        f"/admin/messages?has_transaction=false&max_coin_cost=0&conversation_id={chat['id']}",
        headers=auth(admin["access_token"]),
    )
    invalid_cost_window = client.get(
        "/admin/messages?min_coin_cost=2&max_coin_cost=1",
        headers=auth(admin["access_token"]),
    )
    blank_message_type = client.get("/admin/messages?message_type=%20%20%20", headers=auth(admin["access_token"]))
    blank_hash = client.get("/admin/messages?content_hash=%20%20%20", headers=auth(admin["access_token"]))
    invalid_hash = client.get("/admin/messages?content_hash=short", headers=auth(admin["access_token"]))
    invalid_status = client.get("/admin/messages?status=deleted", headers=auth(admin["access_token"]))

    assert paid.status_code == 200
    assert free.status_code == 200
    assert read.status_code == 200
    assert paid_rows.status_code == 200
    assert len(paid_rows.json()) == 1
    row = paid_rows.json()[0]
    assert row["id"] == paid.json()["id"]
    assert row["coin_cost"] == "1.000000"
    assert row["sender"]["id"] == sender["user"]["id"]
    assert row["receiver"]["id"] == receiver["user"]["id"]
    assert row["content_hash"] == paid_hash
    assert "encrypted_content" not in row
    assert free_rows.status_code == 200
    assert any(row["id"] == free.json()["id"] and row["transaction_id"] is None for row in free_rows.json())
    assert invalid_cost_window.status_code == 400
    assert blank_message_type.status_code == 422
    assert blank_message_type.json()["detail"] == "message_type cannot be blank"
    assert blank_hash.status_code == 422
    assert blank_hash.json()["detail"] == "content_hash cannot be blank"
    assert invalid_hash.status_code == 422
    assert invalid_hash.json()["detail"] == "content_hash must be 64 characters"
    assert invalid_status.status_code == 422


def test_recharge_packs_are_available_to_authenticated_users():
    user = login("+919000000082", "Recharge Pack User")

    response = client.get("/payments/recharge-packs", headers=auth(user["access_token"]))

    assert response.status_code == 200
    packs = response.json()
    assert [pack["id"] for pack in packs] == ["starter_99", "growth_299", "power_999"]
    assert packs[0] == {
        "id": "starter_99",
        "amount": "99.00",
        "currency": "INR",
        "coins": "100.000000",
    }


def test_recharge_order_normalizes_and_validates_pack_id():
    user = login("+919000009907", "Pack Normalize User")

    normalized = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "  STARTER_99  "},
        headers=auth(user["access_token"]),
    )
    blank = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "   "},
        headers=auth(user["access_token"]),
    )

    assert normalized.status_code == 200
    assert normalized.json()["coins"] == "100.000000"
    assert normalized.json()["amount"] == "99.00"
    assert blank.status_code == 422


def test_payment_capture_idempotency_prevents_double_credit():
    user = login("+919000000006", "Payment Retry User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    payload = {"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_retry_001"}
    first = client.post("/payments/dev/capture", json=payload, headers=auth(user["access_token"]))
    second = client.post("/payments/dev/capture", json=payload, headers=auth(user["access_token"]))

    assert first.json()["status"] == "credited"
    assert second.json()["status"] == "already_processed"

    balance = client.get("/wallet/balance", headers=auth(user["access_token"])).json()
    assert balance["purchased_balance"] == "120.000000"


def test_dev_capture_normalizes_gateway_ids():
    user = login("+919000009908", "Dev Capture Normalize User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": f"  {order['gateway_order_id']}  ", "gateway_payment_id": "   "},
        headers=auth(user["access_token"]),
    )
    history = client.get("/payments/history", headers=auth(user["access_token"]))

    assert capture.status_code == 200
    assert capture.json()["status"] == "credited"
    assert history.status_code == 200
    assert history.json()[0]["gateway_order_id"] == order["gateway_order_id"]
    assert history.json()[0]["gateway_payment_id"].startswith("pay_dev_")


def test_payment_gateway_order_id_unique_index_blocks_duplicates():
    user = login("+919000000079", "Unique Gateway Order User")

    with SessionLocal() as db:
        user_row = db.query(User).filter(User.id == user["user"]["id"]).one()
        db.add_all(
            [
                PaymentOrder(
                    user_id=user_row.id,
                    gateway_order_id="order_unique_db_001",
                    amount="99.00",
                    coins_to_credit="120.000000",
                ),
                PaymentOrder(
                    user_id=user_row.id,
                    gateway_order_id="order_unique_db_001",
                    amount="99.00",
                    coins_to_credit="120.000000",
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_user_can_create_same_recharge_pack_multiple_times():
    user = login("+919000000080", "Repeat Recharge User")

    first = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    )
    second = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["gateway_order_id"] != second.json()["gateway_order_id"]


def test_dev_capture_without_payment_id_credits_multiple_orders():
    user = login("+919000000081", "Repeat Capture User")
    first_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    second_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    first_capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": first_order["gateway_order_id"]},
        headers=auth(user["access_token"]),
    )
    second_capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": second_order["gateway_order_id"]},
        headers=auth(user["access_token"]),
    )
    balance = client.get("/wallet/balance", headers=auth(user["access_token"])).json()

    assert first_capture.status_code == 200
    assert first_capture.json()["status"] == "credited"
    assert second_capture.status_code == 200
    assert second_capture.json()["status"] == "credited"
    assert balance["purchased_balance"] == "220.000000"


def test_payment_history_includes_gateway_payment_id_after_capture():
    user = login("+919000000083", "Payment History User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_history_001"},
        headers=auth(user["access_token"]),
    )
    history = client.get("/payments/history", headers=auth(user["access_token"]))

    assert capture.status_code == 200
    assert history.status_code == 200
    latest = history.json()[0]
    assert latest["gateway_order_id"] == order["gateway_order_id"]
    assert latest["gateway_payment_id"] == "pay_history_001"
    assert latest["status"] == "success"


def test_payment_history_filters_by_status_gateway_and_date():
    user = login("+919000000136", "Payment Filter User")
    today = utc_now().date().isoformat()
    first_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    second_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": first_order["gateway_order_id"], "gateway_payment_id": "pay_filter_001"},
        headers=auth(user["access_token"]),
    )
    successful = client.get(
        f"/payments/history?status=success&gateway=razorpay&start_date={today}&end_date={today}",
        headers=auth(user["access_token"]),
    )
    padded_gateway = client.get(
        f"/payments/history?gateway=%20RAZORPAY%20&start_date={today}&end_date={today}",
        headers=auth(user["access_token"]),
    )
    created = client.get("/payments/history?status=created", headers=auth(user["access_token"]))
    invalid_status = client.get("/payments/history?status=refunded", headers=auth(user["access_token"]))
    blank_gateway = client.get("/payments/history?gateway=%20%20%20", headers=auth(user["access_token"]))
    invalid_window = client.get(
        "/payments/history?start_date=2026-04-30&end_date=2026-04-29",
        headers=auth(user["access_token"]),
    )

    assert capture.status_code == 200
    assert successful.status_code == 200
    assert [row["gateway_order_id"] for row in successful.json()] == [first_order["gateway_order_id"]]
    assert padded_gateway.status_code == 200
    assert {row["gateway_order_id"] for row in padded_gateway.json()} == {
        first_order["gateway_order_id"],
        second_order["gateway_order_id"],
    }
    assert created.status_code == 200
    assert [row["gateway_order_id"] for row in created.json()] == [second_order["gateway_order_id"]]
    assert invalid_status.status_code == 422
    assert blank_gateway.status_code == 422
    assert blank_gateway.json()["detail"] == "gateway cannot be blank"
    assert invalid_window.status_code == 400


def test_admin_payments_support_global_filters():
    user = login("+919000000143", "Admin Payment User")
    admin = make_admin("+919999999030")
    today = utc_now().date().isoformat()
    successful_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    pending_order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "growth_299"},
        headers=auth(user["access_token"]),
    ).json()
    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": successful_order["gateway_order_id"], "gateway_payment_id": "pay_admin_filter_001"},
        headers=auth(user["access_token"]),
    )

    successful = client.get(
        (
            "/admin/payments"
            f"?status=success&gateway=%20RAZORPAY%20&user_id={user['user']['id']}"
            f"&gateway_order_id=%20{successful_order['gateway_order_id']}%20"
            "&gateway_payment_id=%20pay_admin_filter_001%20"
            f"&currency=%20inr%20&min_amount=90&max_amount=100&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    pending = client.get(
        f"/admin/payments?status=created&gateway_order_id={pending_order['gateway_order_id']}",
        headers=auth(admin["access_token"]),
    )
    invalid_amount_window = client.get(
        "/admin/payments?min_amount=100&max_amount=10",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/payments?status=refunded", headers=auth(admin["access_token"]))
    blank_gateway = client.get("/admin/payments?gateway=%20%20%20", headers=auth(admin["access_token"]))

    assert capture.status_code == 200
    assert successful.status_code == 200
    assert len(successful.json()) == 1
    row = successful.json()[0]
    assert row["gateway_order_id"] == successful_order["gateway_order_id"]
    assert row["gateway_payment_id"] == "pay_admin_filter_001"
    assert row["amount"] == "99.00"
    assert row["coins_to_credit"] == "100.000000"
    assert row["user"]["id"] == user["user"]["id"]
    assert pending.status_code == 200
    assert [row["gateway_order_id"] for row in pending.json()] == [pending_order["gateway_order_id"]]
    assert invalid_amount_window.status_code == 400
    assert invalid_status.status_code == 422
    assert blank_gateway.status_code == 422
    assert blank_gateway.json()["detail"] == "gateway cannot be blank"


def test_admin_can_mark_created_payment_failed_without_crediting_wallet():
    user = login("+919000000162", "Failed Payment User")
    admin = make_admin("+919999999039")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    failed = client.post(f"/admin/payments/{order['payment_order_id']}/fail", headers=auth(admin["access_token"]))
    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_failed_late_001"},
        headers=auth(user["access_token"]),
    )
    balance = client.get("/wallet/balance", headers=auth(user["access_token"]))
    failed_rows = client.get(
        f"/admin/payments?status=failed&gateway_order_id={order['gateway_order_id']}",
        headers=auth(admin["access_token"]),
    )
    audit_log = client.get(
        f"/admin/audit-logs?action=payment.fail&target_id={order['payment_order_id']}",
        headers=auth(admin["access_token"]),
    )

    assert failed.status_code == 200
    assert failed.json()["id"] == order["payment_order_id"]
    assert failed.json()["status"] == "failed"
    assert capture.status_code == 200
    assert capture.json()["status"] == "payment_order_failed"
    assert balance.json()["purchased_balance"] == "20.000000"
    assert failed_rows.status_code == 200
    assert [row["id"] for row in failed_rows.json()] == [order["payment_order_id"]]
    assert audit_log.status_code == 200
    assert audit_log.json()[0]["metadata"]["previous_status"] == "created"


def test_admin_cannot_mark_successful_payment_failed():
    user = login("+919000000163", "Successful Payment User")
    admin = make_admin("+919999999040")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_success_no_fail_001"},
        headers=auth(user["access_token"]),
    )

    failed = client.post(f"/admin/payments/{order['payment_order_id']}/fail", headers=auth(admin["access_token"]))
    balance = client.get("/wallet/balance", headers=auth(user["access_token"]))

    assert capture.status_code == 200
    assert capture.json()["status"] == "credited"
    assert failed.status_code == 409
    assert failed.json()["detail"] == "Successful payment orders cannot be marked failed"
    assert balance.json()["purchased_balance"] == "120.000000"


def test_dev_capture_is_hidden_when_disabled(monkeypatch):
    user = login("+919000000010", "Production Payment User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()

    monkeypatch.setattr(settings, "enable_dev_payment_capture", False)
    response = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"]},
        headers=auth(user["access_token"]),
    )

    assert response.status_code == 404


def test_razorpay_webhook_rejects_invalid_signature(monkeypatch):
    user = login("+919000000011", "Webhook User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_bad_sig",
                    "order_id": order["gateway_order_id"],
                    "amount": 9900,
                    "currency": "INR",
                }
            }
        },
    }

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")
    response = client.post(
        "/payments/razorpay/webhook",
        json=payload,
        headers={"X-Razorpay-Signature": "bad_signature"},
    )

    assert response.status_code == 400


def test_razorpay_webhook_accepts_valid_signature(monkeypatch):
    user = login("+919000000012", "Webhook Valid User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_valid_sig",
                    "order_id": order["gateway_order_id"],
                    "amount": 9900,
                    "currency": "INR",
                }
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"webhook_secret", raw_body, sha256).hexdigest()

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")
    response = client.post(
        "/payments/razorpay/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "credited"


def test_razorpay_failed_webhook_marks_order_failed_without_credit(monkeypatch):
    user = login("+919000000164", "Webhook Failed User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_failed_webhook_001",
                    "order_id": order["gateway_order_id"],
                    "amount": 9900,
                    "currency": "INR",
                }
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"webhook_secret", raw_body, sha256).hexdigest()

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")
    response = client.post(
        "/payments/razorpay/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )
    balance = client.get("/wallet/balance", headers=auth(user["access_token"]))
    history = client.get("/payments/history?status=failed", headers=auth(user["access_token"]))
    late_capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_late_after_failed_webhook"},
        headers=auth(user["access_token"]),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert balance.json()["purchased_balance"] == "20.000000"
    assert history.status_code == 200
    assert history.json()[0]["gateway_order_id"] == order["gateway_order_id"]
    assert history.json()[0]["gateway_payment_id"] == "pay_failed_webhook_001"
    assert history.json()[0]["status"] == "failed"
    assert late_capture.status_code == 200
    assert late_capture.json()["status"] == "payment_order_failed"


def test_razorpay_failed_webhook_does_not_downgrade_successful_order(monkeypatch):
    user = login("+919000000165", "Webhook Success Then Failed User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    capture = client.post(
        "/payments/dev/capture",
        json={"gateway_order_id": order["gateway_order_id"], "gateway_payment_id": "pay_success_before_failed_001"},
        headers=auth(user["access_token"]),
    )
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_success_before_failed_001",
                    "order_id": order["gateway_order_id"],
                    "amount": 9900,
                    "currency": "INR",
                }
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"webhook_secret", raw_body, sha256).hexdigest()

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")
    response = client.post(
        "/payments/razorpay/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )
    history = client.get("/payments/history?status=success", headers=auth(user["access_token"]))

    assert capture.status_code == 200
    assert capture.json()["status"] == "credited"
    assert response.status_code == 200
    assert response.json()["status"] == "already_processed"
    assert history.json()[0]["gateway_order_id"] == order["gateway_order_id"]


def test_razorpay_webhook_rejects_amount_mismatch(monkeypatch):
    user = login("+919000000047", "Webhook Amount User")
    order = client.post(
        "/payments/razorpay/order",
        json={"pack_id": "starter_99"},
        headers=auth(user["access_token"]),
    ).json()
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_wrong_amount",
                    "order_id": order["gateway_order_id"],
                    "amount": 100,
                    "currency": "INR",
                }
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"webhook_secret", raw_body, sha256).hexdigest()

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")
    response = client.post(
        "/payments/razorpay/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )
    balance = client.get("/wallet/balance", headers=auth(user["access_token"])).json()

    assert response.status_code == 400
    assert response.json()["detail"] == "Payment amount or currency mismatch"
    assert balance["purchased_balance"] == "20.000000"


def test_razorpay_webhook_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_max_bytes", 16)

    response = client.post(
        "/payments/razorpay/webhook",
        content=b'{"event":"payment.captured","padding":"too-large"}',
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": "ignored"},
    )

    assert response.status_code == 413


def test_razorpay_webhook_rejects_malformed_signed_payload(monkeypatch):
    raw_body = b'{"event":"payment.captured","payload":{}}'
    signature = hmac.new(b"webhook_secret", raw_body, sha256).hexdigest()
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "webhook_secret")

    response = client.post(
        "/payments/razorpay/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid payment webhook payload"


def test_ledger_rejects_negative_transaction_amount():
    with SessionLocal() as db:
        db.add(LedgerTransaction(transaction_type="bad_adjustment", gross_amount="-1.000000"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_wallet_entry_rejects_invalid_entry_type():
    with SessionLocal() as db:
        wallet = Wallet(wallet_type="constraint_test")
        db.add(wallet)
        db.flush()
        transaction = LedgerTransaction(transaction_type="constraint_test", gross_amount="1.000000")
        db.add(transaction)
        db.flush()
        db.add(WalletEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            entry_type="MOVE",
            amount="1.000000",
            balance_type="purchased",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_admin_ledger_audit_passes_after_economy_activity():
    sender = login("+919000000013", "Audit Sender")
    receiver = login("+919000000014", "Audit Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Audit this transaction"},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 200

    admin = make_admin("+919999999003")
    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"])).json()

    assert audit["checked"] > 0
    assert audit["imbalanced_count"] == 0


def test_admin_ledger_audit_reports_imbalanced_transaction():
    admin = make_admin("+919999999004")
    with SessionLocal() as db:
        db.add(LedgerTransaction(transaction_type="manual_bad", gross_amount="1.000000"))
        db.commit()

    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"])).json()

    assert audit["imbalanced_count"] >= 1
    assert any(item["transaction_type"] == "manual_bad" for item in audit["imbalanced"])

    with SessionLocal() as db:
        db.query(LedgerTransaction).filter(LedgerTransaction.transaction_type == "manual_bad").delete()
        db.commit()


def test_admin_generates_stable_daily_settlement_hash():
    sender = login("+919000000035", "Settlement Sender")
    receiver = login("+919000000036", "Settlement Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Settlement anchor"},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 200

    admin = make_admin("+919999999010")
    settlement_date = utc_now().date().isoformat()
    first = client.post(f"/admin/settlements/{settlement_date}", headers=auth(admin["access_token"]))
    second = client.post(f"/admin/settlements/{settlement_date}", headers=auth(admin["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first.json()["ledger_hash"]) == 64
    assert first.json()["ledger_hash"] == second.json()["ledger_hash"]
    assert first.json()["transaction_count"] >= 1
    assert first.json()["entry_count"] >= 1

    rows = client.get("/admin/settlements", headers=auth(admin["access_token"]))
    padded_hash = f"%20{first.json()['ledger_hash'].upper()}%20"
    filtered = client.get(
        (
            f"/admin/settlements?status=generated&ledger_hash={padded_hash}"
            f"&start_date={settlement_date}&end_date={settlement_date}"
            "&min_transaction_count=1&min_entry_count=1"
        ),
        headers=auth(admin["access_token"]),
    )
    invalid_window = client.get(
        "/admin/settlements?start_date=2026-04-30&end_date=2026-04-29",
        headers=auth(admin["access_token"]),
    )
    invalid_hash = client.get("/admin/settlements?ledger_hash=short", headers=auth(admin["access_token"]))
    blank_hash = client.get("/admin/settlements?ledger_hash=%20%20%20", headers=auth(admin["access_token"]))
    invalid_status = client.get("/admin/settlements?status=closed", headers=auth(admin["access_token"]))

    assert rows.status_code == 200
    assert any(row["settlement_date"] == settlement_date for row in rows.json())
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()] == [first.json()["id"]]
    assert invalid_window.status_code == 400
    assert invalid_hash.status_code == 422
    assert invalid_hash.json()["detail"] == "ledger_hash must be a 64-character hex string"
    assert blank_hash.status_code == 422
    assert blank_hash.json()["detail"] == "ledger_hash cannot be blank"
    assert invalid_status.status_code == 422


def test_paid_message_reward_unlocks_only_after_lock_period():
    sender = login("+919000000015", "Unlock Sender")
    receiver = login("+919000000016", "Unlock Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Reward unlock test"},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 200

    with SessionLocal() as db:
        reward_event = (
            db.query(RewardEvent)
            .filter(RewardEvent.user_id == receiver["user"]["id"], RewardEvent.status == "locked")
            .order_by(RewardEvent.created_at.desc())
            .first()
        )
        assert reward_event is not None
        assert as_utc(reward_event.lock_until) > utc_now()

    admin = make_admin("+919999999005")
    not_due = client.post("/admin/rewards/unlock", headers=auth(admin["access_token"])).json()
    assert not_due["unlocked_count"] == 0

    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    assert receiver_balance["locked_balance"] == "0.650000"
    assert receiver_balance["earned_balance"] == "0.000000"


def test_admin_reward_unlock_moves_locked_to_earned_and_balances_ledger():
    sender = login("+919000000017", "Due Sender")
    receiver = login("+919000000018", "Due Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Due reward unlock"},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 200

    with SessionLocal() as db:
        reward_event = (
            db.query(RewardEvent)
            .filter(RewardEvent.user_id == receiver["user"]["id"], RewardEvent.status == "locked")
            .order_by(RewardEvent.created_at.desc())
            .first()
        )
        reward_event.lock_until = utc_now() - timedelta(seconds=1)
        db.commit()

    admin = make_admin("+919999999006")
    unlocked = client.post("/admin/rewards/unlock", headers=auth(admin["access_token"])).json()
    assert unlocked["unlocked_count"] >= 1

    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    assert receiver_balance["locked_balance"] == "0.000000"
    assert receiver_balance["earned_balance"] == "0.650000"

    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"])).json()
    assert audit["imbalanced_count"] == 0


def test_admin_rewards_support_filters_and_user_context():
    sender = login("+919000000146", "Reward Filter Sender")
    receiver = login("+919000000147", "Reward Filter Receiver")
    admin = make_admin("+919999999032")
    today = utc_now().date().isoformat()
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Reward filter context"},
        headers=auth(sender["access_token"]),
    )

    with SessionLocal() as db:
        reward_event = (
            db.query(RewardEvent)
            .filter(RewardEvent.user_id == receiver["user"]["id"], RewardEvent.reference_id == sent.json()["transaction_id"])
            .one()
        )
        lock_day = as_utc(reward_event.lock_until).date().isoformat()
        reward_event_id = str(reward_event.id)

    locked = client.get(
        (
            "/admin/rewards"
            f"?status=locked&user_id={receiver['user']['id']}&source=%20MESSAGE%20&reference_id={sent.json()['transaction_id']}"
            f"&min_final_reward=0.65&max_final_reward=0.65&lock_start_date={lock_day}&lock_end_date={lock_day}"
            f"&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    with SessionLocal() as db:
        reward_event = db.get(RewardEvent, reward_event_id)
        reward_event.lock_until = utc_now() - timedelta(seconds=1)
        db.commit()
    unlocked_action = client.post("/admin/rewards/unlock", headers=auth(admin["access_token"]))
    unlocked = client.get(
        f"/admin/rewards?status=unlocked&user_id={receiver['user']['id']}",
        headers=auth(admin["access_token"]),
    )
    invalid_reward_window = client.get(
        "/admin/rewards?min_final_reward=1&max_final_reward=0.5",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/rewards?status=paid", headers=auth(admin["access_token"]))
    blank_source = client.get("/admin/rewards?source=%20%20%20", headers=auth(admin["access_token"]))
    invalid_lock_window = client.get(
        "/admin/rewards?lock_start_date=2026-04-30&lock_end_date=2026-04-29",
        headers=auth(admin["access_token"]),
    )

    assert sent.status_code == 200
    assert locked.status_code == 200
    assert len(locked.json()) == 1
    row = locked.json()[0]
    assert row["id"] == reward_event_id
    assert row["user"]["id"] == receiver["user"]["id"]
    assert row["source"] == "message"
    assert row["base_reward"] == "0.650000"
    assert row["final_reward"] == "0.650000"
    assert row["trust_multiplier"] == "1.0000"
    assert row["fraud_multiplier"] == "1.0000"
    assert unlocked_action.status_code == 200
    assert unlocked.status_code == 200
    assert any(row["id"] == reward_event_id and row["status"] == "unlocked" for row in unlocked.json())
    assert invalid_reward_window.status_code == 400
    assert invalid_status.status_code == 422
    assert blank_source.status_code == 422
    assert blank_source.json()["detail"] == "source cannot be blank"
    assert invalid_lock_window.status_code == 400


def test_admin_can_freeze_wallet_and_block_sender_spend():
    sender = login("+919000000019", "Frozen Sender")
    receiver = login("+919000000020", "Frozen Sender Receiver")
    admin = make_admin("+919999999007")

    freeze = client.post(
        f"/admin/users/{sender['user']['id']}/wallet/freeze",
        headers=auth(admin["access_token"]),
    )
    assert freeze.status_code == 200
    assert freeze.json()["status"] == "frozen"

    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Should be blocked"},
        headers=auth(sender["access_token"]),
    )
    assert sent.status_code == 423

    unfreeze = client.post(
        f"/admin/users/{sender['user']['id']}/wallet/unfreeze",
        headers=auth(admin["access_token"]),
    )
    audit_logs = client.get("/admin/audit-logs?target_type=wallet", headers=auth(admin["access_token"]))

    assert unfreeze.status_code == 200
    assert unfreeze.json()["status"] == "active"
    assert audit_logs.status_code == 200
    actions = [row["action"] for row in audit_logs.json()]
    assert "wallet.freeze" in actions
    assert "wallet.unfreeze" in actions


def test_admin_can_block_user_account_and_prevent_login():
    user = login("+919000000048", "Account Block User")
    admin = make_admin("+919999999005")

    block = client.post(f"/admin/users/{user['user']['id']}/block", headers=auth(admin["access_token"]))
    existing_token = client.get("/auth/me", headers=auth(user["access_token"]))

    client.post("/auth/send-otp", json={"phone": "+919000000048"})
    blocked_login = client.post(
        "/auth/verify-otp",
        json={"phone": "+919000000048", "otp": "123456", "name": "Account Block User"},
    )

    unblock = client.post(f"/admin/users/{user['user']['id']}/unblock", headers=auth(admin["access_token"]))
    old_token_after_unblock = client.get("/auth/me", headers=auth(user["access_token"]))
    client.post("/auth/send-otp", json={"phone": "+919000000048"})
    restored_login = client.post(
        "/auth/verify-otp",
        json={"phone": "+919000000048", "otp": "123456", "name": "Account Block User"},
    )

    assert block.status_code == 200
    assert block.json()["status"] == "blocked"
    assert existing_token.status_code == 401
    assert blocked_login.status_code == 403
    assert unblock.status_code == 200
    assert unblock.json()["status"] == "active"
    assert old_token_after_unblock.status_code == 401
    assert restored_login.status_code == 200


def test_admin_audit_logs_capture_user_mutations_and_filtering():
    user = login("+919000000124", "Audit User")
    admin = make_admin("+919999999025")
    today = utc_now().date().isoformat()

    patched = client.patch(
        f"/admin/users/{user['user']['id']}",
        json={"trust_score": 64, "kyc_status": "verified"},
        headers=auth(admin["access_token"]),
    )
    blocked = client.post(f"/admin/users/{user['user']['id']}/block", headers=auth(admin["access_token"]))
    logs = client.get(
        (
            "/admin/audit-logs"
            f"?action=%20USER.UPDATE%20&target_type=%20USER%20&admin_user_id={admin['user']['id']}"
            f"&target_id={user['user']['id']}&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    invalid_window = client.get(
        "/admin/audit-logs?start_date=2026-04-30&end_date=2026-04-29",
        headers=auth(admin["access_token"]),
    )
    blank_action = client.get("/admin/audit-logs?action=%20%20%20", headers=auth(admin["access_token"]))
    blank_target_type = client.get("/admin/audit-logs?target_type=%20%20%20", headers=auth(admin["access_token"]))

    assert patched.status_code == 200
    assert blocked.status_code == 200
    assert logs.status_code == 200
    assert invalid_window.status_code == 400
    assert blank_action.status_code == 422
    assert blank_action.json()["detail"] == "action cannot be blank"
    assert blank_target_type.status_code == 422
    assert blank_target_type.json()["detail"] == "target_type cannot be blank"
    latest = logs.json()[0]
    assert latest["action"] == "user.update"
    assert latest["admin_user_id"] == admin["user"]["id"]
    assert latest["target_type"] == "user"
    assert latest["target_id"] == user["user"]["id"]
    assert latest["metadata"]["trust_score"] == 64
    assert latest["metadata"]["kyc_status"] == "verified"


def test_admin_users_support_search_and_filters():
    low_trust_user = login("+919000000137", "Admin Search Alpha")
    verified_user = login("+919000000138", "Admin Search Beta")
    admin = make_admin("+919999999027")
    today = utc_now().date().isoformat()

    low_trust_patch = client.patch(
        f"/admin/users/{low_trust_user['user']['id']}",
        json={"trust_score": 20, "kyc_status": "pending"},
        headers=auth(admin["access_token"]),
    )
    verified_patch = client.patch(
        f"/admin/users/{verified_user['user']['id']}",
        json={"trust_score": 90, "kyc_status": "verified"},
        headers=auth(admin["access_token"]),
    )
    search = client.get("/admin/users?q=%20SEARCH%20ALPHA%20", headers=auth(admin["access_token"]))
    verified = client.get(
        f"/admin/users?kyc_status=verified&min_trust_score=80&start_date={today}&end_date={today}",
        headers=auth(admin["access_token"]),
    )
    admins = client.get("/admin/users?role=admin&status=active", headers=auth(admin["access_token"]))
    invalid_trust_window = client.get(
        "/admin/users?min_trust_score=90&max_trust_score=20",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/users?status=suspended", headers=auth(admin["access_token"]))
    blank_query = client.get("/admin/users?q=%20%20%20", headers=auth(admin["access_token"]))

    assert low_trust_patch.status_code == 200
    assert verified_patch.status_code == 200
    assert search.status_code == 200
    assert [row["id"] for row in search.json()] == [low_trust_user["user"]["id"]]
    assert verified.status_code == 200
    assert verified_user["user"]["id"] in {row["id"] for row in verified.json()}
    assert all(row["kyc_status"] == "verified" and row["trust_score"] >= 80 for row in verified.json())
    assert admins.status_code == 200
    assert admin["user"]["id"] in {row["id"] for row in admins.json()}
    assert all(row["role"] == "admin" and row["status"] == "active" for row in admins.json())
    assert invalid_trust_window.status_code == 400
    assert invalid_status.status_code == 422
    assert blank_query.status_code == 422
    assert blank_query.json()["detail"] == "q cannot be blank"


def test_admin_user_detail_includes_wallet_activity_and_risk_summary():
    sender = login("+919000000139", "Admin Detail Sender")
    receiver = login("+919000000140", "Admin Detail Receiver")
    admin = make_admin("+919999999028")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Admin detail context"},
        headers=auth(sender["access_token"]),
    ).json()
    report = client.post(
        f"/messages/{sent['id']}/report",
        json={"reason": "spam", "description": "Admin detail risk context"},
        headers=auth(receiver["access_token"]),
    )

    detail = client.get(
        f"/admin/users/{sender['user']['id']}?recent_limit=5",
        headers=auth(admin["access_token"]),
    )
    missing = client.get(
        "/admin/users/00000000-0000-0000-0000-000000000000",
        headers=auth(admin["access_token"]),
    )
    invalid_limit = client.get(
        f"/admin/users/{sender['user']['id']}?recent_limit=0",
        headers=auth(admin["access_token"]),
    )

    assert report.status_code == 200
    assert detail.status_code == 200
    body = detail.json()
    assert body["user"]["id"] == sender["user"]["id"]
    assert body["wallet"]["status"] == "active"
    assert body["wallet"]["spendable_balance"] == "19.000000"
    assert body["activity"]["active_session_count"] >= 1
    assert body["activity"]["open_report_count"] >= 1
    assert body["activity"]["open_fraud_count"] >= 1
    assert any(
        row["id"] == sent["transaction_id"] and row["transaction_type"] == "message_send" and row["direction"] == "outgoing"
        for row in body["recent_transactions"]
    )
    assert missing.status_code == 404
    assert invalid_limit.status_code == 422


def test_admin_wallet_inventory_supports_filters_and_system_wallets():
    sender = login("+919000000141", "Wallet Inventory Sender")
    receiver = login("+919000000142", "Wallet Inventory Receiver")
    admin = make_admin("+919999999029")
    today = utc_now().date().isoformat()

    transfer = client.post(
        "/wallet/transfer",
        json={"receiver_id": receiver["user"]["id"], "amount": "4.000000", "note": "inventory"},
        headers=auth(sender["access_token"]),
    )
    freeze = client.post(
        f"/admin/users/{sender['user']['id']}/wallet/freeze",
        headers=auth(admin["access_token"]),
    )
    frozen_wallets = client.get(
        (
            "/admin/wallets"
            f"?status=frozen&wallet_type=%20USER%20&user_id={sender['user']['id']}"
            f"&min_spendable_balance=15&max_spendable_balance=17&min_gas_paid_total=0.08"
            f"&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    platform_wallets = client.get(
        "/admin/wallets?wallet_type=platform&min_spendable_balance=0.01",
        headers=auth(admin["access_token"]),
    )
    invalid_balance_window = client.get(
        "/admin/wallets?min_spendable_balance=20&max_spendable_balance=10",
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.get("/admin/wallets?status=locked", headers=auth(admin["access_token"]))
    blank_wallet_type = client.get("/admin/wallets?wallet_type=%20%20%20", headers=auth(admin["access_token"]))

    assert transfer.status_code == 200
    assert freeze.status_code == 200
    assert frozen_wallets.status_code == 200
    assert len(frozen_wallets.json()) == 1
    wallet = frozen_wallets.json()[0]
    assert wallet["status"] == "frozen"
    assert wallet["wallet_type"] == "user"
    assert wallet["spendable_balance"] == "16.000000"
    assert wallet["gas_paid_total"] == "0.080000"
    assert wallet["user"]["id"] == sender["user"]["id"]
    assert platform_wallets.status_code == 200
    assert any(row["wallet_type"] == "platform" and row["user"] is None for row in platform_wallets.json())
    assert invalid_balance_window.status_code == 400
    assert invalid_status.status_code == 422
    assert blank_wallet_type.status_code == 422
    assert blank_wallet_type.json()["detail"] == "wallet_type cannot be blank"


def test_admin_can_update_user_trust_kyc_role_and_block_sessions():
    user = login("+919000000116", "Admin Patch User")
    admin = make_admin("+919999999021")

    updated = client.patch(
        f"/admin/users/{user['user']['id']}",
        json={"trust_score": 87, "kyc_status": "verified", "role": "admin"},
        headers=auth(admin["access_token"]),
    )
    me_after_role = client.get("/auth/me", headers=auth(user["access_token"]))
    blocked = client.patch(
        f"/admin/users/{user['user']['id']}",
        json={"status": "blocked"},
        headers=auth(admin["access_token"]),
    )
    old_token = client.get("/auth/me", headers=auth(user["access_token"]))

    assert updated.status_code == 200
    assert updated.json()["trust_score"] == 87
    assert updated.json()["kyc_status"] == "verified"
    assert updated.json()["role"] == "admin"
    assert me_after_role.status_code == 200
    assert me_after_role.json()["role"] == "admin"
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert old_token.status_code == 401


def test_admin_patch_prevents_self_demote_or_self_block():
    admin = make_admin("+919999999022")

    self_demote = client.patch(
        f"/admin/users/{admin['user']['id']}",
        json={"role": "user"},
        headers=auth(admin["access_token"]),
    )
    self_block = client.patch(
        f"/admin/users/{admin['user']['id']}",
        json={"status": "blocked"},
        headers=auth(admin["access_token"]),
    )
    still_admin = client.get("/auth/me", headers=auth(admin["access_token"]))
    invalid_trust = client.patch(
        f"/admin/users/{admin['user']['id']}",
        json={"trust_score": 101},
        headers=auth(admin["access_token"]),
    )

    assert self_demote.status_code == 400
    assert self_demote.json()["detail"] == "Cannot remove your own admin role"
    assert self_block.status_code == 400
    assert self_block.json()["detail"] == "Cannot block your own account"
    assert still_admin.status_code == 200
    assert still_admin.json()["role"] == "admin"
    assert invalid_trust.status_code == 422


def test_admin_cannot_block_self_or_freeze_own_wallet():
    admin = make_admin("+919999999997")

    self_block = client.post(f"/admin/users/{admin['user']['id']}/block", headers=auth(admin["access_token"]))
    self_freeze = client.post(
        f"/admin/users/{admin['user']['id']}/wallet/freeze",
        headers=auth(admin["access_token"]),
    )
    still_active = client.get("/auth/me", headers=auth(admin["access_token"]))
    wallet = client.get("/wallet/balance", headers=auth(admin["access_token"]))

    assert self_block.status_code == 400
    assert self_block.json()["detail"] == "Cannot block your own account"
    assert self_freeze.status_code == 400
    assert self_freeze.json()["detail"] == "Cannot freeze your own wallet"
    assert still_active.status_code == 200
    assert wallet.json()["status"] == "active"


def test_frozen_receiver_gets_no_reward_for_paid_message():
    sender = login("+919000000021", "Frozen Receiver Sender")
    receiver = login("+919000000022", "Frozen Receiver")
    admin = make_admin("+919999999008")

    freeze = client.post(
        f"/admin/users/{receiver['user']['id']}/wallet/freeze",
        headers=auth(admin["access_token"]),
    )
    assert freeze.status_code == 200

    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Receiver should not earn"},
        headers=auth(sender["access_token"]),
    )

    assert sent.status_code == 200
    receiver_balance = client.get("/wallet/balance", headers=auth(receiver["access_token"])).json()
    assert receiver_balance["locked_balance"] == "0.000000"
    assert receiver_balance["earned_balance"] == "0.000000"

    audit = client.get("/admin/ledger/audit", headers=auth(admin["access_token"])).json()
    assert audit["imbalanced_count"] == 0


def test_block_user_prevents_new_chat_until_unblocked():
    blocker = login("+919000000023", "Blocker")
    blocked = login("+919000000024", "Blocked")

    block = client.post(f"/users/{blocked['user']['id']}/block", headers=auth(blocker["access_token"]))
    assert block.status_code == 200
    assert block.json()["status"] == "active"

    denied = client.post(
        "/chats",
        json={"receiver_id": blocker["user"]["id"]},
        headers=auth(blocked["access_token"]),
    )
    assert denied.status_code == 403

    unblock = client.post(f"/users/{blocked['user']['id']}/unblock", headers=auth(blocker["access_token"]))
    assert unblock.status_code == 200

    allowed = client.post(
        "/chats",
        json={"receiver_id": blocker["user"]["id"]},
        headers=auth(blocked["access_token"]),
    )
    assert allowed.status_code == 200


def test_block_user_prevents_existing_chat_messages():
    sender = login("+919000000025", "Existing Block Sender")
    receiver = login("+919000000026", "Existing Block Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()

    block = client.post(f"/users/{sender['user']['id']}/block", headers=auth(receiver["access_token"]))
    assert block.status_code == 200

    denied = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Blocked message"},
        headers=auth(sender["access_token"]),
    )
    assert denied.status_code == 403


def test_blocked_conversation_is_hidden_from_chat_list_until_unblocked():
    blocker = login("+919000000095", "Inbox Blocker")
    blocked = login("+919000000096", "Inbox Blocked")
    chat = client.post(
        "/chats",
        json={"receiver_id": blocked["user"]["id"]},
        headers=auth(blocker["access_token"]),
    ).json()

    before_block = client.get("/chats", headers=auth(blocker["access_token"]))
    block = client.post(f"/users/{blocked['user']['id']}/block", headers=auth(blocker["access_token"]))
    blocker_after_block = client.get("/chats", headers=auth(blocker["access_token"]))
    blocked_after_block = client.get("/chats", headers=auth(blocked["access_token"]))
    unblock = client.post(f"/users/{blocked['user']['id']}/unblock", headers=auth(blocker["access_token"]))
    after_unblock = client.get("/chats", headers=auth(blocker["access_token"]))

    assert before_block.status_code == 200
    assert any(row["id"] == chat["id"] for row in before_block.json())
    assert block.status_code == 200
    assert blocker_after_block.status_code == 200
    assert blocked_after_block.status_code == 200
    assert all(row["id"] != chat["id"] for row in blocker_after_block.json())
    assert all(row["id"] != chat["id"] for row in blocked_after_block.json())
    assert unblock.status_code == 200
    assert any(row["id"] == chat["id"] for row in after_unblock.json())


def test_chat_detail_exposes_participants_and_block_state():
    blocker = login("+919000000099", "Detail Blocker")
    blocked = login("+919000000100", "Detail Blocked")
    outsider = login("+919000000111", "Detail Outsider")
    chat = client.post(
        "/chats",
        json={"receiver_id": blocked["user"]["id"]},
        headers=auth(blocker["access_token"]),
    ).json()

    before_block = client.get(f"/chats/{chat['id']}", headers=auth(blocker["access_token"]))
    denied = client.get(f"/chats/{chat['id']}", headers=auth(outsider["access_token"]))
    block = client.post(f"/users/{blocked['user']['id']}/block", headers=auth(blocker["access_token"]))
    blocker_detail = client.get(f"/chats/{chat['id']}", headers=auth(blocker["access_token"]))
    blocked_detail = client.get(f"/chats/{chat['id']}", headers=auth(blocked["access_token"]))

    assert before_block.status_code == 200
    assert before_block.json()["can_send"] is True
    assert len(before_block.json()["participants"]) == 2
    assert {row["id"] for row in before_block.json()["participants"]} == {
        blocker["user"]["id"],
        blocked["user"]["id"],
    }
    assert denied.status_code == 403
    assert block.status_code == 200
    assert blocker_detail.json()["blocked_by_me"] is True
    assert blocker_detail.json()["blocked_me"] is False
    assert blocker_detail.json()["can_send"] is False
    assert blocked_detail.json()["blocked_by_me"] is False
    assert blocked_detail.json()["blocked_me"] is True
    assert blocked_detail.json()["can_send"] is False


def test_report_message_creates_fraud_signal_and_admin_metric():
    sender = login("+919000000027", "Reported Sender")
    receiver = login("+919000000028", "Reporter")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Please report this"},
        headers=auth(sender["access_token"]),
    ).json()

    report = client.post(
        f"/messages/{sent['id']}/report",
        json={"reason": "spam", "description": "Repeated promotional content"},
        headers=auth(receiver["access_token"]),
    )
    assert report.status_code == 200
    assert report.json()["status"] == "open"

    duplicate = client.post(
        f"/messages/{sent['id']}/report",
        json={"reason": "spam"},
        headers=auth(receiver["access_token"]),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["report_id"] == report.json()["report_id"]

    admin = make_admin("+919999999009")
    today = utc_now().date().isoformat()
    metrics = client.get("/admin/metrics", headers=auth(admin["access_token"])).json()
    assert metrics["chat"]["spam_reports"] >= 1
    assert metrics["fraud"]["open_events"] >= 1
    reports = client.get("/admin/reports", headers=auth(admin["access_token"]))
    assert reports.status_code == 200
    assert any(item["id"] == report.json()["report_id"] for item in reports.json())
    filtered_reports = client.get(
        (
            "/admin/reports"
            f"?status=open&reason=%20SPAM%20&reporter_id={receiver['user']['id']}"
            f"&reported_user_id={sender['user']['id']}&message_id={sent['id']}"
            f"&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    fraud_events = client.get("/admin/fraud", headers=auth(admin["access_token"]))
    open_fraud_event = next(item for item in fraud_events.json() if item["status"] == "open")
    filtered_fraud = client.get(
        (
            "/admin/fraud"
            f"?status=open&severity=MEDIUM&event_type=%20message_report%20&user_id={sender['user']['id']}"
            f"&start_date={today}&end_date={today}"
        ),
        headers=auth(admin["access_token"]),
    )
    invalid_report_status = client.get("/admin/reports?status=bad", headers=auth(admin["access_token"]))
    blank_report_reason = client.get("/admin/reports?reason=%20%20%20", headers=auth(admin["access_token"]))
    invalid_fraud_severity = client.get("/admin/fraud?severity=CRITICAL", headers=auth(admin["access_token"]))
    blank_fraud_event_type = client.get("/admin/fraud?event_type=%20%20%20", headers=auth(admin["access_token"]))

    resolved_report = client.patch(
        f"/admin/reports/{report.json()['report_id']}",
        json={"status": "resolved"},
        headers=auth(admin["access_token"]),
    )
    resolved_fraud = client.patch(
        f"/admin/fraud/{open_fraud_event['id']}",
        json={"status": "dismissed"},
        headers=auth(admin["access_token"]),
    )
    invalid_status = client.patch(
        f"/admin/reports/{report.json()['report_id']}",
        json={"status": "bad"},
        headers=auth(admin["access_token"]),
    )
    metrics_after = client.get("/admin/metrics", headers=auth(admin["access_token"])).json()

    assert resolved_report.status_code == 200
    assert resolved_report.json()["status"] == "resolved"
    assert filtered_reports.status_code == 200
    assert [item["id"] for item in filtered_reports.json()] == [report.json()["report_id"]]
    assert resolved_fraud.status_code == 200
    assert resolved_fraud.json()["status"] == "dismissed"
    assert filtered_fraud.status_code == 200
    assert any(item["id"] == open_fraud_event["id"] for item in filtered_fraud.json())
    assert invalid_report_status.status_code == 422
    assert blank_report_reason.status_code == 422
    assert blank_report_reason.json()["detail"] == "reason cannot be blank"
    assert invalid_fraud_severity.status_code == 422
    assert blank_fraud_event_type.status_code == 422
    assert blank_fraud_event_type.json()["detail"] == "event_type cannot be blank"
    assert invalid_status.status_code == 422
    assert metrics_after["chat"]["spam_reports"] == metrics["chat"]["spam_reports"] - 1
    assert metrics_after["fraud"]["open_events"] == metrics["fraud"]["open_events"] - 1


def test_report_message_normalizes_moderation_text():
    sender = login("+919000009903", "Trim Report Sender")
    receiver = login("+919000009904", "Trim Report Receiver")
    admin = make_admin("+919999999901")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Normalize this report"},
        headers=auth(sender["access_token"]),
    ).json()

    report = client.post(
        f"/messages/{sent['id']}/report",
        json={"reason": "  SPAM  ", "description": "  Repeated promo text  "},
        headers=auth(receiver["access_token"]),
    )
    reports = client.get(
        f"/admin/reports?reason=spam&message_id={sent['id']}",
        headers=auth(admin["access_token"]),
    )

    assert report.status_code == 200
    assert reports.status_code == 200
    assert reports.json()[0]["reason"] == "spam"
    assert reports.json()[0]["description"] == "Repeated promo text"


def test_report_message_rejects_blank_reason_before_fraud_event():
    sender = login("+919000009905", "Blank Report Sender")
    receiver = login("+919000009906", "Blank Report Receiver")
    chat = client.post(
        "/chats",
        json={"receiver_id": receiver["user"]["id"]},
        headers=auth(sender["access_token"]),
    ).json()
    sent = client.post(
        f"/chats/{chat['id']}/messages",
        json={"receiver_id": receiver["user"]["id"], "content": "Do not report blank"},
        headers=auth(sender["access_token"]),
    ).json()
    with SessionLocal() as db:
        before_fraud_events = db.query(FraudEvent).count()

    response = client.post(
        f"/messages/{sent['id']}/report",
        json={"reason": "   ", "description": "   "},
        headers=auth(receiver["access_token"]),
    )

    with SessionLocal() as db:
        after_fraud_events = db.query(FraudEvent).count()

    assert response.status_code == 422
    assert after_fraud_events == before_fraud_events


def test_otp_resend_cooldown_is_enforced():
    phone = "+919000000007"
    first = client.post("/auth/send-otp", json={"phone": phone})
    second = client.post("/auth/send-otp", json={"phone": phone})

    assert first.status_code == 200
    assert second.status_code == 429
    assert "wait" in second.json()["detail"].lower()


def test_otp_attempt_limit_blocks_bruteforce():
    phone = "+919000000008"
    client.post("/auth/send-otp", json={"phone": phone})

    last = None
    for _ in range(5):
        last = client.post("/auth/verify-otp", json={"phone": phone, "otp": "000000"})

    assert last.status_code == 400
    blocked = client.post("/auth/verify-otp", json={"phone": phone, "otp": "123456"})
    assert blocked.status_code == 429


def test_admin_routes_require_admin_role():
    normal = login("+919000000009", "Normal User")
    denied = client.get("/admin/metrics", headers=auth(normal["access_token"]))
    assert denied.status_code == 403

    admin = make_admin("+919999999002")
    allowed = client.get("/admin/metrics", headers=auth(admin["access_token"]))
    assert allowed.status_code == 200
    assert "users" in allowed.json()


def test_production_config_rejects_demo_secrets():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            auto_create_tables=False,
            jwt_secret="change_me",
            otp_provider="msg91",
            msg91_auth_key="key",
            msg91_template_id="template",
            razorpay_key_id="rzp_live_key",
            razorpay_key_secret="live_secret",
            razorpay_webhook_secret="secret",
            enable_dev_payment_capture=False,
            enable_public_user_directory=False,
            enable_dev_user_seed=False,
            hsts_enabled=True,
        )


def test_production_config_rejects_wildcard_cors():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            auto_create_tables=False,
            jwt_secret="a" * 32,
            cors_origins="*",
            allowed_hosts="api.example.com",
            otp_provider="msg91",
            msg91_auth_key="key",
            msg91_template_id="template",
            razorpay_key_id="rzp_live_key",
            razorpay_key_secret="live_secret",
            razorpay_webhook_secret="secret",
            enable_dev_payment_capture=False,
            enable_public_user_directory=False,
            enable_dev_user_seed=False,
            hsts_enabled=True,
        )


def test_production_config_rejects_public_directory():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            auto_create_tables=False,
            jwt_secret="a" * 32,
            cors_origins="https://app.example.com",
            allowed_hosts="api.example.com",
            otp_provider="msg91",
            msg91_auth_key="key",
            msg91_template_id="template",
            razorpay_key_id="rzp_live_key",
            razorpay_key_secret="live_secret",
            razorpay_webhook_secret="secret",
            enable_dev_payment_capture=False,
            enable_public_user_directory=True,
            enable_dev_user_seed=False,
            hsts_enabled=True,
        )


def test_production_config_rejects_disabled_hsts():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            auto_create_tables=False,
            jwt_secret="a" * 32,
            cors_origins="https://app.example.com",
            allowed_hosts="api.example.com",
            otp_provider="msg91",
            msg91_auth_key="key",
            msg91_template_id="template",
            razorpay_key_id="rzp_live_key",
            razorpay_key_secret="live_secret",
            razorpay_webhook_secret="secret",
            enable_dev_payment_capture=False,
            enable_public_user_directory=False,
            enable_dev_user_seed=False,
            hsts_enabled=False,
        )


def test_production_config_rejects_placeholder_razorpay_credentials():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            auto_create_tables=False,
            jwt_secret="a" * 32,
            cors_origins="https://app.example.com",
            allowed_hosts="api.example.com",
            otp_provider="msg91",
            msg91_auth_key="key",
            msg91_template_id="template",
            razorpay_key_id="rzp_test_xxx",
            razorpay_key_secret="xxx",
            razorpay_webhook_secret="secret",
            enable_dev_payment_capture=False,
            enable_public_user_directory=False,
            enable_dev_user_seed=False,
            hsts_enabled=True,
        )


def test_production_config_accepts_locked_down_settings():
    settings = Settings(
        env="production",
        auto_create_tables=False,
        jwt_secret="a" * 32,
        cors_origins="https://app.example.com",
        allowed_hosts="api.example.com",
        otp_provider="msg91",
        msg91_auth_key="key",
        msg91_template_id="template",
        razorpay_key_id="rzp_live_key",
        razorpay_key_secret="live_secret",
        razorpay_webhook_secret="secret",
        enable_dev_payment_capture=False,
        enable_public_user_directory=False,
        enable_dev_user_seed=False,
        hsts_enabled=True,
    )

    assert settings.cors_origin_list == ["https://app.example.com"]
    assert settings.allowed_host_list == ["api.example.com"]

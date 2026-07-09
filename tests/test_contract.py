"""API contract tests — exact field names, status codes, error codes."""
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine, Base
from app.database import SessionLocal
from app.services.reference import init_counter_from_db
from app.services import reference
from app.services.ratelimit import reset as reset_ratelimit

client = TestClient(app)


def _future(h: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=h)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


@pytest.fixture(autouse=True)
def reset_state():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_ratelimit()
    reference._counter = 1000


class TestContract:

    def test_health(self):
        """GET /health → 200 {"status": "ok"}"""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_register_new_org(self):
        """POST /auth/register → 201 {user_id, org_id, username, role}"""
        r = client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        assert r.status_code == 201
        body = r.json()
        assert set(body.keys()) == {"user_id", "org_id", "username", "role"}
        assert body["username"] == "alice"
        assert body["role"] == "admin"

    def test_register_existing_org(self):
        """Joining existing org → role=member"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        r = client.post("/auth/register", json={"org_name": "Acme", "username": "bob", "password": "pw"})
        assert r.status_code == 201
        assert r.json()["role"] == "member"

    def test_register_duplicate_username(self):
        """Duplicate username in org → 409 USERNAME_TAKEN"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        r = client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        assert r.status_code == 409
        assert r.json()["code"] == "USERNAME_TAKEN"

    def test_login_success(self):
        """POST /auth/login → 200 {access_token, refresh_token, token_type: "bearer"}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        r = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"access_token", "refresh_token", "token_type"}
        assert body["token_type"] == "bearer"
        assert isinstance(body["access_token"], str)
        assert isinstance(body["refresh_token"], str)

    def test_login_invalid_credentials(self):
        """Bad credentials → 401 INVALID_CREDENTIALS"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        r = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "wrong"})
        assert r.status_code == 401
        assert r.json()["code"] == "INVALID_CREDENTIALS"

    def test_refresh(self):
        """POST /auth/refresh body {refresh_token} → same shape as login"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        login = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()
        r = client.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"access_token", "refresh_token", "token_type"}
        assert body["token_type"] == "bearer"
        # Refresh token must be single-use
        r2 = client.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})
        assert r2.status_code == 401

    def test_logout(self):
        """POST /auth/logout → 200, invalidates presented access token"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post("/auth/logout", headers=headers)
        assert r.status_code == 200
        # Same token now fails
        r2 = client.get("/bookings", headers=headers)
        assert r2.status_code == 401

    def test_list_rooms(self):
        """GET /rooms → 200 [{id, org_id, name, capacity, hourly_rate_cents}]"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        # Create a room
        client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h)
        r = client.get("/rooms", headers=h)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        room = r.json()[0]
        assert set(room.keys()) == {"id", "org_id", "name", "capacity", "hourly_rate_cents"}

    def test_create_room_admin_only(self):
        """POST /rooms → 201 for admin, 403 for member"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token_a = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        # Member tries
        client.post("/auth/register", json={"org_name": "Acme", "username": "bob", "password": "pw"})
        token_b = client.post("/auth/login", json={"org_name": "Acme", "username": "bob", "password": "pw"}).json()["access_token"]
        r = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000},
                        headers={"Authorization": f"Bearer {token_b}"})
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
        # Admin succeeds
        r = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000},
                        headers={"Authorization": f"Bearer {token_a}"})
        assert r.status_code == 201
        assert set(r.json().keys()) == {"id", "org_id", "name", "capacity", "hourly_rate_cents"}

    def test_availability(self):
        """GET /rooms/{id}/availability?date= → {room_id, date, busy: [{start_time, end_time}]}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        start = _future(50)
        end = _future(52)
        date = start[:10]
        client.post("/bookings", json={"room_id": room["id"], "start_time": start, "end_time": end}, headers=h)
        r = client.get(f"/rooms/{room['id']}/availability?date={date}", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"room_id", "date", "busy"}
        assert isinstance(body["busy"], list)
        if body["busy"]:
            assert set(body["busy"][0].keys()) == {"start_time", "end_time"}

    def test_stats(self):
        """GET /rooms/{id}/stats → {room_id, total_confirmed_bookings, total_revenue_cents}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        r = client.get(f"/rooms/{room['id']}/stats", headers=h)
        assert r.status_code == 200
        assert set(r.json().keys()) == {"room_id", "total_confirmed_bookings", "total_revenue_cents"}

    def test_create_booking(self):
        """POST /bookings body {room_id, start_time, end_time} → Booking response"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        r = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(2), "end_time": _future(4)}, headers=h)
        assert r.status_code == 201
        body = r.json()
        expected = {"id", "reference_code", "room_id", "user_id", "start_time", "end_time", "status", "price_cents", "created_at"}
        assert set(body.keys()) == expected, f"Got {set(body.keys())}, expected {expected}"

    def test_list_bookings(self):
        """GET /bookings → {items: [Booking], page, limit, total}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        client.post("/bookings", json={"room_id": room["id"], "start_time": _future(30), "end_time": _future(32)}, headers=h)
        r = client.get("/bookings", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"items", "page", "limit", "total"}
        assert isinstance(body["items"], list)
        if body["items"]:
            assert set(body["items"][0].keys()) == {"id", "reference_code", "room_id", "user_id", "start_time", "end_time", "status", "price_cents", "created_at"}

    def test_get_booking_detail(self):
        """GET /bookings/{id} → Booking + refunds: [{amount_cents, status, processed_at}]"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        b = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(50), "end_time": _future(52)}, headers=h).json()
        r = client.get(f"/bookings/{b['id']}", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"id", "reference_code", "room_id", "user_id", "start_time", "end_time", "status", "price_cents", "created_at", "refunds"}
        assert isinstance(body["refunds"], list)

    def test_cancel_booking(self):
        """POST /bookings/{id}/cancel → {id, status: "cancelled", refund_percent, refund_amount_cents}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        b = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(50), "end_time": _future(52)}, headers=h).json()
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"id", "status", "refund_percent", "refund_amount_cents"}
        assert body["status"] == "cancelled"

    def test_admin_usage_report(self):
        """GET /admin/usage-report?from=&to= → {from, to, rooms: [{room_id, room_name, confirmed_bookings, revenue_cents}]}"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h)
        r = client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"from", "to", "rooms"}
        if body["rooms"]:
            assert set(body["rooms"][0].keys()) == {"room_id", "room_name", "confirmed_bookings", "revenue_cents"}

    def test_member_forbidden_from_admin(self):
        """Member gets 403 FORBIDDEN on /admin endpoints"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        client.post("/auth/register", json={"org_name": "Acme", "username": "bob", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "bob", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        r = client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h)
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
        r2 = client.get("/admin/export", headers=h)
        assert r2.status_code == 403

    def test_export_csv_header(self):
        """CSV header: id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h)
        r = client.get("/admin/export", headers=h)
        assert r.status_code == 200
        header = r.text.splitlines()[0]
        expected = "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"
        assert header == expected, f"CSV header mismatch: got '{header}', expected '{expected}'"

    def test_error_code_room_conflict(self):
        """Overlap → 409 ROOM_CONFLICT"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        client.post("/bookings", json={"room_id": room["id"], "start_time": _future(2), "end_time": _future(4)}, headers=h)
        r = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(3), "end_time": _future(5)}, headers=h)
        assert r.status_code == 409
        assert r.json()["code"] == "ROOM_CONFLICT"

    def test_error_code_quota_exceeded(self):
        """Violation → 409 QUOTA_EXCEEDED"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        for i in range(3):
            client.post("/bookings", json={"room_id": room["id"], "start_time": _future(1 + i * 2), "end_time": _future(3 + i * 2)}, headers=h)
        r = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(10), "end_time": _future(12)}, headers=h)
        assert r.status_code == 409
        assert r.json()["code"] == "QUOTA_EXCEEDED"

    def test_error_code_already_cancelled(self):
        """Already-cancelled → 409 ALREADY_CANCELLED"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        b = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(50), "end_time": _future(52)}, headers=h).json()
        client.post(f"/bookings/{b['id']}/cancel", headers=h)
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 409
        assert r.json()["code"] == "ALREADY_CANCELLED"

    def test_error_code_booking_not_found(self):
        """Non-existent booking → 404 BOOKING_NOT_FOUND"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        r = client.get("/bookings/99999", headers=h)
        assert r.status_code == 404
        assert r.json()["code"] == "BOOKING_NOT_FOUND"

    def test_error_code_room_not_found(self):
        """Non-existent room → 404 ROOM_NOT_FOUND"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        r = client.get("/rooms/99999/availability?date=2026-07-10", headers=h)
        assert r.status_code == 404
        assert r.json()["code"] == "ROOM_NOT_FOUND"

    def test_error_code_forbidden(self):
        """Member on admin endpoint → 403 FORBIDDEN"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        client.post("/auth/register", json={"org_name": "Acme", "username": "bob", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "bob", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        r = client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h)
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

    def test_error_code_invalid_booking_window(self):
        """Past start → 400 INVALID_BOOKING_WINDOW"""
        client.post("/auth/register", json={"org_name": "Acme", "username": "alice", "password": "pw"})
        token = client.post("/auth/login", json={"org_name": "Acme", "username": "alice", "password": "pw"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1000}, headers=h).json()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = client.post("/bookings", json={"room_id": room["id"], "start_time": past, "end_time": future}, headers=h)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BOOKING_WINDOW"

    def test_missing_token_401(self):
        """Missing token → 401 UNAUTHORIZED"""
        r = client.get("/bookings")
        assert r.status_code == 401

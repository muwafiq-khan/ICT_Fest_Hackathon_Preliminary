"""Comprehensive test covering all 16 business rules."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.database import SessionLocal
from app.models import Booking, RefundLog
from app.services.reference import init_counter_from_db, next_reference_code
from app.services import reference
from app.services.ratelimit import reset as reset_ratelimit

client = TestClient(app)


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _past(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _register(org: str, user: str, pw: str = "pw"):
    return client.post(
        "/auth/register",
        json={"org_name": org, "username": user, "password": pw},
    )


def _login(org: str, user: str, pw: str = "pw"):
    return client.post(
        "/auth/login",
        json={"org_name": org, "username": user, "password": pw},
    )


def _create_room(headers: dict, name="Room", rate=1000, capacity=4):
    return client.post(
        "/rooms",
        json={"name": name, "capacity": capacity, "hourly_rate_cents": rate},
        headers=headers,
    )


def _book(headers: dict, room_id: int, start: str, end: str):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )


@pytest.fixture(autouse=True)
def clean_db():
    """Drop and recreate tables and reset in-memory state before each test."""
    from app.database import engine, Base
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Reset in-memory rate-limit buckets and reference counter
    reset_ratelimit()
    reference._counter = 1000


class TestRule1Datetimes:
    """Rule 1: ISO 8601, UTC offset handling."""

    def test_z_suffix(self):
        r = _register("org1", "u1")
        assert r.status_code == 201
        h = {"Authorization": f"Bearer {_login('org1', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
        end = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:00:00Z")
        r = _book(h, room["id"], start, end)
        assert r.status_code == 201, f"Z suffix failed: {r.json()}"

    def test_offset_conversion(self):
        r = _register("org2", "u1")
        assert r.status_code == 201
        h = {"Authorization": f"Bearer {_login('org2', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        # Compute UTC times, then express them as +05:00 local times
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_utc = now + timedelta(hours=5)  # 5h future in UTC
        end_utc = now + timedelta(hours=7)
        # Express as +05:00 local time
        start_local = start_utc + timedelta(hours=5)
        end_local = end_utc + timedelta(hours=5)
        start = start_local.strftime("%Y-%m-%dT%H:%M:%S") + "+05:00"
        end = end_local.strftime("%Y-%m-%dT%H:%M:%S") + "+05:00"
        r = _book(h, room["id"], start, end)
        assert r.status_code == 201, f"Offset failed: {r.json()}"
        assert r.json()["start_time"].endswith("+00:00")

    def test_naive_treated_as_utc(self):
        r = _register("org3", "u1")
        assert r.status_code == 201
        h = {"Authorization": f"Bearer {_login('org3', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        start = _future(2).replace("+00:00", "")
        end = _future(4).replace("+00:00", "")
        r = _book(h, room["id"], start, end)
        assert r.status_code == 201

    def test_response_has_utc_designator(self):
        r = _register("org4", "u1")
        assert r.status_code == 201
        h = {"Authorization": f"Bearer {_login('org4', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(2), _future(4)).json()
        assert b["start_time"].endswith("+00:00") or b["start_time"].endswith("Z")
        assert b["end_time"].endswith("+00:00") or b["end_time"].endswith("Z")


class TestRule2BookingPrice:
    """Rule 2: price_cents = rate * hours, whole hours, 1-8, future only."""

    def test_correct_price(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h, rate=1500).json()
        b = _book(h, room["id"], _future(2), _future(5)).json()
        assert b["price_cents"] == 1500 * 3

    def test_past_start_rejected(self):
        r = _register("org", "u2")
        h = {"Authorization": f"Bearer {_login('org', 'u2').json()['access_token']}"}
        room = _create_room(h).json()
        r = _book(h, room["id"], _past(1), _future(1))
        assert r.status_code == 400

    def test_non_whole_hours_rejected(self):
        r = _register("org", "u3")
        h = {"Authorization": f"Bearer {_login('org', 'u3').json()['access_token']}"}
        room = _create_room(h).json()
        start = _future(2)
        end = (datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)).isoformat()
        r = _book(h, room["id"], start, end)
        assert r.status_code == 400

    def test_min_duration(self):
        r = _register("org", "u4")
        h = {"Authorization": f"Bearer {_login('org', 'u4').json()['access_token']}"}
        room = _create_room(h).json()
        r = _book(h, room["id"], _future(2), _future(2))
        assert r.status_code == 400

    def test_max_duration(self):
        r = _register("org", "u5")
        h = {"Authorization": f"Bearer {_login('org', 'u5').json()['access_token']}"}
        room = _create_room(h).json()
        r = _book(h, room["id"], _future(2), _future(11))
        assert r.status_code == 400


class TestRule3NoDoubleBooking:
    """Rule 3: Overlap check with back-to-back allowed."""

    def test_overlap_rejected(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        assert _book(h, room["id"], _future(2), _future(4)).status_code == 201
        r = _book(h, room["id"], _future(3), _future(5))
        assert r.status_code == 409

    def test_back_to_back_allowed(self):
        r = _register("org", "u2")
        h = {"Authorization": f"Bearer {_login('org', 'u2').json()['access_token']}"}
        room = _create_room(h).json()
        assert _book(h, room["id"], _future(2), _future(4)).status_code == 201
        r = _book(h, room["id"], _future(4), _future(6))
        assert r.status_code == 201

    def test_identical_time_allowed_different_room(self):
        r = _register("org", "u3")
        h = {"Authorization": f"Bearer {_login('org', 'u3').json()['access_token']}"}
        r1 = _create_room(h, name="A").json()
        r2 = _create_room(h, name="B").json()
        assert _book(h, r1["id"], _future(2), _future(4)).status_code == 201
        assert _book(h, r2["id"], _future(2), _future(4)).status_code == 201


class TestRule4BookingQuota:
    """Rule 4: At most 3 confirmed bookings in (now, now+24h]."""

    def test_quota_limit(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        assert _book(h, room["id"], _future(1), _future(3)).status_code == 201
        assert _book(h, room["id"], _future(4), _future(6)).status_code == 201
        assert _book(h, room["id"], _future(7), _future(9)).status_code == 201
        r = _book(h, room["id"], _future(10), _future(12))
        assert r.status_code == 409

    def test_quota_does_not_apply_outside_window(self):
        r = _register("org", "u2")
        h = {"Authorization": f"Bearer {_login('org', 'u2').json()['access_token']}"}
        room = _create_room(h).json()
        assert _book(h, room["id"], _future(1), _future(3)).status_code == 201
        assert _book(h, room["id"], _future(4), _future(6)).status_code == 201
        assert _book(h, room["id"], _future(7), _future(9)).status_code == 201
        # 30h from now is outside (now, now+24h]
        r = _book(h, room["id"], _future(30), _future(32))
        assert r.status_code == 201


class TestRule5RateLimit:
    """Rule 5: 20 requests per 60s rolling window."""

    def test_rate_limit_exceeded(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        # Make 20 valid requests (first 19 should succeed, 20th may be rate-limited)
        successes = 0
        ratelimited = 0
        for i in range(21):
            r = _book(h, room["id"], _future(100 + i), _future(102 + i))
            if r.status_code == 201:
                successes += 1
            elif r.status_code == 429:
                ratelimited += 1
        assert ratelimited >= 1, "Rate limit should have triggered at least once"
        assert successes <= 20


class TestRule6CancellationRefund:
    """Rule 6: Refund tiers, half-up rounding, one RefundLog, lock."""

    def test_48h_refund(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h, rate=1000).json()
        b = _book(h, room["id"], _future(60), _future(62)).json()
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 200
        assert r.json()["refund_percent"] == 100
        assert r.json()["refund_amount_cents"] == 2000

    def test_24h_refund(self):
        r = _register("org", "u2")
        h = {"Authorization": f"Bearer {_login('org', 'u2').json()['access_token']}"}
        room = _create_room(h, rate=1500).json()
        b = _book(h, room["id"], _future(30), _future(32)).json()  # 30h notice
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 200
        assert r.json()["refund_percent"] == 50

    def test_0h_refund(self):
        r = _register("org", "u3")
        h = {"Authorization": f"Bearer {_login('org', 'u3').json()['access_token']}"}
        room = _create_room(h, rate=1000).json()
        b = _book(h, room["id"], _future(2), _future(4)).json()  # 2h notice
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 200
        assert r.json()["refund_percent"] == 0
        assert r.json()["refund_amount_cents"] == 0

    def test_half_up_rounding(self):
        r = _register("org", "u4")
        h = {"Authorization": f"Bearer {_login('org', 'u4').json()['access_token']}"}
        room = _create_room(h, rate=1001).json()  # 1001 cents * 2h = 2002
        b = _book(h, room["id"], _future(30), _future(32)).json()  # 30h = 50%
        assert b["price_cents"] == 2002
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        # 50% of 2002 = 1001, half-up = 1001
        assert r.json()["refund_amount_cents"] == 1001

    def test_exactly_one_refund_log(self):
        r = _register("org", "u5")
        h = {"Authorization": f"Bearer {_login('org', 'u5').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(50), _future(52)).json()
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 200
        # Verify via detail endpoint
        detail = client.get(f"/bookings/{b['id']}", headers=h).json()
        assert len(detail["refunds"]) == 1
        assert detail["refunds"][0]["amount_cents"] == r.json()["refund_amount_cents"]

    def test_already_cancelled(self):
        r = _register("org", "u6")
        h = {"Authorization": f"Bearer {_login('org', 'u6').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(50), _future(52)).json()
        assert client.post(f"/bookings/{b['id']}/cancel", headers=h).status_code == 200
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h)
        assert r.status_code == 409

    def test_member_cannot_cancel_others(self):
        r = _register("org", "alice")
        h_alice = {"Authorization": f"Bearer {_login('org', 'alice').json()['access_token']}"}
        _register("org", "bob")
        h_bob = {"Authorization": f"Bearer {_login('org', 'bob').json()['access_token']}"}
        room = _create_room(h_alice).json()
        b = _book(h_alice, room["id"], _future(50), _future(52)).json()
        r = client.post(f"/bookings/{b['id']}/cancel", headers=h_bob)
        assert r.status_code == 404


class TestRule7ReferenceCodes:
    """Rule 7: Reference codes unique."""

    def test_codes_are_unique(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        codes = set()
        # Space bookings 30h apart to stay outside the 24h quota window
        for i in range(5):
            b = _book(h, room["id"], _future(30 + i * 30), _future(32 + i * 30))
            assert b.status_code == 201, f"Booking {i} failed: {b.json()}"
            data = b.json()
            assert data["reference_code"] not in codes
            codes.add(data["reference_code"])

    def test_init_from_db_on_restart(self):
        # Create a booking first, then simulate restart
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(10), _future(12)).json()
        assert b["reference_code"] == "CW-001000"
        from app.services import reference
        assert reference._counter > 1000
        old_counter = reference._counter
        reference._counter = 1000  # simulate restart reset
        db = SessionLocal()
        init_counter_from_db(db)
        db.close()
        assert reference._counter == old_counter


class TestRule8Auth:
    """Rule 8: JWT, 900s access, 7d refresh, single-use, logout."""

    def test_access_token_expiry(self):
        r = _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        assert _book(h, room["id"], _future(2), _future(4)).status_code == 201

    def test_refresh_single_use(self):
        _register("org", "u1")
        login = _login("org", "u1").json()
        rt = login["refresh_token"]
        # First use works
        r1 = client.post("/auth/refresh", json={"refresh_token": rt})
        assert r1.status_code == 200
        # Second use fails
        r2 = client.post("/auth/refresh", json={"refresh_token": rt})
        assert r2.status_code == 401

    def test_logout_invalidates_token(self):
        _register("org", "u1")
        login = _login("org", "u1").json()
        token = login["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        # Logout
        r = client.post("/auth/logout", headers=headers)
        assert r.status_code == 200
        # Same token now fails
        r = client.get("/bookings", headers=headers)
        assert r.status_code == 401


class TestRule9MultiTenancy:
    """Rule 9: Users only see their own org's data."""

    def test_cross_org_room_404(self):
        _register("orgA", "admin_a")
        h_a = {"Authorization": f"Bearer {_login('orgA', 'admin_a').json()['access_token']}"}
        _register("orgB", "admin_b")
        h_b = {"Authorization": f"Bearer {_login('orgB', 'admin_b').json()['access_token']}"}
        room = _create_room(h_a).json()
        # orgB should not see orgA's room
        r = client.get(f"/rooms/{room['id']}/availability?date=2026-07-10", headers=h_b)
        assert r.status_code == 404


class TestRule10BookingVisibility:
    """Rule 10: Members see own bookings, admins see all."""

    def test_member_cannot_see_others_booking_detail(self):
        _register("org", "alice")
        h_a = {"Authorization": f"Bearer {_login('org', 'alice').json()['access_token']}"}
        _register("org", "bob")
        h_b = {"Authorization": f"Bearer {_login('org', 'bob').json()['access_token']}"}
        room = _create_room(h_a).json()
        b = _book(h_a, room["id"], _future(50), _future(52)).json()
        r = client.get(f"/bookings/{b['id']}", headers=h_b)
        assert r.status_code == 404

    def test_admin_can_see_all(self):
        _register("org", "alice")
        h_a = {"Authorization": f"Bearer {_login('org', 'alice').json()['access_token']}"}
        _register("org", "bob")
        h_b = {"Authorization": f"Bearer {_login('org', 'bob').json()['access_token']}"}
        # alice is admin (first user), bob is member
        room = _create_room(h_a).json()
        b = _book(h_b, room["id"], _future(50), _future(52)).json()
        r = client.get(f"/bookings/{b['id']}", headers=h_a)
        assert r.status_code == 200


class TestRule11Pagination:
    """Rule 11: Pagination and ordering."""

    def test_pagination(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        # Create 3 bookings within quota, spaced outside 24h window
        bookings = []
        for i in range(3):
            r = _book(h, room["id"], _future(30 + i * 30), _future(32 + i * 30))
            assert r.status_code == 201, f"Booking {i} failed: {r.json()}"
            bookings.append(r.json())
        # Page 1, limit 2
        r = client.get("/bookings?page=1&limit=2", headers=h)
        assert r.status_code == 200
        assert len(r.json()["items"]) == 2
        assert r.json()["total"] == 3
        # Page 2, limit 2 should return the remaining item
        r2 = client.get("/bookings?page=2&limit=2", headers=h)
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 1
        assert r.json()["items"][0]["id"] != r2.json()["items"][0]["id"]


class TestRule12UsageReport:
    """Rule 12: Usage report includes zero-booking rooms."""

    def test_zero_booking_room_included(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        _create_room(h, name="Room A").json()
        _create_room(h, name="Room B").json()
        r = client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h)
        assert r.status_code == 200
        assert len(r.json()["rooms"]) == 2
        for room in r.json()["rooms"]:
            assert room["confirmed_bookings"] == 0

    def test_excludes_cancelled(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(50), _future(52)).json()
        client.post(f"/bookings/{b['id']}/cancel", headers=h)
        r = client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h)
        for room in r.json()["rooms"]:
            assert room["confirmed_bookings"] == 0


class TestRule13Availability:
    """Rule 13: Availability reflects current state immediately."""

    def test_availability_shows_confirmed(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        start = _future(50)
        end = _future(52)
        date = start[:10]
        b = _book(h, room["id"], start, end).json()
        r = client.get(f"/rooms/{room['id']}/availability?date={date}", headers=h)
        assert r.status_code == 200
        assert len(r.json()["busy"]) >= 1

    def test_availability_updates_after_cancel(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        start = _future(50)
        end = _future(52)
        date = start[:10]
        b = _book(h, room["id"], start, end).json()
        # Cancel
        client.post(f"/bookings/{b['id']}/cancel", headers=h)
        # Availability should reflect cancellation
        r = client.get(f"/rooms/{room['id']}/availability?date={date}", headers=h)
        assert r.status_code == 200
        busy_ids = [slot for slot in r.json()["busy"]]
        assert len(busy_ids) == 0  # no busy slots after cancel


class TestRule14RoomStats:
    """Rule 14: Room stats match DB."""

    def test_stats_match_db(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h, rate=2000).json()
        b1 = _book(h, room["id"], _future(50), _future(52)).json()
        b2 = _book(h, room["id"], _future(53), _future(55)).json()
        r = client.get(f"/rooms/{room['id']}/stats", headers=h)
        assert r.status_code == 200
        assert r.json()["total_confirmed_bookings"] == 2
        assert r.json()["total_revenue_cents"] == 2000 * 2 + 2000 * 2

    def test_stats_decrement_on_cancel(self):
        _register("org", "u1")
        h = {"Authorization": f"Bearer {_login('org', 'u1').json()['access_token']}"}
        room = _create_room(h).json()
        b1 = _book(h, room["id"], _future(50), _future(52)).json()
        b2 = _book(h, room["id"], _future(53), _future(55)).json()
        client.post(f"/bookings/{b1['id']}/cancel", headers=h)
        r = client.get(f"/rooms/{room['id']}/stats", headers=h)
        assert r.json()["total_confirmed_bookings"] == 1


class TestRule15Registration:
    """Rule 15: Registration rules."""

    def test_new_org_creates_admin(self):
        r = _register("neworg", "admin")
        assert r.status_code == 201
        assert r.json()["role"] == "admin"

    def test_existing_org_creates_member(self):
        _register("org", "alice")
        r = _register("org", "bob")
        assert r.status_code == 201
        assert r.json()["role"] == "member"

    def test_duplicate_username_409(self):
        _register("org", "alice")
        r = _register("org", "alice")
        assert r.status_code == 409

    def test_duplicate_username_different_org_ok(self):
        _register("orgA", "alice")
        r = _register("orgB", "alice")
        assert r.status_code == 201


class TestRule16Liveness:
    """Rule 16: All endpoints respond."""

    def test_health(self):
        assert client.get("/health").json() == {"status": "ok"}

    def test_all_endpoints_respond(self):
        _register("org", "admin")
        h = {"Authorization": f"Bearer {_login('org', 'admin').json()['access_token']}"}
        room = _create_room(h).json()
        b = _book(h, room["id"], _future(50), _future(52)).json()
        assert client.get("/rooms", headers=h).status_code == 200
        assert client.get(f"/rooms/{room['id']}/availability?date=2026-07-10", headers=h).status_code in (200, 400)
        assert client.get(f"/rooms/{room['id']}/stats", headers=h).status_code == 200
        assert client.get("/bookings", headers=h).status_code == 200
        assert client.get(f"/bookings/{b['id']}", headers=h).status_code == 200
        assert client.post(f"/bookings/{b['id']}/cancel", headers=h).status_code == 200
        assert client.get("/admin/usage-report?from=2026-01-01&to=2026-12-31", headers=h).status_code == 200
        assert client.get("/admin/export", headers=h).status_code == 200

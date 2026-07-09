# Bug Report — CoWork API

## Bug 1: Timezone offset not converted to UTC
- **File:** `app/timeutils.py:13`
- **Bug:** `parse_input_datetime` strips the timezone info via `.replace(tzinfo=None)` instead of converting to UTC. An input like `2026-07-09T10:00:00+05:00` would be stored as `10:00 UTC` instead of `05:00 UTC`.
- **Fix:** Use `dt.astimezone(timezone.utc).replace(tzinfo=None)` to convert to UTC before stripping the tz.

## Bug 2: Access token lifetime too long
- **File:** `app/auth.py:50`
- **Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` computes 15 × 60 = 900 **minutes** (15 hours) instead of 15 minutes. Spec requires 900 seconds (15 minutes).
- **Fix:** Remove the `* 60` multiplier so the config value is treated as minutes directly.

## Bug 3: Revoked token check uses wrong claim
- **File:** `app/auth.py:97`
- **Bug:** `revoke_access_token` stores the token's `jti` in `_revoked_tokens`, but the check in `get_token_payload` reads `payload.get("sub")` (the user id) instead of `payload.get("jti")`. Result: revoked tokens are never actually rejected.
- **Fix:** Change `payload.get("sub")` to `payload.get("jti")`.

## Bug 4: Refresh tokens not invalidated after use
- **File:** `app/routers/auth.py:83-93`
- **Bug:** The `/auth/refresh` endpoint issues new tokens without tracking the old refresh token, so a refresh token can be reused indefinitely, violating the single-use requirement.
- **Fix:** Add a `_used_refresh_tokens` set; check if the presented refresh token's `jti` is in it (reject with 401), then add it before issuing new tokens.

## Bug 5: Overlap check prevents back-to-back bookings
- **File:** `app/routers/bookings.py:53`
- **Bug:** Uses `<=` on both comparisons (`b.start_time <= end and start <= b.end_time`). Spec says overlap iff `existing.start < new.end AND new.start < existing.end`. Using `<=` falsely flags back-to-back bookings (e.g. 10:00–12:00 and 12:00–14:00) as conflicts.
- **Fix:** Change both `<=` to `<`.

## Bug 6: Grace window for past start times
- **File:** `app/routers/bookings.py:89`
- **Bug:** `start <= now - timedelta(seconds=300)` allows booking start times up to 5 minutes in the past. Spec says "strictly in the future — no grace window."
- **Fix:** Change to `start <= now`.

## Bug 7: Missing minimum duration check
- **File:** `app/routers/bookings.py:96`
- **Bug:** The duration validation only checks `duration_hours > MAX_DURATION_HOURS` but never checks `duration_hours < MIN_DURATION_HOURS`. A 0-hour or negative booking would be accepted.
- **Fix:** Add `duration_hours < MIN_DURATION_HOURS` to the range check.

## Bug 8: Wrong sort order in booking listing
- **File:** `app/routers/bookings.py:142`
- **Bug:** `Booking.start_time.desc()` sorts descending. Spec says ascending by start time.
- **Fix:** Change to `Booking.start_time.asc()`.

## Bug 9: Wrong pagination offset
- **File:** `app/routers/bookings.py:143`
- **Bug:** `.offset(page * limit)` skips the first page. Page 1 should show items 0–9 (offset 0), not skip to items 10–19.
- **Fix:** Change to `.offset((page - 1) * limit)`.

## Bug 10: Hardcoded pagination limit
- **File:** `app/routers/bookings.py:144`
- **Bug:** `.limit(10)` ignores the user-supplied `limit` parameter (max 100).
- **Fix:** Change to `.limit(limit)`.

## Bug 11: Start_time overwritten with created_at in booking detail
- **File:** `app/routers/bookings.py:170`
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrites the correct start_time (set by `serialize_booking`) with the booking's `created_at` timestamp.
- **Fix:** Remove the erroneous line.

## Bug 12: Incorrect refund tier thresholds and 0% case
- **File:** `app/routers/bookings.py:205-210`
- **Bug:** (a) Uses `> 48` instead of `>= 48`, so exactly-48-hours notice gets 50% instead of 100%. (b) The `else` branch returns 50% instead of 0%. Spec: notice < 24h → 0%.
- **Fix:** Change threshold to `>= 48`, use `notice_hours >= 24` for consistency, and set `else` to 0.

## Bug 13: Refund amount calculation inconsistency between cancel response and RefundLog
- **File:** `app/routers/bookings.py:212`, `app/services/refunds.py:14`
- **Bug:** The cancel endpoint calculates `refund_amount_cents` via `round(price_cents * percent / 100)` (banker's rounding) and returns it, but `log_refund` recalculates via `price_cents → dollars → percent → int(dollars * 100)` (truncation). These can produce different values (e.g. 149¢ at 50% → 150 vs 149). Spec: "amount returned by cancel must equal amount stored in RefundLog."
- **Fix:** Use `int(x + 0.5)` for half-up rounding per spec. Pass the pre-computed `refund_amount_cents` directly to `log_refund` instead of recalculating.

## Bug 14: Reference code generation not thread-safe
- **File:** `app/services/reference.py:17-21`
- **Bug:** Read-modify-write of `_counter["value"]` without a lock allows duplicate reference codes under concurrent requests. Spec: "unique including under concurrent creation."
- **Fix:** Wrap the counter read/increment/format in `threading.Lock`.

## Bug 15: Rate limiter not thread-safe
- **File:** `app/services/ratelimit.py:18-26`
- **Bug:** The rolling-window bucket is read, filtered, appended, and written back without a lock. Concurrent requests can bypass the rate limit by seeing stale bucket state.
- **Fix:** Wrap the bucket operation in `threading.Lock`.

## Bug 16: Live room stats not thread-safe
- **File:** `app/services/stats.py:15-26`
- **Bug:** `record_create` and `record_cancel` read the current stats dict, compute new values, and write back — all without a lock. Concurrent booking creation/cancellation can lose increments/decrements.
- **Fix:** Wrap each operation in `threading.Lock`.

## Bug 17: Booking creation not protected against concurrent double-booking
- **File:** `app/routers/bookings.py:103-122`
- **Bug:** The conflict check and quota check happen outside any lock, allowing two concurrent requests to both see no conflict and both create bookings for the same slot. Spec: "Must hold under concurrent requests."
- **Fix:** Wrap conflict check + quota check + booking insert in `_booking_lock` (per-process `threading.Lock`).

## Bug 18: Usage report cache not invalidated on booking creation
- **File:** `app/routers/bookings.py:126`
- **Bug:** `create_booking` calls `invalidate_availability` but not `invalidate_report`, so the usage-report cache becomes stale when new bookings are made.
- **Fix:** Add `cache.invalidate_report(user.org_id)` after booking creation.

## Bug 19: Availability cache not invalidated on cancellation
- **File:** `app/routers/bookings.py:222`
- **Bug:** `cancel_booking` invalidates the report cache but not the availability cache, so cancelled bookings still appear in availability until the TTL.
- **Fix:** Add `cache.invalidate_availability(booking.room_id, ...)` in the cancel handler.

## Bug 20: Duplicate username returns 200 instead of 409
- **File:** `app/routers/auth.py:39-45`
- **Bug:** When a duplicate username is registered within the same org, the code returns the existing user data with status 200 instead of raising `409 USERNAME_TAKEN`.
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)` instead of returning the existing user.

## Bug 21: `datetime.fromisoformat` fails on Z suffix in Python 3.10
- **File:** `app/timeutils.py:11`
- **Bug:** Python 3.10's `fromisoformat` doesn't accept `Z` suffix. Input like `2026-07-09T10:00:00Z` would raise an error.
- **Fix:** Convert `Z`/`z` suffix to `+00:00` before parsing.

## Bug 22: Admin listing only shows own bookings
- **File:** `app/routers/bookings.py:139`
- **Bug:** `list_bookings` filters by `Booking.user_id == user.id` for all users. Admins should see all bookings in their org per Rule 10.
- **Fix:** For admins, query all bookings joined with Room filtered by org_id.

## Bug 23: Non-atomic refund + cancel transaction
- **File:** `app/services/refunds.py:22-23`, `app/routers/bookings.py:214-218`
- **Bug:** `log_refund` commits the RefundLog immediately, then `cancel_booking` commits the status change separately. If the second commit fails, the RefundLog exists without the cancellation.
- **Fix:** Remove `db.commit()` and `db.refresh()` from `log_refund`; let the single `db.commit()` in `cancel_booking` handle both atomically.

## Bug 24: Race condition on concurrent org registration
- **File:** `app/routers/auth.py:26-32`
- **Bug:** Two concurrent requests for the same new org name both see `org is None`, both create orgs, and one hits a unique constraint error → 500.
- **Fix:** Wrap org creation in try/except IntegrityError to handle the race gracefully.

## Bug 25: Cross-org data leak in CSV export
- **File:** `app/services/export.py:22-29`, `app/routers/admin.py:72`
- **Bug:** `fetch_bookings_raw` doesn't scope by org_id. An admin could request export for a room_id belonging to another org and get leaked data.
- **Fix:** Add `org_id` parameter to `fetch_bookings_raw` and join with Room to filter.

## Bug 26: Reference code counter resets on restart
- **File:** `app/services/reference.py:9`
- **Bug:** The counter starts at 1000 every time the server restarts, even if bookings already exist with higher codes, causing duplicate reference codes.
- **Fix:** Add `init_counter_from_db()` called on startup that initializes the counter from the highest existing booking reference code.

## Bug 27: No DB-level uniqueness on reference_code
- **File:** `app/models.py:55`
- **Bug:** No UNIQUE constraint on `reference_code` column, allowing possible duplicates at the DB level.
- **Fix:** Add `unique=True` to the column definition.

## Bug 28: GET /bookings/{id} lets members read other members' bookings (Rule 10)
- **File:** `app/routers/bookings.py:164-171`
- **Bug:** The `get_booking` endpoint scopes by org but not by user_id for members. A member can read any booking in their org. Spec: "Another member's booking id → 404 BOOKING_NOT_FOUND."
- **Fix:** Add `if user.role != "admin" and booking.user_id != user.id: raise AppError(404, ...)` after the existence check.

## Bug 29: Concurrent cancel causes multiple RefundLog entries (Rule 6)
- **File:** `app/routers/bookings.py:186-233`
- **Bug:** `cancel_booking` has no mutex. Two concurrent requests both see `status == "confirmed"`, both call `log_refund`, both commit → two RefundLog entries for one booking. Spec: "A cancelled booking has exactly one RefundLog entry."
- **Fix:** Add `_cancel_lock` and wrap the cancel logic (status check → log_refund → commit) in `with _cancel_lock:`.

## Bug 30: Opposite lock ordering in notifications → deadlock (Rule 16)
- **File:** `app/services/notifications.py`
- **Bug:** `notify_created` acquires `_email_lock` then `_audit_lock`; `notify_cancelled` acquires `_audit_lock` then `_email_lock`. If one thread is in each simultaneously, they deadlock. Spec: "No combination of concurrent requests may hang the service."
- **Fix:** Use the same lock ordering (email → audit) in both functions.

## Bug 31: init_counter_from_db not triggered by module-level TestClient (Rule 7)
- **File:** `app/main.py`
- **Bug:** `@app.on_event("startup")` doesn't fire when Starlette's `TestClient(app)` is instantiated at module level (outside a context manager). So `init_counter_from_db` never runs, the counter stays at 1000, and reference codes collide with existing DB data.
- **Fix:** Move `init_counter_from_db` call out of the startup event to module level, right after `Base.metadata.create_all`.

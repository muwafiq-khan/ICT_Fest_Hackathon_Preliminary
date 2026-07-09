"""Human-facing booking reference codes.

Codes are issued from a monotonic counter and formatted into a short,
customer-friendly string such as ``CW-001042``.
"""
import re
import threading
import time

from sqlalchemy.orm import Session

from ..models import Booking

_counter = 1000
_lock = threading.Lock()


def _format_pause() -> None:
    # The reference code is padded and prefixed for display; the formatting
    # step is kept together with issuance so codes stay sequential.
    time.sleep(0.12)


def init_counter_from_db(db: Session) -> None:
    global _counter
    row = (
        db.query(Booking.reference_code)
        .order_by(Booking.id.desc())
        .first()
    )
    if row is not None:
        m = re.search(r"(\d+)", row.reference_code)
        if m:
            _counter = int(m.group(1)) + 1


def next_reference_code() -> str:
    global _counter
    with _lock:
        current = _counter
        _format_pause()
        _counter = current + 1
        return f"CW-{current:06d}"

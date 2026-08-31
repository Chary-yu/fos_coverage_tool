"""Small, explicit retry policy for transactional database work.

Only server-reported deadlocks and lock-wait timeouts are retryable.  The
caller supplies one complete transaction operation so a retry always rolls
back and replays the smallest safe unit rather than an entire import.
"""

from __future__ import print_function

import time

from app.db.transaction import transaction


RETRYABLE_DEADLOCK_CODES = frozenset((1205, 1213))


def is_retryable_deadlock(error):
    """Return True for MariaDB/MySQL deadlock or lock-wait errors only."""
    args = getattr(error, "args", ()) or ()
    for value in args:
        try:
            if int(value) in RETRYABLE_DEADLOCK_CODES:
                return True
        except (TypeError, ValueError):
            continue
    message = str(error or "").lower()
    return ("deadlock" in message or
            "lock wait timeout" in message or
            "lock wait time-out" in message)


def run_transaction_with_deadlock_retry(connection, operation,
                                        max_retries=2, base_delay=0.05,
                                        sleep=time.sleep):
    """Run ``operation`` in a bounded transaction retry loop.

    ``max_retries`` counts retries after the first attempt.  The return value
    is the operation result; non-deadlock failures and exhausted retries are
    propagated unchanged.
    """
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        try:
            with transaction(connection) as conn:
                return operation(conn)
        except Exception as error:
            if not is_retryable_deadlock(error) or attempt >= retries:
                raise
            delay = max(0.0, float(base_delay)) * (2 ** attempt)
            if delay:
                sleep(delay)
    raise RuntimeError("deadlock retry loop did not return")


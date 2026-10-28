"""The single certificate insert helper, used by the API, the seed and gen_large."""

import secrets
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SERIAL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
SERIAL_ATTEMPTS = 5


def new_serial() -> str:
    def block():
        return "".join(secrets.choice(SERIAL_ALPHABET) for _ in range(4))
    return f"CERT-{block()}-{block()}"


def utc_iso(dt: datetime) -> str:
    """'2026-01-10T13:30:00Z'. Rejects naive datetimes."""
    if dt.tzinfo is None:
        raise ValueError("issued_at_utc must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date_time(dt: datetime, tz_name: str) -> tuple[str, str]:
    """(issued_local_date, issued_local_time) in the tenant's timezone."""
    if dt.tzinfo is None:
        raise ValueError("issued_at_utc must be timezone-aware")
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:%M")


def issue_certificate(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    course_id: int,
    learner_id: int,
    issued_at_utc: datetime,
    bulk_operation_id: int | None = None,
) -> int:
    """Insert one active certificate and return its id.

    The caller owns the transaction: this never BEGINs or COMMITs. A duplicate
    (tenant, course, learner) raises sqlite3.IntegrityError from the UNIQUE constraint.
    """
    row = conn.execute(
        "SELECT t.timezone, c.title AS course_title, l.name AS learner_name "
        "FROM tenants t "
        "JOIN courses c ON c.tenant_id = t.id AND c.id = ? "
        "JOIN learners l ON l.tenant_id = t.id AND l.id = ? "
        "WHERE t.id = ?",
        (course_id, learner_id, tenant_id),
    ).fetchone()
    if row is None:
        raise LookupError("course or learner not found in tenant")

    local_date, local_time = local_date_time(issued_at_utc, row["timezone"])
    for attempt in range(SERIAL_ATTEMPTS):
        try:
            cur = conn.execute(
                "INSERT INTO certificates (tenant_id, course_id, learner_id, learner_name, "
                "course_title, serial, status, issued_at_utc, issued_local_date, "
                "issued_local_time, bulk_operation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (tenant_id, course_id, learner_id, row["learner_name"], row["course_title"],
                 new_serial(), utc_iso(issued_at_utc), local_date, local_time,
                 bulk_operation_id),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            # Retry only a random serial collision; everything else propagates.
            if "certificates.serial" not in str(e) or attempt == SERIAL_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def record_audit_event(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    actor_admin_id: int,
    action: str,
    certificate_id: int,
    created_at_utc: datetime,
    bulk_operation_id: int | None = None,
) -> int:
    """Insert one audit event. The caller owns the transaction."""
    cur = conn.execute(
        "INSERT INTO audit_events (tenant_id, actor_admin_id, action, certificate_id, "
        "bulk_operation_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, actor_admin_id, action, certificate_id, bulk_operation_id,
         utc_iso(created_at_utc)),
    )
    return cur.lastrowid

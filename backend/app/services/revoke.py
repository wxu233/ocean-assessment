"""Revocation: a conditional update plus one audit event, in one BEGIN IMMEDIATE transaction."""

import sqlite3
from datetime import datetime, timezone

from .. import queries
from .certificates import record_audit_event, utc_iso


def revoke_certificate(
    conn: sqlite3.Connection, *, tenant_id: int, admin_id: int, cert_id: int
) -> dict | None:
    """Revoke and return the CertDetail, or None if the id is missing or in another tenant.

    Only the request whose UPDATE flips 'active' -> 'revoked' writes the audit event, so
    repeats and concurrent revokes return the current state without a second event.
    """
    now = datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE certificates SET status = 'revoked', revoked_at_utc = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'active'",
            (utc_iso(now), cert_id, tenant_id),
        )
        if cur.rowcount == 1:
            record_audit_event(conn, tenant_id=tenant_id, actor_admin_id=admin_id,
                               action="certificate.revoked", certificate_id=cert_id,
                               created_at_utc=now)
        cert = queries.get_certificate(conn, tenant_id, cert_id)
        conn.execute("COMMIT")
        return cert
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

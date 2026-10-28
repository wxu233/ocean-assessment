"""Bulk issue: one BEGIN IMMEDIATE transaction per request, idempotent per (tenant, key)."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .certificates import issue_certificate, record_audit_event, utc_iso

# sqlite3's message when the certificate UNIQUE(tenant_id, course_id, learner_id) fails.
DUPLICATE_CERT = "certificates.tenant_id, certificates.course_id, certificates.learner_id"


class KeyReused(Exception):
    """The idempotency key exists in this tenant with a different request."""


class CourseNotFound(Exception):
    """The course doesn't exist or belongs to another tenant."""


def request_hash(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_bulk_issue(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    admin_id: int,
    key: str,
    body: dict,
) -> str:
    """Run (or replay) a bulk issue and return the BulkResult JSON string.

    BEGIN IMMEDIATE takes the write lock before the key lookup, so concurrent requests
    with the same key serialize here: the first runs the operation, later ones replay it.
    """
    req_hash = request_hash(body)
    course_id = body["course_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT request_hash, response_json FROM bulk_operations "
            "WHERE tenant_id = ? AND idempotency_key = ?",
            (tenant_id, key),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            if existing["request_hash"] != req_hash:
                raise KeyReused()
            return existing["response_json"]

        # Ownership check before anything is written.
        course = conn.execute(
            "SELECT id FROM courses WHERE id = ? AND tenant_id = ?", (course_id, tenant_id)
        ).fetchone()
        if course is None:
            conn.execute("ROLLBACK")
            raise CourseNotFound()

        now = datetime.now(timezone.utc)
        # response_json is filled in before COMMIT; the row is never visible without it.
        op_id = conn.execute(
            "INSERT INTO bulk_operations (tenant_id, admin_id, idempotency_key, request_hash, "
            "course_id, status, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'completed', '', ?)",
            (tenant_id, admin_id, key, req_hash, course_id, utc_iso(now)),
        ).lastrowid

        results = [_issue_one(conn, tenant_id, admin_id, course_id, op_id, row, now)
                   for row in _enrollments(conn, tenant_id, course_id)]
        summary = {o: sum(r["outcome"] == o for r in results)
                   for o in ("issued", "skipped", "failed")}
        response_json = json.dumps({"operation_id": op_id, "course_id": course_id,
                                    "summary": summary, "results": results})
        conn.execute(
            "UPDATE bulk_operations SET response_json = ? WHERE id = ? AND tenant_id = ?",
            (response_json, op_id, tenant_id),
        )
        conn.execute("COMMIT")
        return response_json
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _enrollments(conn: sqlite3.Connection, tenant_id: int, course_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.learner_id, l.name AS learner_name, e.status, c.id AS certificate_id "
        "FROM enrollments e "
        "JOIN learners l ON l.tenant_id = e.tenant_id AND l.id = e.learner_id "
        "LEFT JOIN certificates c ON c.tenant_id = e.tenant_id "
        "  AND c.course_id = e.course_id AND c.learner_id = e.learner_id "
        "WHERE e.tenant_id = ? AND e.course_id = ? ORDER BY e.learner_id",
        (tenant_id, course_id),
    ).fetchall()


def _issue_one(conn, tenant_id, admin_id, course_id, op_id, row, now) -> dict:
    result = {"learner_id": row["learner_id"], "learner_name": row["learner_name"],
              "outcome": "skipped", "reason": None, "certificate_id": None}
    if row["status"] != "completed":
        result["reason"] = "not_completed"
        return result
    if row["certificate_id"] is not None:
        result["reason"] = "already_certified"
        return result

    conn.execute("SAVEPOINT learner")
    try:
        cert_id = issue_certificate(conn, tenant_id=tenant_id, course_id=course_id,
                                    learner_id=row["learner_id"], issued_at_utc=now,
                                    bulk_operation_id=op_id)
        record_audit_event(conn, tenant_id=tenant_id, actor_admin_id=admin_id,
                           action="certificate.issued", certificate_id=cert_id,
                           created_at_utc=now, bulk_operation_id=op_id)
    except Exception as e:
        conn.execute("ROLLBACK TO learner")
        conn.execute("RELEASE learner")
        if isinstance(e, sqlite3.IntegrityError) and DUPLICATE_CERT in str(e):
            result["reason"] = "already_certified"
        else:
            result["outcome"], result["reason"] = "failed", str(e) or type(e).__name__
        return result
    conn.execute("RELEASE learner")
    result["outcome"], result["certificate_id"] = "issued", cert_id
    return result

"""Add N certificates for one tenant under the course "Load Test 101" (A6).

Only inserts: a new learner, a completed enrollment, a certificate and its audit
event per row. Existing rows are never changed. Honours APP_DB_PATH.

Ties are deliberate so sort stability can be checked: rows come in pairs that share
a learner name *and* an issue time, and both names and times repeat across pairs.
About 30% of learner names start with "J", so `q=j` is a large but strict subset.
"""

import argparse
from datetime import datetime, timedelta, timezone

from backend.app import db
from backend.app.services.certificates import issue_certificate, record_audit_event

COURSE_TITLE = "Load Test 101"
FIRST_NAMES = [
    "James", "Jane", "Jordan", "Julia", "Jack", "Jasmine",
    "Aisha", "Ben", "Chloe", "Daniel", "Emma", "Farah", "Grace",
    "Hiro", "Isla", "Liam", "Mia", "Noah", "Olivia", "Zoe",
]
LAST_NAMES = ["Nguyen", "Smith", "Patel", "Brown", "Wilson", "Garcia", "Tan", "Kelly"]
BASE_UTC = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
SLOT_MINUTES = 47


def row_shape(i: int, count: int) -> tuple[str, datetime]:
    """(learner name, issued_at) for row i. Rows 2k and 2k+1 always tie on both."""
    pair = i // 2
    first = FIRST_NAMES[pair % len(FIRST_NAMES)]
    last = LAST_NAMES[(pair // len(FIRST_NAMES)) % len(LAST_NAMES)]
    slots = max(1, count // 8)
    return f"{first} {last}", BASE_UTC + timedelta(minutes=SLOT_MINUTES * (pair % slots))


def generate(conn, tenant_name: str, count: int) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        tenant = conn.execute("SELECT id FROM tenants WHERE name = ?", (tenant_name,)).fetchone()
        if tenant is None:
            raise SystemExit(f"unknown tenant: {tenant_name}")
        tenant_id = tenant["id"]
        admin_id = conn.execute(
            "SELECT MIN(id) FROM admins WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        row = conn.execute("SELECT id FROM courses WHERE tenant_id = ? AND title = ?",
                           (tenant_id, COURSE_TITLE)).fetchone()
        course_id = row["id"] if row else conn.execute(
            "INSERT INTO courses (tenant_id, title) VALUES (?, ?)",
            (tenant_id, COURSE_TITLE)).lastrowid

        for i in range(count):
            name, issued_at = row_shape(i, count)
            learner_id = conn.execute(
                "INSERT INTO learners (tenant_id, name) VALUES (?, ?)",
                (tenant_id, name)).lastrowid
            completed_at = (issued_at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO enrollments (tenant_id, course_id, learner_id, status, "
                "completed_at) VALUES (?, ?, ?, 'completed', ?)",
                (tenant_id, course_id, learner_id, completed_at))
            cert_id = issue_certificate(conn, tenant_id=tenant_id, course_id=course_id,
                                        learner_id=learner_id, issued_at_utc=issued_at)
            record_audit_event(conn, tenant_id=tenant_id, actor_admin_id=admin_id,
                               action="certificate.issued", certificate_id=cert_id,
                               created_at_utc=issued_at)
        conn.execute("COMMIT")
        return course_id
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="East", help="tenant name (default: East)")
    parser.add_argument("--count", type=int, default=100_000, help="certificates to add")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    path = db.db_path()
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        generate(conn, args.tenant, args.count)
    finally:
        conn.close()
    print(f"added {args.count} certificates for {args.tenant} under {COURSE_TITLE!r}: {path}")


if __name__ == "__main__":
    main()

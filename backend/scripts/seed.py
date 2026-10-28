"""Create the schema and the demo data.

Idempotent: does nothing if tenants already exist. `--reset` deletes the
database file first. Honours APP_DB_PATH.
"""

import argparse
from datetime import datetime

from backend.app import db
from backend.app.services.certificates import issue_certificate, record_audit_event

A5_ISSUED_AT = "2026-01-10T13:30:00Z"

# Each tenant: (name, tz, admin email, token, courses).
# A course: (title, [(learner, status, completed_at, issued_at or None)]).
TENANTS = [
    ("East", "Australia/Sydney", "alice@east.test", "east-alice-token", [
        ("Safety 101", [
            ("Alex Lee", "completed", "2026-01-09T22:00:00Z", A5_ISSUED_AT),
            ("Priya Shah", "completed", "2026-02-02T23:00:00Z", "2026-02-03T01:15:00Z"),
            ("Sam Carter", "completed", "2026-03-01T04:00:00Z", None),
            ("Jordan Kim", "completed", "2026-03-04T06:30:00Z", None),
            ("Morgan Diaz", "incomplete", None, None),
        ]),
        ("First Aid 201", [
            ("Alex Lee", "completed", "2026-03-10T02:00:00Z", None),
            ("Priya Shah", "incomplete", None, None),
        ]),
    ]),
    ("West", "Australia/Perth", "bob@west.test", "west-bob-token", [
        ("Safety 101", [
            ("Alex Lee", "completed", "2026-01-09T20:00:00Z", A5_ISSUED_AT),
            ("Chris Ng", "completed", "2026-02-10T03:00:00Z", "2026-02-10T05:45:00Z"),
            ("Taylor Brooks", "completed", "2026-03-02T01:00:00Z", None),
            ("Riley Evans", "incomplete", None, None),
        ]),
    ]),
]


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


def seed(conn) -> bool:
    """Insert the demo data in one transaction. Returns False if already seeded."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            conn.execute("ROLLBACK")
            return False
        for name, tz, email, token, courses in TENANTS:
            tenant_id = conn.execute(
                "INSERT INTO tenants (name, timezone) VALUES (?, ?)", (name, tz)).lastrowid
            admin_id = conn.execute(
                "INSERT INTO admins (tenant_id, email, token) VALUES (?, ?, ?)",
                (tenant_id, email, token)).lastrowid
            learner_ids = {}
            for title, enrollments in courses:
                course_id = conn.execute(
                    "INSERT INTO courses (tenant_id, title) VALUES (?, ?)",
                    (tenant_id, title)).lastrowid
                for learner, status, completed_at, issued_at in enrollments:
                    if learner not in learner_ids:
                        learner_ids[learner] = conn.execute(
                            "INSERT INTO learners (tenant_id, name) VALUES (?, ?)",
                            (tenant_id, learner)).lastrowid
                    learner_id = learner_ids[learner]
                    conn.execute(
                        "INSERT INTO enrollments (tenant_id, course_id, learner_id, status, "
                        "completed_at) VALUES (?, ?, ?, ?, ?)",
                        (tenant_id, course_id, learner_id, status, completed_at))
                    if issued_at:
                        cert_id = issue_certificate(
                            conn, tenant_id=tenant_id, course_id=course_id,
                            learner_id=learner_id, issued_at_utc=_utc(issued_at))
                        record_audit_event(
                            conn, tenant_id=tenant_id, actor_admin_id=admin_id,
                            action="certificate.issued", certificate_id=cert_id,
                            created_at_utc=_utc(issued_at))
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    args = parser.parse_args()

    path = db.db_path()
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        created = seed(conn)
    finally:
        conn.close()
    print(f"{'seeded' if created else 'already seeded'}: {path}")


if __name__ == "__main__":
    main()

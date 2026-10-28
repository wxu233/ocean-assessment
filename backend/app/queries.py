"""Tenant-scoped SQL. Every function that reads tenant data takes tenant_id.

The two exceptions are actor lookups, which run before a tenant is known:
`list_actors` (demo picker) and `actor_by_token` (resolves the tenant).
"""

import sqlite3

CERT_SUMMARY_COLS = (
    "id, serial, learner_name, course_title, issued_local_date, issued_local_time, status"
)
CERT_DETAIL_COLS = CERT_SUMMARY_COLS + ", issued_at_utc, revoked_at_utc"


def list_actors(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT a.email, t.name AS tenant_name, a.token "
        "FROM admins a JOIN tenants t ON t.id = a.tenant_id ORDER BY a.id"
    ).fetchall()
    return [dict(r) for r in rows]


def actor_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT a.id AS admin_id, a.email, a.tenant_id, t.name AS tenant_name, t.timezone "
        "FROM admins a JOIN tenants t ON t.id = a.tenant_id WHERE a.token = ?",
        (token,),
    ).fetchone()


def list_courses(conn: sqlite3.Connection, tenant_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title FROM courses WHERE tenant_id = ? ORDER BY title, id", (tenant_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_certificate(conn: sqlite3.Connection, tenant_id: int, cert_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {CERT_DETAIL_COLS} FROM certificates WHERE tenant_id = ? AND id = ?",
        (tenant_id, cert_id),
    ).fetchone()
    return dict(row) if row else None

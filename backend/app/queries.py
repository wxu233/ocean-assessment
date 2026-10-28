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


# Whitelists: request values map to fixed SQL fragments; nothing from the request is
# interpolated. `issued_date` sorts on the stored tenant-local columns (A5).
SORT_COLUMNS = {
    "issued_date": ("issued_local_date", "issued_local_time"),
    "learner_name": ("learner_name",),
    "course_title": ("course_title",),
    "serial": ("serial",),
    "status": ("status",),
}
SORT_DIRS = {"asc": "ASC", "desc": "DESC"}
EXPORT_COLS = "learner_name, course_title, serial, issued_local_date"


def _like_prefix(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _cert_filter(tenant_id: int, q: str | None) -> tuple[str, list]:
    """WHERE clause: tenant, plus `q` as a case-insensitive prefix of the learner name or
    the serial (an exact serial is its own prefix). LIKE ignores case for ASCII only."""
    if not q:
        return "tenant_id = ?", [tenant_id]
    pattern = _like_prefix(q)
    return (
        "tenant_id = ? AND (learner_name LIKE ? ESCAPE '\\' OR serial LIKE ? ESCAPE '\\')",
        [tenant_id, pattern, pattern],
    )


def build_cert_query(
    tenant_id: int, *, q: str | None, sort: str, direction: str, columns: str
) -> tuple[str, list]:
    """The one SELECT shared by the list and the export, so both return the same rows
    in the same order. Ties are always broken by id ASC. Unknown sort/dir raise KeyError."""
    d = SORT_DIRS[direction]
    order = ", ".join(f"{col} {d}" for col in SORT_COLUMNS[sort]) + ", id ASC"
    where, params = _cert_filter(tenant_id, q)
    return f"SELECT {columns} FROM certificates WHERE {where} ORDER BY {order}", params


def list_certificates(
    conn: sqlite3.Connection, tenant_id: int, *, q: str | None, sort: str, direction: str,
    page: int, page_size: int,
) -> dict:
    where, params = _cert_filter(tenant_id, q)
    sql, sel_params = build_cert_query(tenant_id, q=q, sort=sort, direction=direction,
                                       columns=CERT_SUMMARY_COLS)
    # Count and page in one read transaction so `total` matches `items`.
    conn.execute("BEGIN")
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM certificates WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"{sql} LIMIT ? OFFSET ?",
                            [*sel_params, page_size, (page - 1) * page_size]).fetchall()
    finally:
        conn.execute("COMMIT")
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size}


def iter_export_rows(
    conn: sqlite3.Connection, tenant_id: int, *, q: str | None, sort: str, direction: str,
    batch_size: int = 2000,
):
    """Yield batches of export rows from one read transaction (a single WAL snapshot),
    so a long export sees a consistent view and never blocks writers or the list."""
    sql, params = build_cert_query(tenant_id, q=q, sort=sort, direction=direction,
                                   columns=EXPORT_COLS)
    conn.execute("BEGIN")
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        while rows := cur.fetchmany(batch_size):
            yield rows
    finally:
        cur.close()  # finalize the statement first, even if the client went away mid-stream
        conn.execute("COMMIT")

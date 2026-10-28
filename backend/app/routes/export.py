import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from .. import db, queries
from ..deps import Actor, get_actor
from .certificates import SortDir, SortKey

router = APIRouter(prefix="/api/certificates")

CSV_HEADER = ["learner_name", "course_title", "serial", "issued_local_date"]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str) -> str:
    """Stop spreadsheets from running a cell as a formula (CSV injection, per OWASP):
    a leading quote makes Excel/Sheets show it as text. Stored data is unchanged."""
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def _csv_chunks(tenant_id: int, q: str | None, sort: str, direction: str):
    """Sync generator: Starlette runs it in the threadpool, so a long export never
    blocks the event loop. It owns its connection, separate from the request's."""
    conn = db.connect()
    try:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(CSV_HEADER)
        for rows in queries.iter_export_rows(conn, tenant_id, q=q, sort=sort,
                                             direction=direction):
            writer.writerows([_safe_cell(v) for v in r] for r in rows)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
        if buf.tell():  # header only, when nothing matched
            yield buf.getvalue()
    finally:
        conn.close()


@router.get("/export.csv")
def export_csv(
    q: str | None = None,
    sort: SortKey = "issued_date",
    dir: SortDir = "desc",
    actor: Actor = Depends(get_actor),
):
    return StreamingResponse(
        _csv_chunks(actor.tenant_id, q, sort, dir),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="certificates.csv"'},
    )

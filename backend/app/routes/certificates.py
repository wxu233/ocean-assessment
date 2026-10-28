import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query

from .. import queries
from ..deps import Actor, get_actor, get_conn
from ..errors import not_found
from ..services.revoke import revoke_certificate

router = APIRouter(prefix="/api/certificates")

# Unknown values are a 422 here; queries.SORT_COLUMNS / SORT_DIRS map them to SQL.
SortKey = Literal["issued_date", "learner_name", "course_title", "serial", "status"]
SortDir = Literal["asc", "desc"]


@router.get("")
def list_certificates(
    q: str | None = None,
    sort: SortKey = "issued_date",
    dir: SortDir = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: Actor = Depends(get_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return queries.list_certificates(conn, actor.tenant_id, q=q, sort=sort, direction=dir,
                                     page=page, page_size=page_size)


@router.get("/{cert_id}")
def certificate_detail(
    cert_id: int,
    actor: Actor = Depends(get_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    cert = queries.get_certificate(conn, actor.tenant_id, cert_id)
    if cert is None:
        raise not_found()
    return cert


@router.post("/{cert_id}/revoke")
def revoke(
    cert_id: int,
    actor: Actor = Depends(get_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    cert = revoke_certificate(conn, tenant_id=actor.tenant_id, admin_id=actor.admin_id,
                              cert_id=cert_id)
    if cert is None:
        raise not_found()
    return cert

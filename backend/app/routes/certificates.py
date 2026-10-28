import sqlite3

from fastapi import APIRouter, Depends

from .. import queries
from ..deps import Actor, get_actor, get_conn
from ..errors import not_found

router = APIRouter(prefix="/api/certificates")


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

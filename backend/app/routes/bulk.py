import sqlite3

from fastapi import APIRouter, Depends, Header
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictInt

from ..deps import Actor, get_actor, get_conn
from ..errors import ApiError, not_found
from ..services import bulk_issue

router = APIRouter(prefix="/api/bulk-issues")


class BulkIssueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: StrictInt


@router.post("")
def create_bulk_issue(
    body: BulkIssueBody,
    idempotency_key: str | None = Header(default=None),
    actor: Actor = Depends(get_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
        raise ApiError(400, "invalid_idempotency_key",
                       "Idempotency-Key header is required (8-128 characters).")
    try:
        # The stored string is returned as-is, so first responses and replays are byte-identical.
        response_json = bulk_issue.run_bulk_issue(
            conn, tenant_id=actor.tenant_id, admin_id=actor.admin_id,
            key=idempotency_key, body=body.model_dump())
    except bulk_issue.KeyReused:
        raise ApiError(409, "idempotency_key_reused",
                       "This Idempotency-Key was already used with a different request.")
    except bulk_issue.CourseNotFound:
        raise not_found()
    return Response(content=response_json, media_type="application/json")

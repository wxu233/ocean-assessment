import sqlite3

from fastapi import APIRouter, Depends

from .. import queries
from ..deps import Actor, get_actor, get_conn

router = APIRouter(prefix="/api")


@router.get("/actors")
def actors(conn: sqlite3.Connection = Depends(get_conn)):
    return queries.list_actors(conn)


@router.get("/me")
def me(actor: Actor = Depends(get_actor)):
    return {"email": actor.email, "tenant_name": actor.tenant_name, "timezone": actor.tz}


@router.get("/courses")
def courses(actor: Actor = Depends(get_actor), conn: sqlite3.Connection = Depends(get_conn)):
    return queries.list_courses(conn, actor.tenant_id)

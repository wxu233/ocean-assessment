"""Request dependencies: one connection per request, and actor resolution."""

import sqlite3
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Header

from . import db, queries
from .errors import ApiError


@dataclass(frozen=True)
class Actor:
    admin_id: int
    tenant_id: int
    tz: str
    email: str
    tenant_name: str


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_actor(
    x_actor_token: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Actor:
    row = queries.actor_by_token(conn, x_actor_token) if x_actor_token else None
    if row is None:
        raise ApiError(401, "unauthorized", "Missing or unknown X-Actor-Token.")
    return Actor(admin_id=row["admin_id"], tenant_id=row["tenant_id"], tz=row["timezone"],
                 email=row["email"], tenant_name=row["tenant_name"])

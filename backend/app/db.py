"""SQLite connection factory, pragmas and schema DDL."""

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "app.db"


def db_path() -> Path:
    return Path(os.environ.get("APP_DB_PATH") or DEFAULT_DB_PATH)


def connect(path=None) -> sqlite3.Connection:
    """Open a connection in autocommit mode; callers BEGIN explicitly."""
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  timezone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  email TEXT NOT NULL UNIQUE,
  token TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  title TEXT NOT NULL,
  UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS learners (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL,
  UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS enrollments (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  course_id INTEGER NOT NULL,
  learner_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('completed', 'incomplete')),
  completed_at TEXT,
  UNIQUE (course_id, learner_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES courses(tenant_id, id),
  FOREIGN KEY (tenant_id, learner_id) REFERENCES learners(tenant_id, id),
  CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS bulk_operations (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  admin_id INTEGER NOT NULL REFERENCES admins(id),
  idempotency_key TEXT NOT NULL
    CHECK (length(idempotency_key) BETWEEN 8 AND 128),
  request_hash TEXT NOT NULL,
  course_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('completed')),
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, course_id) REFERENCES courses(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS certificates (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  course_id INTEGER NOT NULL,
  learner_id INTEGER NOT NULL,
  learner_name TEXT NOT NULL,
  course_title TEXT NOT NULL,
  serial TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  issued_at_utc TEXT NOT NULL,
  issued_local_date TEXT NOT NULL,
  issued_local_time TEXT NOT NULL,
  revoked_at_utc TEXT,
  bulk_operation_id INTEGER REFERENCES bulk_operations(id),
  UNIQUE (tenant_id, course_id, learner_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES courses(tenant_id, id),
  FOREIGN KEY (tenant_id, learner_id) REFERENCES learners(tenant_id, id),
  CHECK ((status = 'revoked') = (revoked_at_utc IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS cert_by_date   ON certificates(tenant_id, issued_local_date, id);
CREATE INDEX IF NOT EXISTS cert_by_serial ON certificates(tenant_id, serial);
CREATE INDEX IF NOT EXISTS cert_by_name   ON certificates(tenant_id, learner_name, id);
CREATE INDEX IF NOT EXISTS cert_by_course ON certificates(tenant_id, course_title, id);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
  action TEXT NOT NULL
    CHECK (action IN ('certificate.issued', 'certificate.revoked')),
  certificate_id INTEGER NOT NULL REFERENCES certificates(id),
  bulk_operation_id INTEGER REFERENCES bulk_operations(id),
  created_at TEXT NOT NULL,
  UNIQUE (certificate_id, action)
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)

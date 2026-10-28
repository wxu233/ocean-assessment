SQLite settings on every connection: `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=30000`, and opened with `autocommit=True`. Transactions are always started explicitly.

```sql
tenants(id PK, name TEXT NOT NULL UNIQUE, timezone TEXT NOT NULL)

admins(id PK, tenant_id NOT NULL → tenants, email TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE)

courses(id PK, tenant_id NOT NULL → tenants, title TEXT NOT NULL, UNIQUE(tenant_id, id))

learners(id PK, tenant_id NOT NULL → tenants, name TEXT NOT NULL, UNIQUE(tenant_id, id))

enrollments(
  id PK, tenant_id, course_id, learner_id,
  status TEXT NOT NULL CHECK (status IN ('completed','incomplete')),
  completed_at TEXT,                                  -- UTC ISO-8601
  UNIQUE(course_id, learner_id),
  FK (tenant_id, course_id)  → courses(tenant_id, id),
  FK (tenant_id, learner_id) → learners(tenant_id, id),
  CHECK ((status='completed') = (completed_at IS NOT NULL)))

bulk_operations(
  id PK, tenant_id NOT NULL, admin_id NOT NULL → admins,
  idempotency_key TEXT NOT NULL CHECK (length BETWEEN 8 AND 128),
  request_hash TEXT NOT NULL,                         -- sha256(canonical JSON body)
  course_id NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('completed')),
  response_json TEXT NOT NULL,                        -- replayed verbatim
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, idempotency_key),
  FK (tenant_id, course_id) → courses(tenant_id, id))

certificates(
  id PK, tenant_id, course_id, learner_id,
  learner_name TEXT NOT NULL,                         -- copied at issue time
  course_title TEXT NOT NULL,                         -- copied at issue time
  serial TEXT NOT NULL UNIQUE,                        -- random, e.g. CERT-7K3M-Q9XP
  status TEXT NOT NULL CHECK (status IN ('active','revoked')),
  issued_at_utc TEXT NOT NULL,                        -- '2026-01-10T13:30:00Z'
  issued_local_date TEXT NOT NULL,                    -- '2026-01-11' (tenant tz)
  issued_local_time TEXT NOT NULL,                    -- '2026-01-11 00:30' (tenant tz)
  revoked_at_utc TEXT,
  bulk_operation_id → bulk_operations,
  UNIQUE(tenant_id, course_id, learner_id),
  FK (tenant_id, course_id)  → courses(tenant_id, id),
  FK (tenant_id, learner_id) → learners(tenant_id, id),
  CHECK ((status='revoked') = (revoked_at_utc IS NOT NULL)))
INDEX cert_by_date   (tenant_id, issued_local_date, id)
INDEX cert_by_serial (tenant_id, serial)
INDEX cert_by_name   (tenant_id, learner_name, id)
INDEX cert_by_course (tenant_id, course_title, id)

audit_events(
  id PK, tenant_id NOT NULL, actor_admin_id NOT NULL → admins,
  action TEXT NOT NULL CHECK (action IN ('certificate.issued','certificate.revoked')),
  certificate_id NOT NULL → certificates,
  bulk_operation_id → bulk_operations,
  created_at TEXT NOT NULL,
  UNIQUE(certificate_id, action))
```

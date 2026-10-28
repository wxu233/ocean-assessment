Never break these. Each scenario below lists the rules and the backstop that enforces them.

**A1 — Tenant isolation**
- The tenant always comes from the actor token, never from a request parameter or body.
- Every query filters on `tenant_id`.
- A cross-tenant id returns a 404 identical to a nonexistent id.
- In bulk issue, the course ownership check runs **before** anything is written.
- Backstop: the composite FKs make cross-tenant rows impossible to store.

**A2 — Retry safety**
- A bulk issue is one `BEGIN IMMEDIATE` transaction:
  1. Look up `(tenant_id, key)`.
  2. If it exists with the same hash, replay `response_json`.
  3. Otherwise insert the operation, then the certificates and audit events, store `response_json`, and commit.
- Any exception rolls back everything, including the operation row.
- A learner gets at most one certificate per course, and each issuance has exactly one audit event.

**A3 — Key misuse**
- If the key exists with a different `request_hash`, return 409 with zero writes.
- A stored operation is never updated.

**A4 — Learner outcomes**
- Bulk issue goes through the course's enrollments in this tenant:
  - `incomplete` → `skipped / not_completed`
  - an existing certificate (active **or** revoked) → `skipped / already_certified`
  - `completed` → issued inside a per-learner `SAVEPOINT`
- If the insert hits the unique constraint, report `skipped / already_certified`. Any other error → `failed / <reason>`, rolled back to the savepoint.

**A5 — Local dates**
- `issued_local_date` and `issued_local_time` are computed once, at insert, from the tenant's `ZoneInfo`.
- Display, sort and CSV use only these stored columns.
- The frontend never formats dates in the browser's timezone.

**A6 — Export**
- Export uses the same `build_cert_query()` as the list.
- It streams from a **sync** generator, over its own connection, inside one read transaction.
- Ordering is deterministic thanks to the `id` tiebreaker.
- A list request must succeed while an export is running.
- A 100k-row export must finish in under 60 s.

**A7 — Revocation**
- Revoking runs `UPDATE … SET status='revoked' WHERE id=? AND tenant_id=? AND status='active'`.
  - `rowcount == 1`: insert one audit event.
  - `rowcount == 0`: re-select. Missing → 404. Already revoked → 200 with the current state and no audit event.
- Backstop: `UNIQUE(certificate_id, action)`.
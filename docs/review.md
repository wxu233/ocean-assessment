# Hostile review: tenant isolation, idempotency, revoke, export, local dates

Reviewed against `docs/brief.md`, `AGENTS.md` and the contracts in `docs/`. Every backend finding was reproduced against a live `uvicorn` on a throwaway DB (seed, plus extra rows where stated). The UI finding comes from reading the code. **Nothing has been fixed yet.**

Actors: `A` = `X-Actor-Token: east-alice-token`, `B` = `X-Actor-Token: west-bob-token`. Seed ids: East Safety 101 = 1, West Safety 101 = 3.

## Summary

| # | Severity | Area | Finding |
|---|---|---|---|
| R1 | Medium | All id/int inputs | A 64-bit overflow returns 500 instead of 404/422 |
| R2 | Medium | Local dates, sort | DST fall-back: `issued_date` sort isn't chronological, and two different instants show the same time |
| R3 | Low–Med | Search, export filter | "Ignoring case" works only for ASCII |
| R4 | Low | UI revoke | A revoke response that arrives late overwrites the detail panel of a different certificate |
| R5 | Low | Schema backstop | `audit_events` and `bulk_operations` lack the composite tenant FKs that the invariants claim |
| R6 | Low | Sort | `learner_name` sort is case-sensitive (byte order) |
| R7 | Low | Bulk issue | The response is unbounded (13 MB for a 100k-enrollment course), stored and replayed |
| R8 | Info | Idempotency scope | The key is scoped per tenant, not per admin. The hash ignores the actor |
| R9 | Info | CSV | No UTF-8 BOM, so Excel garbles non-ASCII names |
| R10 | Info | Actor picker | `GET /api/actors` gives every tenant's token to an unauthenticated caller |

---

## R1: Integer overflow turns 404/422 into 500

**Where**
- `backend/app/routes/certificates.py:34` and `:46`: `cert_id: int` has no upper bound.
- `backend/app/queries.py:40-41`: bound into `WHERE … id = ?`.
- `backend/app/routes/certificates.py:23`: `page` has no upper bound. `backend/app/queries.py:98-99` computes `(page - 1) * page_size`.
- `backend/app/routes/bulk.py:16`: `course_id: StrictInt` has no bound. `backend/app/services/bulk_issue.py:56-58` uses it.

**Requests**
```
GET  /api/certificates/99999999999999999999                     (-H "$B")
POST /api/certificates/99999999999999999999/revoke              (-H "$B")
GET  /api/certificates?page=999999999999999999                  (-H "$A")
POST /api/bulk-issues  Idempotency-Key: huge-course-0001
     {"course_id": 99999999999999999999}                        (-H "$A")
```

**Expected:** the first two return 404 with `{"error":{"code":"not_found",…}}`, the same body as any other missing id (api-contract). The page request returns 422 or an empty page. The bulk request returns 404 (or 422).
**Actual:** all four return `500 Internal Server Error` as a plain-text body, not the contract error shape. The log shows `OverflowError: Python int too large to convert to SQLite INTEGER`.

**Impact:** no data leaks and no writes happen (the bulk transaction rolls back). It does break "every error uses the error body" and the 404 contract. An external checker probing ids will see 500s.

---

## R2: DST fall-back breaks `issued_date` ordering and display

**Where**
- `backend/app/queries.py:49`: `issued_date` sorts on `(issued_local_date, issued_local_time)` and has no UTC tiebreak before `id`.
- `backend/app/services/certificates.py:30`: `issued_local_time` is `%Y-%m-%d %H:%M`, with no seconds and no offset.

**Setup:** four East certificates. Sydney DST ends on 2026-04-05, when 03:00 AEDT falls back to 02:00 AEST. They were inserted in this id order:

| id | issued_at_utc | issued_local_time |
|---|---|---|
| 8 | 2026-04-04T16:30Z | 2026-04-05 02:30 (AEST) |
| 9 | 2026-04-04T15:30Z | 2026-04-05 02:30 (AEDT) |
| 10 | 2026-04-04T16:10Z | 2026-04-05 02:10 (AEST) |
| 11 | 2026-04-04T15:45Z | 2026-04-05 02:45 (AEDT) |

**Request:** `GET /api/certificates?sort=issued_date&dir=asc&page_size=100` (-H "$A"). The CSV export with the same params gives the same order.

**Expected (chronological):** 9, 11, 10, 8.
**Actual:** 10, 8, 9, 11. The certificate issued last (8) sorts before the one issued first (9). Ids 8 and 9 both show `2026-04-05 02:30` in the list and the CSV, although they are an hour apart.

**Notes:** the code follows `docs/api-contract.md` exactly ("`issued_date` sorts on `issued_local_date, issued_local_time`"), so this is a spec gap, not a coding slip. The brief needs only the *calendar date* to be tenant-local. Adding `issued_at_utc` as a sort key after the local date would give real chronology and leave date grouping unchanged, but it means a contract change. That decision is yours. The detail view shows `issued_at_utc`, so it can tell the two apart. The list and the CSV can't.

---

## R3: Case-insensitive search is ASCII-only

**Where:** `backend/app/queries.py:63-71`. SQLite's `LIKE` folds case only for ASCII. The docstring admits this, but the README doesn't.

**Setup:** East certificates for `Émile Zola` and `émile zola`.

**Requests:**
```
GET /api/certificates?q=%C3%A9mile      (q=émile, -H "$A")
GET /api/certificates?q=%C3%89MILE      (q=ÉMILE, -H "$A")
```

**Expected:** both return both rows. The contract says `q` matches by prefix "ignoring case".
**Actual:** `q=émile` returns only `émile zola`, and `q=ÉMILE` returns only `Émile Zola` (`total: 1` each). The export with the same `q` has the same gap, because it shares `build_cert_query`.

---

## R4: UI: a late revoke response overwrites a different certificate's panel

**Where:** `frontend/src/App.jsx:288-299`. `revoke()` calls `setCert(...)` without the `live` guard that the load effect uses at `:273-286`. `CertificateDetail` is reused across selections because it isn't keyed by `id` (`App.jsx:107-108`).

**Steps:** open certificate X and click **Revoke… → Confirm revoke**. While the request is in flight (slow network), click row Y.

**Expected:** the panel shows Y. X's revoke result only refreshes the list.
**Actual:** the effect loads Y. Then the X response arrives and `setCert(X)` replaces it, so the panel shows X's serial and status while row Y is highlighted. The panel and the selection now disagree. `revoking` also stays `true` across the switch, so Y's "Confirm revoke" button stays disabled until X's request settles. No wrong certificate is revoked, because `revoke` uses the `id` prop and the stale panel shows "revoked", so no Revoke button appears. The UI is misleading, not destructive.

Server-side double revoke is fine; see "Held up" below.

---

## R5: The schema backstop doesn't cover audit and operation rows

**Where:** `backend/app/db.py`
- `:71`: `bulk_operations.admin_id REFERENCES admins(id)`. The admin's tenant isn't tied to `tenant_id`.
- `:96`: `certificates.bulk_operation_id REFERENCES bulk_operations(id)`. The operation's tenant isn't tied.
- `:110`, `:113`, `:114`: `audit_events.actor_admin_id`, `certificate_id` and `bulk_operation_id` are single-column FKs. Nothing ties `audit_events.tenant_id` to the certificate's tenant.

**Trigger:** no API request triggers this today. Every write path passes the actor's `tenant_id` correctly. A future bug, for example passing the wrong `tenant_id` to `record_audit_event`, could store an East certificate's audit event under West, or Bob as the actor on an East event, and nothing would stop it.

**Expected (per `docs/invariants.md` A1):** "the composite FKs make cross-tenant rows impossible to store."
**Actual:** that holds for enrollments, certificates and the operation's course, but not for these columns. This is defense in depth only.

---

## R6: `learner_name` sort is case-sensitive

**Where:** `backend/app/queries.py:50`. It uses the default `BINARY` collation.

**Request:** `GET /api/certificates?sort=learner_name&dir=asc` (-H "$A"), with names `dst early` and `Émile Zola` present.
**Actual:** `Alex Lee, Jordan Kim, Priya Shah, Sam Carter, dst early, dst late, Émile Zola, émile zola`. Lowercase and accented names sort after `Z`.
**Expected:** an admin would expect alphabetical order regardless of case. The contract says nothing about this.
**Note:** `docs/verify.md:249` deliberately compares by byte order to match SQLite. Changing the collation means changing that test, which needs your approval under AGENTS.md. I recommend leaving it and documenting it as a known limit.

---

## R7: The bulk issue response is unbounded

**Where:** `backend/app/services/bulk_issue.py:72-81`. One result row per enrollment, serialized into `response_json`, stored permanently and replayed.

**Request:** after `gen_large --count 100000`:
```
POST /api/bulk-issues  Idempotency-Key: big-course-0001  {"course_id": <Load Test 101>}   (-H "$A")
```
**Actual:** 200, with a **13 MB** body (100,000 `skipped/already_certified` rows) and the same 13 MB stored in `bulk_operations`. The UI renders a 100k-row table (`App.jsx:487`).
**Expected:** this is contract-compliant, so it isn't a correctness bug. Flagging it as a scale risk. It took 0.17 s here, so the write lock isn't at risk at this size.

---

## R8: The idempotency key is per tenant, not per admin

**Where:** `backend/app/services/bulk_issue.py:22-24`. The hash covers only the body. `:44-47` looks up by `(tenant_id, key)`.

**Effect:** with today's seed (one admin per tenant) this has no impact. If a tenant had two admins, admin 2 reusing admin 1's key with the same body would get admin 1's result replayed (`operation_id` included) instead of a 409. The audit trail would still name admin 1.
**Tenant isolation holds.** Bob and Alice using the same key string (`bob-key-00001`) produce two independent operations, and Bob reusing his own key with East's course id gets 409, not 404, which reveals nothing about East. Consistent with `UNIQUE(tenant_id, idempotency_key)` in the data model.

---

## R9: CSV has no BOM

**Where:** `backend/app/routes/export.py:52`. `text/csv; charset=utf-8` with no BOM.
**Effect:** if you double-click the file in Excel, `Émile Zola` shows as `Ã‰mile Zola`. Sheets and LibreOffice are fine. UX only.

## R10: `/api/actors` hands out every token

**Where:** `backend/app/routes/meta.py:11-13`.
**Request:** `GET /api/actors` with no header returns `east-alice-token` and `west-bob-token`.
**Note:** this is the documented demo picker (api-contract) and in scope under "no production auth". Listing it so nobody claims isolation is stronger than "the caller has to pick the right token".

---

## Held up under attack (no bug found)

| Probe | Result |
|---|---|
| Bob: `GET /api/certificates/<East id>` vs a missing id | Both 404 with an identical body |
| Bob: `POST /api/certificates/<East id>/revoke` | 404, row still `active` |
| Bob: `POST /api/bulk-issues {"course_id": 1}` (East) with a new key | 404, the same body as a nonexistent course, no operation row |
| Bob: his own key reused with East's course id | 409, nothing written |
| Alice and Bob using the same key string | Independent operations |
| 20 concurrent same-key, same-body requests | All 200, byte-identical bodies, 1 operation row |
| 20 concurrent same-key requests alternating two courses | One 200, the rest 409, exactly 1 operation row |
| 20 concurrent revokes of one certificate | All 200, identical `revoked_at_utc`, exactly 1 `certificate.revoked` event |
| List (paged, `page_size=3`) vs CSV order: 5 sorts × 2 dirs × `q` ∈ {"", "d", "cert-"} | Identical serial sequences |
| Client aborts a 100k export partway through | Generator closed: `wal_checkpoint(TRUNCATE)` completes right away, so no snapshot is pinned |
| 100k export, `q=j&sort=learner_name` | 200 in under 1 s |
| `q` containing `%` or `_` | Escaped by `_like_prefix` (`queries.py:59-60`); code-read only |
| A5 values | East `2026-01-11 00:30`, West `2026-01-10 21:30`, from the stored columns. The UI renders the stored strings (`App.jsx:229`, `:322`) |

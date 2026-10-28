# Acceptance Tests — A1 to A7

There is one test per scenario in `docs/brief.md` §4. Request shapes follow `docs/api-contract.md`. Each assertion is followed by the brief sentence it comes from. Quotes are word-for-word; only the PDF's line breaks and stray spacing have been removed.

## Common harness

- **Fresh database per test.** Each test gets its own temporary SQLite file, created by `seed.py`. No test depends on another.
- **Live server.** Requests go over real HTTP to `uvicorn` running on the temporary DB. A2 and A6 need real sockets and threads.
- **Database checks.** The test opens its own read-only `sqlite3` connection to the same file. "Certificate count" means `SELECT COUNT(*) FROM certificates` (optionally filtered). "Audit count" means `SELECT COUNT(*) FROM audit_events` (optionally filtered by `action` and `certificate_id`).
- **Actors.** Alice sends `X-Actor-Token: east-alice-token`. Bob sends `X-Actor-Token: west-bob-token`.
- **Seed lookups.** `EAST_SAFETY` and `WEST_SAFETY` are the ids of the Safety 101 course in each tenant. `EAST_CERT_A5` and `WEST_CERT_A5` are the seeded certificates issued at `2026-01-10T13:30:00Z`. All are looked up in the DB, not hard-coded.
---

## A1 — Tenant isolation

**Setup**
1. Fresh seed.
2. From the DB, record:
   - `EAST_IDS`: the set of East certificate ids
   - `EAST_SERIALS`: the set of East serials
   - `S_E`: one East serial
   - `X_E`: one East certificate id
   - `MISSING_ID`: `MAX(certificates.id) + 1000`, an id that doesn't exist
3. From the DB, record `C0` (certificate count) and `A0` (audit count) across all tenants.

**Requests (all as Bob)**
1. `GET /api/certificates?page_size=100`
2. `GET /api/certificates?q=<S_E>`
3. `GET /api/certificates/<X_E>`
4. `GET /api/certificates/<MISSING_ID>`
5. `POST /api/bulk-issues` with header `Idempotency-Key: a1-bob-cross-tenant-0001` and body `{"course_id": <EAST_SAFETY>}`
6. `GET /api/certificates/export.csv`
7. `GET /api/certificates/export.csv?q=<S_E>`

**Assertions**
1. Every item in response 1 has an id whose DB row has West's `tenant_id`, and `total` equals the DB count of West certificates.
   > "Only West data is accessible."
2. Response 2 has `total = 0` and no items.
   > "Only West data is accessible."
3. Response 3 has status 404.
   > "A cross-tenant detail request returns 404 or an equivalent non-revealing response."
4. Response 3's body is identical to response 4's body (cross-tenant and nonexistent ids can't be told apart).
   > "A cross-tenant detail request returns 404 or an equivalent non-revealing response."
5. Response 5 has a 4xx status and is not a `BulkResult`.
   > "Cross-tenant bulk issue is rejected and creates no certificates or audit events."
6. After request 5, the DB certificate count still equals `C0`.
   > "Cross-tenant bulk issue is rejected and creates no certificates or audit events."
7. After request 5, the DB audit count still equals `A0`.
   > "Cross-tenant bulk issue is rejected and creates no certificates or audit events."
8. No serial in the CSV from response 6 is in `EAST_SERIALS`, and its row count equals the DB count of West certificates.
   > "Only West data is accessible."
9. The CSV from response 7 has only the header row.
   > "Only West data is accessible."

---

## A2 — Retry safety (sequential and concurrent)

The test has two phases. Each runs on its own fresh seed, because the first phase issues every eligible certificate.

**Setup (each phase)**
1. Fresh seed.
2. From the DB, record:
   - `ELIGIBLE`: learners with a `completed` enrollment in `EAST_SAFETY` and no certificate for it
   - `C0` (certificate count) and `A0` (count of `certificate.issued` audit events)
3. Check the precondition: `|ELIGIBLE| ≥ 1`.

### Phase 1 — Sequential

**Requests (as Alice)**
1. `POST /api/bulk-issues` with `Idempotency-Key: a2-seq-key-0001` and body `{"course_id": <EAST_SAFETY>}`. Call the response `R1`.
2. The same request again, with the same key and body. Call the response `R2`.

**Assertions**
1. `R1` and `R2` have the same status, and their bodies are identical, including `operation_id`.
   > "All callers receive the outcome of the same logical operation, without additional certificates or audit events."
2. The DB has exactly 1 `bulk_operations` row for key `a2-seq-key-0001`.
   > "One logical operation."
3. The DB certificate count equals `C0 + |ELIGIBLE|`.
   > "All callers receive the outcome of the same logical operation, without additional certificates or audit events."
4. For every learner in `ELIGIBLE`, the DB has exactly 1 certificate for `EAST_SAFETY`.
   > "Each eligible learner receives at most one certificate."
5. The DB count of `certificate.issued` audit events equals `A0 + |ELIGIBLE|`.
   > "Each actual issuance has exactly one audit event."
6. Every certificate created in this phase has exactly 1 `certificate.issued` audit event.
   > "Each actual issuance has exactly one audit event."

### Phase 2 — Truly concurrent

**Requests (as Alice)**
1. Open 10 independent HTTP connections, one per thread.
2. Each thread waits on a shared `threading.Barrier(10)`. When the barrier releases, each sends `POST /api/bulk-issues` with `Idempotency-Key: a2-conc-key-0001` and body `{"course_id": <EAST_SAFETY>}`.
3. Each thread records its send time `t_send` and its response time `t_recv`.

**Assertions**
1. **The requests really overlapped:** the latest `t_send` is earlier than the earliest `t_recv`. Every request was in flight before any response came back. If this check fails, the test is invalid and must be rerun. It does not count as a pass.
   > "Alice sends the same bulk request with the same idempotency key sequentially and concurrently."
2. All 10 responses have the same success status and bodies that are byte-identical, including `operation_id`. None is a 409, a 5xx or a "busy" response.
   > "All callers receive the outcome of the same logical operation, without additional certificates or audit events."
3. The DB has exactly 1 `bulk_operations` row for key `a2-conc-key-0001`.
   > "One logical operation."
4. The DB certificate count equals `C0 + |ELIGIBLE|`.
   > "All callers receive the outcome of the same logical operation, without additional certificates or audit events."
5. For every learner in `ELIGIBLE`, the DB has exactly 1 certificate for `EAST_SAFETY`.
   > "Each eligible learner receives at most one certificate."
6. The DB count of `certificate.issued` audit events equals `A0 + |ELIGIBLE|`, and each new certificate has exactly 1 such event.
   > "Each actual issuance has exactly one audit event."
7. An 11th, sequential request with the same key and body returns a body identical to the concurrent ones, and the certificate and audit counts don't change.
   > "All callers receive the outcome of the same logical operation, without additional certificates or audit events."

---

## A3 — Key misuse

**Setup**
1. Fresh seed.
2. Look up `EAST_OTHER`, a second East course that has at least one `completed` enrollment without a certificate. The seed must provide this course.

**Requests (as Alice)**
1. `POST /api/bulk-issues` with `Idempotency-Key: a3-misuse-key-0001` and body `{"course_id": <EAST_SAFETY>}`. Call the response `R1`.
2. From the DB, record:
   - `C1`: certificate count
   - `A1`: audit count
   - `B1`: `bulk_operations` row count
   - `J1`: the stored `response_json` for this key
   - `K1`: the certificate count for `EAST_OTHER`
3. `POST /api/bulk-issues` with the same key and body `{"course_id": <EAST_OTHER>}`. Call the response `R2`.
4. `POST /api/bulk-issues` with the same key and the original body `{"course_id": <EAST_SAFETY>}`. Call the response `R3`.

**Assertions**
1. `R2` has status 409.
   > "Return 409."
2. After `R2`:
   - the DB certificate count equals `C1`
   - the certificate count for `EAST_OTHER` equals `K1`
   - the audit count equals `A1`
   > "Do not execute a second operation or overwrite the first result."
3. After `R2`, the `bulk_operations` row count equals `B1`.
   > "Do not execute a second operation or overwrite the first result."
4. After `R2`, the stored `response_json` for the key is byte-identical to `J1`, and its `course_id` is still `EAST_SAFETY`.
   > "Do not execute a second operation or overwrite the first result."
5. `R3`'s body is identical to `R1`'s body.
   > "Do not execute a second operation or overwrite the first result."

---

## A4 — Learner outcomes

**Setup**
1. Fresh seed.
2. From the DB, partition the learners enrolled in `EAST_SAFETY` into:
   - `COMPLETED_NEW`: `completed` with no certificate
   - `INCOMPLETE`: `incomplete`
   - `ALREADY`: has a certificate for the course; also record that certificate's id for each learner
3. Check the precondition: each group is non-empty.
4. From the DB, record `C0` (certificate count) and `A0` (count of `certificate.issued` audit events).

**Requests (as Alice)**
1. `POST /api/bulk-issues` with `Idempotency-Key: a4-outcomes-key-0001` and body `{"course_id": <EAST_SAFETY>}`. Call the response `R`.

**Assertions**
1. `R.results` has exactly one entry per learner enrolled in `EAST_SAFETY`, and each entry's `outcome` is one of `issued`, `skipped` or `failed`.
   > "Return per-learner outcomes: issued, skipped, or failed."
2. Every learner in `COMPLETED_NEW` has `outcome = issued` and a non-null `certificate_id`, and that certificate exists in the DB for that learner and course.
   > "Certificates are issued only to eligible learners."
3. Every learner in `INCOMPLETE` has `outcome = skipped` and `reason = not_completed`, and the DB has no certificate for that learner and course.
   > "Incomplete and duplicate cases are skipped with reasons."
4. Every learner in `ALREADY` has `outcome = skipped` and `reason = already_certified`, and the DB still has exactly 1 certificate for that learner and course, with the same id as before.
   > "Incomplete and duplicate cases are skipped with reasons."
5. The DB certificate count equals `C0 + |COMPLETED_NEW|`.
   > "Certificates are issued only to eligible learners."
6. The DB count of `certificate.issued` audit events equals `A0 + |COMPLETED_NEW|`, and each newly issued certificate has exactly 1 such event.
   > "Audit both issuance and revocation."

---

## A5 — DST and local date

**Setup**
1. Fresh seed. This gives `EAST_CERT_A5` and `WEST_CERT_A5`, both with `issued_at_utc = 2026-01-10T13:30:00Z`.
2. Through the shared issue helper, add one more East certificate, `EAST_CERT_LATE`, with `issued_at_utc = 2026-01-10T12:30:00Z`. Its Sydney local time is `2026-01-10 23:30`.
   - Its UTC date is the same as `EAST_CERT_A5`'s, but its local date is different.
   - It is one hour earlier in UTC, so in ascending order it should come before `EAST_CERT_A5`.
3. Check the precondition: in the DB, both seeded A5 certificates have `issued_at_utc = 2026-01-10T13:30:00Z`.

**Requests**
1. As Alice: `GET /api/certificates/<EAST_CERT_A5>`
2. As Bob: `GET /api/certificates/<WEST_CERT_A5>`
3. As Alice: `GET /api/certificates?sort=issued_date&dir=asc&page_size=100&page=1`, then the following pages until every row has been fetched.
4. As Alice: `GET /api/certificates/export.csv?sort=issued_date&dir=asc`
5. As Bob: `GET /api/certificates/export.csv?sort=issued_date&dir=asc`

**Assertions**
1. Response 1 has `issued_local_time = "2026-01-11 00:30"` and `issued_local_date = "2026-01-11"`. This is Sydney's daylight-saving offset of +11; the standard offset of +10 would give `2026-01-10 23:30`.
   > "East displays 2026-01-11 00:30."
2. Response 2 has `issued_local_time = "2026-01-10 21:30"` and `issued_local_date = "2026-01-10"`.
   > "West displays 2026-01-10 21:30."
3. In the ascending list from request 3:
   - `EAST_CERT_LATE` shows `issued_local_date = "2026-01-10"`
   - `EAST_CERT_A5` shows `issued_local_date = "2026-01-11"`
   - `EAST_CERT_LATE` comes before `EAST_CERT_A5`
   - the `issued_local_date` values never decrease from row to row
   > "List sorting and CSV use the tenant-local calendar date."
4. In Alice's CSV:
   - the row with `EAST_CERT_A5`'s serial has issue date `2026-01-11`
   - the row for `EAST_CERT_LATE` has `2026-01-10`
   - the issue-date column never decreases
   > "List sorting and CSV use the tenant-local calendar date."
5. In Bob's CSV, the row with `WEST_CERT_A5`'s serial has issue date `2026-01-10`.
   > "List sorting and CSV use the tenant-local calendar date."
6. The CSV header names exactly the four columns: learner name, course title, serial, and issue date.
   > "Export learner name, course title, serial number, and tenant-local issue date."

*Note on request 3:* the test reads the whole ascending list, using `page_size=100` and paging until all rows are fetched. It does not depend on a search term, so the result doesn't rely on how search is implemented.

---

## A6 — Export growth

**Setup**
1. Fresh seed.
2. Run `python -m backend.scripts.gen_large --tenant East --count 100000` against the temporary DB.
3. Choose the parameters:
   - `Q`: a learner-name prefix produced by the generator. The DB count of matching East rows should be at least 10,000 and fewer than all East rows, so the search really filters.
   - `SORT=learner_name`, `DIR=asc`
4. From the DB, record:
   - `N`: the number of East certificates matching `Q` (`lower(learner_name)` starts with `lower(Q)`, or `serial` starts with `Q`)
   - `SERIALS_DB`: the set of their serials
5. Check the preconditions:
   - in the DB, the count of East certificates is at least 100,000 more than before generation
   - at least one `learner_name` appears 2 or more times among the `N` matching rows (equal sort keys exist)

**Requests (as Alice)**
1. **Export 1:**
   1. Start `GET /api/certificates/export.csv?q=<Q>&sort=learner_name&dir=asc` on thread T1. Record its start time, and stream the body to a buffer.
   2. Once T1 has received its first chunk and before its last chunk, send `GET /api/certificates?q=<Q>&sort=learner_name&dir=asc&page=1&page_size=20` from the main thread. Record when its response arrives (`t_list`).
   3. T1 records its end time (`t_export1_end`).
2. **Export 2:** after export 1 finishes, run the same export request again.

**Assertions**
1. **The list was requested mid-export:** `t_list` is earlier than `t_export1_end`. If this check fails, the test is invalid and must be rerun. It does not count as a pass.
   > "During the first export, request the certificate list."
2. The list request has status 200 and a valid page body.
   > "The certificate list request succeeds while the export is running."
3. Each export took less than 60 seconds from request to last byte.
   > "Each export completes within 60 seconds."
4. Export 1 has exactly `N` data rows, and its set of serials equals `SERIALS_DB`. There are no extra rows (including no West rows), no missing rows, and no duplicates.
   > "The CSV contains exactly the matching East rows in the requested order."
5. In export 1, `learner_name` never decreases from row to row, compared by byte order to match SQLite's default collation.
   > "The CSV contains exactly the matching East rows in the requested order."
6. Export 2 has the same serial sequence as export 1. In particular, within every run of equal `learner_name`, the serials appear in the same order in both.
   > "Rows with equal sort keys appear in the same relative order on repeated exports."
7. The 20 serials on list page 1 equal the first 20 serials in export 1, in the same order.
   > "Preserve current search and sort."

---

## A7 — Revocation audit

**Setup**
1. Fresh seed.
2. From the DB, pick `X`, an East certificate with `status = 'active'`.
3. From the DB, record `A0` (total audit count) and `R0`, the count of `certificate.revoked` events for `X`. The precondition is `R0 = 0`.

**Requests (as Alice)**
1. `POST /api/certificates/<X>/revoke`. Call the response `R1`.
2. From the DB, record `A1` (total audit count).
3. `POST /api/certificates/<X>/revoke` again. Call the response `R2`.

**Assertions**
1. `R1` is successful, and its body shows `status = "revoked"`.
   > "The first request changes status and writes one revocation audit event."
2. After `R1`, the DB row for `X` has `status = 'revoked'`.
   > "The first request changes status and writes one revocation audit event."
3. After `R1`:
   - the DB has exactly 1 `certificate.revoked` audit event for `X`, with Alice as the actor
   - `A1 = A0 + 1`
   > "The first request changes status and writes one revocation audit event."
4. After `R2`:
   - the DB still has exactly 1 `certificate.revoked` audit event for `X`
   - the total audit count still equals `A1`
   > "The repeat does not create duplicate audit events."
5. After `R2`, the DB row for `X` still has `status = 'revoked'`.
   > "Alice revokes a valid certificate, then repeats the request."

---


## Accepted extras

- **401 for a missing or unknown `X-Actor-Token` on every endpoint.** The contract requires it, and without it the A1 checks have no unauthenticated baseline.
- **Bob revokes an East certificate → 404, and the status and audit count don't change.** Revoke is an East-scoped operation, but the brief's A1 list doesn't name it.
- **A rejected cross-tenant bulk request creates no `bulk_operations` row, and Alice can later use the same key.** This proves a rejected request doesn't use up the key. The brief only mentions certificates and audit events.
- **Bob and Alice using the same idempotency key run independently.** The keys are scoped per tenant (D2), and a collision would silently replay the other tenant's result.
- **Concurrent requests with the same key but different courses → exactly one succeeds and the others get 409.** This combines A2 and A3 under a race, which the brief tests separately.
- **A3 with a changed payload but the same `course_id`.** The brief says "or request payload", but the contract's body only has `course_id`. Extra fields get rejected.
- **A repeat revoke returns 200 with the current state, and `revoked_at_utc` doesn't change.** The contract decides this (D7). The brief doesn't specify the response.
- **Two concurrent revokes of the same certificate → one audit event.** The A7 mechanism should hold under a race, but the brief only tests repeats one after another.
- **Bulk issue for a learner whose certificate was revoked → `skipped / already_certified`.** This tests the D3 decision, which the brief doesn't cover.
- **Local date near the Sydney daylight-saving changeover (2026-04-05) and at Perth midnight.** A5's example only covers the daylight-saving offset (+11), not the changeover day itself.
- **Exporting a search with no matches → header only.** An edge case for the "exactly the matching rows" rule.
- **A6 repeated with `sort=issued_date` and `dir=desc`.** This covers ties on a second sort key and the reverse direction.
- **A `failed` per-learner outcome when a single insert fails.** The brief lists `failed` as an outcome but has no scenario that produces it.
- **`gen_large` doesn't change the quick-start dataset.** The brief requires this, but outside A1–A7: compare counts for the non-Load-Test rows before and after.


## Proposed extras

These are not acceptance tests: the brief does not require them. Each is listed with the reason it might be worth adding.

- **UI checks: duplicate submits blocked, loading/empty/error states, and confirmation before revoking.** These are required product behaviour in brief §3, outside A1–A7.

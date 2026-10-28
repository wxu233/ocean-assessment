**Actor header:** every `/api/*` route except `GET /api/actors` requires `X-Actor-Token: <token>`.

**Status codes:**
- 401: the token is missing or unknown.
- 404: a resource doesn't exist *or* belongs to another tenant. Both cases return an identical body.
- 409: idempotency key misuse.
- 400: the `Idempotency-Key` header is missing or invalid.
- 422: any other input is invalid.

**Error body:** `{"error": {"code": "<snake_case>", "message": "<human text>"}}`

| Method & path | Request | Response |
|---|---|---|
| `GET /api/actors` | — | `[{email, tenant_name, token}]`, demo actor picker only |
| `GET /api/me` | — | `{email, tenant_name, timezone}` |
| `GET /api/courses` | — | `[{id, title}]` for the actor's tenant |
| `GET /api/certificates` | `q`, `sort`, `dir`, `page` (from 1), `page_size` (default 20, max 100) | `{items: [CertSummary], total, page, page_size}` |
| `GET /api/certificates/{id}` | — | `CertDetail` |
| `POST /api/certificates/{id}/revoke` | — | `CertDetail` (200 on the first call and on repeats) |
| `POST /api/bulk-issues` | header `Idempotency-Key`; body `{course_id}` | `BulkResult` (200; a replay returns the identical body) |
| `GET /api/certificates/export.csv` | same `q`, `sort`, `dir` as the list; no paging | `text/csv`, streamed |

**Types:**
- `CertSummary`: `{id, serial, learner_name, course_title, issued_local_date, issued_local_time, status}`
- `CertDetail`: `CertSummary` plus `{issued_at_utc, revoked_at_utc}`
- `BulkResult`: `{operation_id, course_id, summary: {issued, skipped, failed}, results: [{learner_id, learner_name, outcome: "issued"|"skipped"|"failed", reason: null|"not_completed"|"already_certified"|"<error>", certificate_id: int|null}]}`

**List and export parameters:**
- `q` matches `learner_name` by prefix, ignoring case, **or** `serial` exactly or by prefix.
- `sort` is one of `issued_date | learner_name | course_title | serial | status`. Default: `issued_date`.
- `dir` is `asc | desc`. Default: `desc`.
- Rows are always tiebroken by `id ASC`.
- `issued_date` sorts on `issued_local_date, issued_local_time`.

**CSV:**
- Header row: `learner_name,course_title,serial,issued_local_date`
- Rows come in exactly the list order for the same `q`, `sort` and `dir`.

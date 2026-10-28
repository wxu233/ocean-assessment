# Certificates admin console

Internal admin console for a multi-tenant training platform: list, search, sort and page certificates; bulk-issue for a course; view and revoke; export CSV. FastAPI + SQLite backend, React (Vite) frontend, served as one process on :8000.

## Start

Requires Python 3.12 and Node.js ≥ 18 (with npm). `start.sh` checks both and exits with a message if either is missing.

```sh
./start.sh          # install deps, build the UI, seed demo data if missing, serve http://localhost:8000
./start.sh --reset  # same, but recreate the demo data first
./verify.sh         # acceptance tests (docs/verify.md) on a throwaway DB; 21 tests
./verify.sh -m slow # the 100k-row A6 export test
```

The database is `data/app.db` (override with `APP_DB_PATH`).

## Actors

There is no login. Pick the actor in the UI's "Acting as" menu; API calls send the header `X-Actor-Token`.

| Tenant | Timezone | Admin | Token |
|---|---|---|---|
| East | Australia/Sydney | alice@east.test | `east-alice-token` |
| West | Australia/Perth | bob@west.test | `west-bob-token` |

```sh
curl -H 'X-Actor-Token: east-alice-token' localhost:8000/api/certificates
```

## Reproducing A1–A7

Each scenario has an automated test in `backend/tests/test_acceptance.py` (`.venv/bin/python -m pytest -k <name>`). Manual steps, after `./start.sh --reset`:

| | Scenario | Test | Manual steps |
|---|---|---|---|
| A1 | Tenant isolation | `test_a1_tenant_isolation` | Act as bob@west.test: the list and CSV show only West rows; searching an East serial finds nothing. `curl -H 'X-Actor-Token: west-bob-token' localhost:8000/api/certificates/<east id>` returns the same 404 body as a nonexistent id. |
| A2 | Retry safety | `test_a2_retry_safety` | Send the same `POST /api/bulk-issues` (same `Idempotency-Key`, same body) twice: identical bodies, one operation, no extra certificates. In the UI, a failed submission shows **Retry**, which resends the same key. |
| A3 | Key misuse | `test_a3_key_misuse` | Reuse a key with a different `course_id`: 409 `idempotency_key_reused`, nothing written. |
| A4 | Learner outcomes | `test_a4_learner_outcomes` | As Alice, **Bulk issue** → Safety 101 → confirm. The results table shows issued, `skipped/not_completed` and `skipped/already_certified` learners. |
| A5 | Local dates | `test_a5_dst_and_local_date` | The certificate issued at `2026-01-10T13:30:00Z` shows `2026-01-11 00:30` for Alice and `2026-01-10 21:30` for Bob, whatever the browser's timezone. |
| A6 | Export growth | `test_a6_export_growth_1k`, `test_a6_export_growth_100k` (slow) | `.venv/bin/python -m backend.scripts.gen_large --tenant East --count 100000`, then **Export CSV** as Alice (or `curl -H 'X-Actor-Token: east-alice-token' localhost:8000/api/certificates/export.csv -o out.csv`) twice; the list keeps loading while an export runs. The export uses the current search and sort. |
| A7 | Revocation audit | `test_a7_revocation_audit` | As Alice, open an active certificate → **Revoke…** → **Confirm revoke**. Status becomes revoked; repeating the revoke via the API returns 200 with the same state and no second audit event. |

## 100k generation

```sh
.venv/bin/python -m backend.scripts.gen_large --tenant East --count 100000
```

Adds 100,000 East certificates under the course "Load Test 101", with repeated learner names and repeated local dates/times. Startup never adds them; `./start.sh --reset` removes them.

## Known defects and limits

- The UI's CSV download goes through `fetch` (the actor header can't be set on a plain link), so the browser holds the whole file in memory before saving it. The server still streams; a 100k export is a few MB.
- The UI has no automated tests; its behaviours (loading/empty/error states, disabled submit while in flight, revoke confirmation, export keeping search and sort) are checked by hand.
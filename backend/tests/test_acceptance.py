"""Acceptance tests for A1–A7 (docs/verify.md) plus the accepted extras.

Every test runs against a live uvicorn process on its own freshly seeded
SQLite file, and asserts database state through a separate connection.

Harness contract the app must satisfy:
- `APP_DB_PATH` selects the SQLite file for the server, `seed.py` and `gen_large.py`.
- `python -m backend.scripts.seed` creates the schema and demo data.
- `python -m backend.scripts.gen_large --tenant East --count N` adds N certificates.
- `backend.app.main:app` is the ASGI app.
- `backend.app.services.certificates.issue_certificate(conn, *, tenant_id, course_id,
  learner_id, issued_at_utc: datetime) -> int` inserts one certificate and returns its
  id. The caller owns the transaction.

Run:   pytest                  (default; the 100k A6 test is excluded)
       pytest -m slow          (the 100k A6 test only)
"""

import csv
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALICE = "east-alice-token"
BOB = "west-bob-token"
CSV_HEADER = ["learner_name", "course_title", "serial", "issued_local_date"]
A5_UTC = "2026-01-10T13:30:00Z"
SERVER_START_TIMEOUT = 20
CONCURRENCY_ATTEMPTS = 5
CONCURRENT_REQUESTS = 10


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@dataclass
class Resp:
    status: int
    body: bytes
    t_send: float = 0.0
    t_recv: float = 0.0

    def json(self):
        return json.loads(self.body)


class DB:
    """Direct access to the test's SQLite file, independent of app code."""

    def __init__(self, path):
        self.path = path

    def _connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def all(self, sql, *params):
        conn = self._connect()
        try:
            conn.execute("PRAGMA query_only=ON")
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def scalar(self, sql, *params):
        rows = self.all(sql, *params)
        return rows[0][0] if rows else None

    def write(self, sql, *params):
        """Test setup only: never used to fake what the app should do."""
        conn = self._connect()
        try:
            return conn.execute(sql, params).lastrowid
        finally:
            conn.close()

    def cert_count(self, where="1=1", *params):
        return self.scalar(f"SELECT COUNT(*) FROM certificates WHERE {where}", *params)

    def audit_count(self, where="1=1", *params):
        return self.scalar(f"SELECT COUNT(*) FROM audit_events WHERE {where}", *params)

    def bulk_op_count(self, where="1=1", *params):
        return self.scalar(f"SELECT COUNT(*) FROM bulk_operations WHERE {where}", *params)

    def cert_ids(self, where="1=1", *params):
        return {r[0] for r in self.all(f"SELECT id FROM certificates WHERE {where}", *params)}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppEnv:
    """A freshly seeded DB plus (once started) a live uvicorn serving it."""

    def __init__(self, workdir):
        self.dir = workdir
        self.db_path = workdir / "app.db"
        self.db = DB(self.db_path)
        self.env = {**os.environ, "APP_DB_PATH": str(self.db_path), "PYTHONPATH": str(ROOT)}
        self.proc = None
        self.port = None
        self._run_module("backend.scripts.seed")

    def _run_module(self, module, *args):
        r = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            pytest.fail(f"{module} exited {r.returncode}\n{r.stdout}\n{r.stderr}")

    def gen_large(self, count):
        self._run_module("backend.scripts.gen_large", "--tenant", "East", "--count", str(count))

    def start(self):
        self.port = _free_port()
        log_path = self.dir / "server.log"
        with open(log_path, "w") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app.main:app",
                 "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                if self.get("/api/actors").status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        self.stop()
        pytest.fail(f"server did not start:\n{log_path.read_text()[-4000:]}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    # -- HTTP ---------------------------------------------------------------

    def conn(self, timeout=120):
        return HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    @staticmethod
    def url(path, **params):
        return f"{path}?{urllib.parse.urlencode(params)}" if params else path

    def request(self, method, path, token=None, body=None, headers=None, conn=None):
        h = dict(headers or {})
        if token:
            h["X-Actor-Token"] = token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = conn or self.conn()
        try:
            t_send = time.perf_counter()
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            payload = r.read()
            return Resp(r.status, payload, t_send, time.perf_counter())
        finally:
            if conn is None:
                c.close()

    def get(self, path, token=None, **params):
        return self.request("GET", self.url(path, **params), token)

    def list(self, token, **params):
        return self.get("/api/certificates", token, **params)

    def list_all(self, token, **params):
        items, page = [], 1
        while True:
            r = self.list(token, page=page, page_size=100, **params)
            assert r.status == 200, r.body
            body = r.json()
            items += body["items"]
            if not body["items"] or len(items) >= body["total"]:
                return items
            page += 1

    def detail(self, token, cert_id):
        return self.get(f"/api/certificates/{cert_id}", token)

    def revoke(self, token, cert_id):
        return self.request("POST", f"/api/certificates/{cert_id}/revoke", token)

    def bulk(self, token, key, body):
        return self.request("POST", "/api/bulk-issues", token, body=body,
                            headers={"Idempotency-Key": key})

    def export(self, token, **params):
        return self.get("/api/certificates/export.csv", token, **params)


@pytest.fixture
def make_env(tmp_path_factory):
    envs = []

    def factory(start=True):
        env = AppEnv(tmp_path_factory.mktemp("app"))
        envs.append(env)
        if start:
            env.start()
        return env

    yield factory
    for env in envs:
        env.stop()


def fire_concurrently(env, calls):
    """Send `calls` from one thread each, all released together by a barrier.

    Each thread opens its connection before the barrier, so only the request
    itself remains after release. Returns (responses, overlapped), where
    overlapped means every request was sent before any response arrived.
    """
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)
    errors = []

    def worker(i):
        method, path, token, body, headers = calls[i]
        c = env.conn()
        try:
            c.connect()
            barrier.wait(timeout=30)
            results[i] = env.request(method, path, token, body, headers, conn=c)
        except BaseException as e:  # surface in the main thread
            errors.append(e)
            barrier.abort()
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    if errors:
        raise errors[0]
    overlapped = max(r.t_send for r in results) < min(r.t_recv for r in results)
    return results, overlapped


def run_overlapping(make_env, build):
    """Retry on a fresh env until the concurrent requests truly overlap.

    A run where they did not overlap is invalid, not a pass.
    """
    for _ in range(CONCURRENCY_ATTEMPTS):
        env = make_env()
        calls, state = build(env)
        results, overlapped = fire_concurrently(env, calls)
        if overlapped:
            return env, state, results
        env.stop()
    pytest.fail(f"requests never overlapped in {CONCURRENCY_ATTEMPTS} attempts; run is invalid")


# ---------------------------------------------------------------------------
# Seed lookups and data helpers
# ---------------------------------------------------------------------------

def seed_ids(db):
    ids = SimpleNamespace(
        east=db.scalar("SELECT id FROM tenants WHERE name='East'"),
        west=db.scalar("SELECT id FROM tenants WHERE name='West'"),
        alice=db.scalar("SELECT id FROM admins WHERE email='alice@east.test'"),
        bob=db.scalar("SELECT id FROM admins WHERE email='bob@west.test'"),
    )
    ids.east_safety = db.scalar(
        "SELECT id FROM courses WHERE tenant_id=? AND title='Safety 101'", ids.east)
    ids.west_safety = db.scalar(
        "SELECT id FROM courses WHERE tenant_id=? AND title='Safety 101'", ids.west)
    missing = [k for k, v in vars(ids).items() if v is None]
    assert not missing, f"seed is missing: {missing}"
    return ids


def a5_cert(db, tenant_id):
    rows = db.all("SELECT id FROM certificates WHERE tenant_id=? AND issued_at_utc=?",
                  tenant_id, A5_UTC)
    assert len(rows) == 1, f"seed must have exactly one certificate at {A5_UTC} per tenant"
    return rows[0][0]


def partition_enrollments(db, tenant_id, course_id):
    rows = db.all(
        """SELECT e.learner_id, e.status, c.id AS cert_id
           FROM enrollments e
           LEFT JOIN certificates c
             ON c.tenant_id = e.tenant_id AND c.course_id = e.course_id
            AND c.learner_id = e.learner_id
           WHERE e.tenant_id = ? AND e.course_id = ?""",
        tenant_id, course_id)
    return SimpleNamespace(
        enrolled={r["learner_id"] for r in rows},
        completed_new={r["learner_id"] for r in rows
                       if r["status"] == "completed" and r["cert_id"] is None},
        incomplete={r["learner_id"] for r in rows
                    if r["status"] == "incomplete" and r["cert_id"] is None},
        already={r["learner_id"]: r["cert_id"] for r in rows if r["cert_id"] is not None},
    )


def other_east_course(db, ids):
    """A second East course with at least one completed, uncertified learner."""
    for (course_id,) in db.all(
            "SELECT id FROM courses WHERE tenant_id=? AND id<>? AND title<>'Load Test 101' "
            "ORDER BY id", ids.east, ids.east_safety):
        if partition_enrollments(db, ids.east, course_id).completed_new:
            return course_id
    pytest.fail("seed must provide a second East course with a completed, uncertified learner")


def certs_for(db, tenant_id, course_id, learner_id):
    return db.all("SELECT id, status FROM certificates "
                  "WHERE tenant_id=? AND course_id=? AND learner_id=?",
                  tenant_id, course_id, learner_id)


def assert_one_issued_audit_each(db, cert_ids):
    for cert_id in cert_ids:
        n = db.audit_count("certificate_id=? AND action='certificate.issued'", cert_id)
        assert n == 1, f"certificate {cert_id} has {n} issuance audit events"


def add_completed_learners(db, tenant_id, course_title, names):
    """Test setup: a new course with completed, uncertified enrollments."""
    course_id = db.write("INSERT INTO courses(tenant_id, title) VALUES (?, ?)",
                         tenant_id, course_title)
    learner_ids = []
    for name in names:
        learner_id = db.write("INSERT INTO learners(tenant_id, name) VALUES (?, ?)",
                              tenant_id, name)
        db.write("INSERT INTO enrollments(tenant_id, course_id, learner_id, status, completed_at) "
                 "VALUES (?, ?, ?, 'completed', '2026-01-01T00:00:00Z')",
                 tenant_id, course_id, learner_id)
        learner_ids.append(learner_id)
    return course_id, learner_ids


def issue_via_helper(db_path, *, tenant_id, course_id, learner_id, issued_at_utc):
    """Insert a certificate through the app's single issue helper."""
    from backend.app.services.certificates import issue_certificate

    conn = sqlite3.connect(db_path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cert_id = issue_certificate(
            conn, tenant_id=tenant_id, course_id=course_id, learner_id=learner_id,
            issued_at_utc=datetime.fromisoformat(issued_at_utc))
        conn.execute("COMMIT")
        return cert_id
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def parse_csv(resp):
    assert resp.status == 200, resp.body[:500]
    rows = list(csv.reader(io.StringIO(resp.body.decode("utf-8-sig"))))
    assert rows, "CSV is empty (no header row)"
    header, data = rows[0], rows[1:]
    return header, [dict(zip(header, r)) for r in data]


def is_bulk_result(resp):
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "results" in body


def results_by_learner(resp):
    return {r["learner_id"]: r for r in resp.json()["results"]}


# ---------------------------------------------------------------------------
# A1 — Tenant isolation
# ---------------------------------------------------------------------------

def test_a1_tenant_isolation(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    east_serials = {r[0] for r in db.all("SELECT serial FROM certificates WHERE tenant_id=?",
                                         ids.east)}
    assert east_serials, "seed must contain East certificates"
    s_e = sorted(east_serials)[0]
    x_e = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=?", ids.east)
    missing_id = db.scalar("SELECT COALESCE(MAX(id), 0) FROM certificates") + 1000
    c0, a0 = db.cert_count(), db.audit_count()
    west_total = db.cert_count("tenant_id=?", ids.west)

    r1 = env.list(BOB, page_size=100)
    r2 = env.list(BOB, q=s_e)
    r3 = env.detail(BOB, x_e)
    r4 = env.detail(BOB, missing_id)
    r5 = env.bulk(BOB, "a1-bob-cross-tenant-0001", {"course_id": ids.east_safety})
    c_after_bulk, a_after_bulk = db.cert_count(), db.audit_count()
    r6 = env.export(BOB)
    r7 = env.export(BOB, q=s_e)

    # "Only West data is accessible."
    assert r1.status == 200, r1.body
    for item in r1.json()["items"]:
        assert db.scalar("SELECT tenant_id FROM certificates WHERE id=?", item["id"]) == ids.west
    assert r1.json()["total"] == west_total
    # "Only West data is accessible."
    assert r2.status == 200, r2.body
    assert r2.json()["total"] == 0 and r2.json()["items"] == []
    # "A cross-tenant detail request returns 404 or an equivalent non-revealing response."
    assert r3.status == 404
    assert r3.body == r4.body
    # "Cross-tenant bulk issue is rejected and creates no certificates or audit events."
    assert 400 <= r5.status < 500, r5.status
    assert not is_bulk_result(r5)
    assert c_after_bulk == c0
    assert a_after_bulk == a0
    # "Only West data is accessible."
    header6, rows6 = parse_csv(r6)
    assert not ({row["serial"] for row in rows6} & east_serials)
    assert len(rows6) == west_total
    # "Only West data is accessible."
    _, rows7 = parse_csv(r7)
    assert rows7 == []


# ---------------------------------------------------------------------------
# A2 — Retry safety (sequential and truly concurrent)
# ---------------------------------------------------------------------------

def _a2_state(env):
    db = env.db
    ids = seed_ids(db)
    eligible = partition_enrollments(db, ids.east, ids.east_safety).completed_new
    assert eligible, "precondition: Safety 101 (East) has an eligible learner"
    return SimpleNamespace(
        ids=ids, eligible=eligible, before_ids=db.cert_ids(),
        c0=db.cert_count(), a0=db.audit_count("action='certificate.issued'"))


def _assert_a2_db(db, st, key):
    # "One logical operation."
    assert db.bulk_op_count("tenant_id=? AND idempotency_key=?", st.ids.east, key) == 1
    # "All callers receive the outcome of the same logical operation, without additional
    #  certificates or audit events."
    assert db.cert_count() == st.c0 + len(st.eligible)
    # "Each eligible learner receives at most one certificate."
    for learner_id in st.eligible:
        assert len(certs_for(db, st.ids.east, st.ids.east_safety, learner_id)) == 1
    # "Each actual issuance has exactly one audit event."
    assert db.audit_count("action='certificate.issued'") == st.a0 + len(st.eligible)
    assert_one_issued_audit_each(db, db.cert_ids() - st.before_ids)


def test_a2_retry_safety(make_env):
    # Phase 1: sequential.
    env = make_env()
    st = _a2_state(env)
    key = "a2-seq-key-0001"
    r1 = env.bulk(ALICE, key, {"course_id": st.ids.east_safety})
    r2 = env.bulk(ALICE, key, {"course_id": st.ids.east_safety})
    # "All callers receive the outcome of the same logical operation, without additional
    #  certificates or audit events."
    assert r1.status == 200, r1.body
    assert r2.status == r1.status
    assert r2.json() == r1.json()
    _assert_a2_db(env.db, st, key)
    env.stop()

    # Phase 2: truly concurrent, on a fresh seed.
    key = "a2-conc-key-0001"

    def build(env):
        st = _a2_state(env)
        call = ("POST", "/api/bulk-issues", ALICE, {"course_id": st.ids.east_safety},
                {"Idempotency-Key": key})
        return [call] * CONCURRENT_REQUESTS, st

    # "Alice sends the same bulk request with the same idempotency key sequentially and
    #  concurrently." (run_overlapping only returns runs where every request was in flight
    #  before the first response.)
    env, st, results = run_overlapping(make_env, build)
    # "All callers receive the outcome of the same logical operation, without additional
    #  certificates or audit events."
    assert [r.status for r in results] == [200] * CONCURRENT_REQUESTS, \
        [(r.status, r.body[:200]) for r in results]
    assert len({r.body for r in results}) == 1, "concurrent bodies are not byte-identical"
    _assert_a2_db(env.db, st, key)

    # An 11th, sequential retry replays the same outcome and changes nothing.
    c1, a1 = env.db.cert_count(), env.db.audit_count()
    r11 = env.bulk(ALICE, key, {"course_id": st.ids.east_safety})
    assert r11.status == 200
    assert r11.body == results[0].body
    assert env.db.cert_count() == c1 and env.db.audit_count() == a1


# ---------------------------------------------------------------------------
# A3 — Key misuse
# ---------------------------------------------------------------------------

def test_a3_key_misuse(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    east_other = other_east_course(db, ids)
    key = "a3-misuse-key-0001"

    r1 = env.bulk(ALICE, key, {"course_id": ids.east_safety})
    assert r1.status == 200, r1.body
    c1, a1, b1 = db.cert_count(), db.audit_count(), db.bulk_op_count()
    j1 = db.scalar("SELECT response_json FROM bulk_operations "
                   "WHERE tenant_id=? AND idempotency_key=?", ids.east, key)
    assert j1 is not None
    k1 = db.cert_count("course_id=?", east_other)

    r2 = env.bulk(ALICE, key, {"course_id": east_other})
    # "Return 409."
    assert r2.status == 409, r2.body
    # "Do not execute a second operation or overwrite the first result."
    assert db.cert_count() == c1
    assert db.cert_count("course_id=?", east_other) == k1
    assert db.audit_count() == a1
    assert db.bulk_op_count() == b1
    row = db.all("SELECT course_id, response_json FROM bulk_operations "
                 "WHERE tenant_id=? AND idempotency_key=?", ids.east, key)[0]
    assert row["response_json"] == j1
    assert row["course_id"] == ids.east_safety
    assert json.loads(row["response_json"])["course_id"] == ids.east_safety

    r3 = env.bulk(ALICE, key, {"course_id": ids.east_safety})
    # "Do not execute a second operation or overwrite the first result."
    assert r3.status == 200
    assert r3.json() == r1.json()


# ---------------------------------------------------------------------------
# A4 — Learner outcomes
# ---------------------------------------------------------------------------

def test_a4_learner_outcomes(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    part = partition_enrollments(db, ids.east, ids.east_safety)
    assert part.completed_new and part.incomplete and part.already, \
        "precondition: Safety 101 (East) has completed, incomplete and already-certified learners"
    c0 = db.cert_count()
    a0 = db.audit_count("action='certificate.issued'")
    before_ids = db.cert_ids()

    r = env.bulk(ALICE, "a4-outcomes-key-0001", {"course_id": ids.east_safety})
    assert r.status == 200, r.body
    results = r.json()["results"]
    by_learner = results_by_learner(r)

    # "Return per-learner outcomes: issued, skipped, or failed."
    assert len(results) == len(part.enrolled)
    assert set(by_learner) == part.enrolled
    assert all(x["outcome"] in ("issued", "skipped", "failed") for x in results)
    # "Certificates are issued only to eligible learners."
    for learner_id in part.completed_new:
        entry = by_learner[learner_id]
        assert entry["outcome"] == "issued", entry
        assert entry["certificate_id"] is not None
        certs = certs_for(db, ids.east, ids.east_safety, learner_id)
        assert [c["id"] for c in certs] == [entry["certificate_id"]]
    # "Incomplete and duplicate cases are skipped with reasons."
    for learner_id in part.incomplete:
        entry = by_learner[learner_id]
        assert (entry["outcome"], entry["reason"]) == ("skipped", "not_completed"), entry
        assert certs_for(db, ids.east, ids.east_safety, learner_id) == []
    # "Incomplete and duplicate cases are skipped with reasons."
    for learner_id, cert_id in part.already.items():
        entry = by_learner[learner_id]
        assert (entry["outcome"], entry["reason"]) == ("skipped", "already_certified"), entry
        certs = certs_for(db, ids.east, ids.east_safety, learner_id)
        assert [c["id"] for c in certs] == [cert_id]
    # "Certificates are issued only to eligible learners."
    assert db.cert_count() == c0 + len(part.completed_new)
    # "Audit both issuance and revocation."
    assert db.audit_count("action='certificate.issued'") == a0 + len(part.completed_new)
    assert_one_issued_audit_each(db, db.cert_ids() - before_ids)


# ---------------------------------------------------------------------------
# A5 — DST and local date
# ---------------------------------------------------------------------------

def test_a5_dst_and_local_date(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    east_a5 = a5_cert(db, ids.east)
    west_a5 = a5_cert(db, ids.west)
    course_id, [learner_id] = add_completed_learners(db, ids.east, "A5 Late Course",
                                                     ["A5 Late Learner"])
    east_late = issue_via_helper(env.db_path, tenant_id=ids.east, course_id=course_id,
                                 learner_id=learner_id, issued_at_utc="2026-01-10T12:30:00Z")
    serial = {cid: db.scalar("SELECT serial FROM certificates WHERE id=?", cid)
              for cid in (east_a5, west_a5, east_late)}

    r1 = env.detail(ALICE, east_a5)
    r2 = env.detail(BOB, west_a5)
    listed = env.list_all(ALICE, sort="issued_date", dir="asc")
    header_e, rows_e = parse_csv(env.export(ALICE, sort="issued_date", dir="asc"))
    _, rows_w = parse_csv(env.export(BOB, sort="issued_date", dir="asc"))

    # "East displays 2026-01-11 00:30."  (+11 daylight saving; +10 would give 01-10 23:30)
    assert r1.status == 200, r1.body
    assert r1.json()["issued_local_time"] == "2026-01-11 00:30"
    assert r1.json()["issued_local_date"] == "2026-01-11"
    # "West displays 2026-01-10 21:30."
    assert r2.status == 200, r2.body
    assert r2.json()["issued_local_time"] == "2026-01-10 21:30"
    assert r2.json()["issued_local_date"] == "2026-01-10"
    # "List sorting and CSV use the tenant-local calendar date."
    pos = {item["id"]: i for i, item in enumerate(listed)}
    assert listed[pos[east_late]]["issued_local_date"] == "2026-01-10"
    assert listed[pos[east_a5]]["issued_local_date"] == "2026-01-11"
    assert pos[east_late] < pos[east_a5]
    dates = [item["issued_local_date"] for item in listed]
    assert dates == sorted(dates)
    # "List sorting and CSV use the tenant-local calendar date."
    csv_e = {row["serial"]: row for row in rows_e}
    assert csv_e[serial[east_a5]]["issued_local_date"] == "2026-01-11"
    assert csv_e[serial[east_late]]["issued_local_date"] == "2026-01-10"
    csv_dates = [row["issued_local_date"] for row in rows_e]
    assert csv_dates == sorted(csv_dates)
    # "List sorting and CSV use the tenant-local calendar date."
    csv_w = {row["serial"]: row for row in rows_w}
    assert csv_w[serial[west_a5]]["issued_local_date"] == "2026-01-10"
    # "Export learner name, course title, serial number, and tenant-local issue date."
    assert header_e == CSV_HEADER


# ---------------------------------------------------------------------------
# A6 — Export growth
# ---------------------------------------------------------------------------

def choose_search_prefix(db, east_id, min_matches):
    """A short alphanumeric learner-name prefix that filters East rows to a large,
    strict subset, and that no serial starts with (so serial matching can't interfere)."""
    names = [r[0] for r in db.all("SELECT learner_name FROM certificates WHERE tenant_id=?",
                                  east_id)]
    serial_prefixes = {s[:n].lower() for (s,) in db.all("SELECT serial FROM certificates")
                       for n in range(1, 5)}
    total, best = len(names), None
    for length in range(1, 5):
        counts = Counter(n[:length].lower() for n in names if len(n) >= length)
        for prefix, count in counts.items():
            if not (prefix.isascii() and prefix.isalnum()) or prefix in serial_prefixes:
                continue
            if min_matches <= count < total:
                score = abs(count - total * 0.3)
                if best is None or score < best[0]:
                    best = (score, prefix)
    if best is None:
        pytest.fail(f"no learner-name prefix matches >= {min_matches} and < {total} East rows")
    return best[1]


def matching_east_rows(db, east_id, q):
    """Independent oracle for `q`: name prefix (case-insensitive) or serial prefix."""
    rows = db.all("SELECT id, serial, learner_name, issued_local_date, issued_local_time "
                  "FROM certificates WHERE tenant_id=?", east_id)
    ql = q.lower()
    return [r for r in rows
            if r["learner_name"][:len(q)].lower() == ql or r["serial"].startswith(q)]


def run_a6(make_env, *, count, min_matches, sort, direction, sort_key):
    env = make_env(start=False)
    ids = seed_ids(env.db)
    east_before = env.db.cert_count("tenant_id=?", ids.east)
    env.gen_large(count)
    env.start()
    db = env.db
    assert db.cert_count("tenant_id=?", ids.east) - east_before >= count

    q = choose_search_prefix(db, ids.east, min_matches)
    matches = matching_east_rows(db, ids.east, q)
    by_serial = {r["serial"]: r for r in matches}
    assert any(n >= 2 for n in Counter(sort_key(r) for r in matches).values()), \
        f"precondition: generated rows must include equal values for sort={sort}"
    params = {"q": q, "sort": sort, "dir": direction}

    first_chunk, list_done = threading.Event(), threading.Event()
    export1 = {}

    def stream_export():
        c = env.conn(timeout=120)
        try:
            t0 = time.perf_counter()
            c.request("GET", env.url("/api/certificates/export.csv", **params),
                      headers={"X-Actor-Token": ALICE})
            r = c.getresponse()
            export1["status"] = r.status
            buf, first = bytearray(), True
            while chunk := r.read1(16384):
                buf += chunk
                if first:
                    first = False
                    first_chunk.set()
                    list_done.wait(timeout=90)  # hold the export open until the list returns
            export1["t_end"] = time.perf_counter()
            export1["elapsed"] = export1["t_end"] - t0
            export1["body"] = bytes(buf)
        except BaseException as e:
            export1["error"] = e
        finally:
            first_chunk.set()
            c.close()

    t = threading.Thread(target=stream_export)
    t.start()
    assert first_chunk.wait(timeout=90), "export produced no bytes within 90s"
    listed = env.list(ALICE, page=1, page_size=20, **params)
    list_done.set()
    t.join(timeout=180)
    if "error" in export1:
        raise export1["error"]
    assert "t_end" in export1, "export did not finish"
    export2 = env.export(ALICE, **params)

    # "During the first export, request the certificate list."  (validity of the run)
    assert listed.t_recv < export1["t_end"], "list did not complete during the export; invalid run"
    # "The certificate list request succeeds while the export is running."
    assert listed.status == 200, listed.body
    assert isinstance(listed.json()["items"], list)
    # "Each export completes within 60 seconds."
    assert export1["elapsed"] < 60, export1["elapsed"]
    assert export2.t_recv - export2.t_send < 60, export2.t_recv - export2.t_send
    # "The CSV contains exactly the matching East rows in the requested order."
    header1, rows1 = parse_csv(Resp(export1["status"], export1["body"]))
    assert header1 == CSV_HEADER
    seq1 = [row["serial"] for row in rows1]
    assert len(seq1) == len(matches)
    assert len(set(seq1)) == len(seq1), "duplicate rows in export"
    assert set(seq1) == set(by_serial)
    # "The CSV contains exactly the matching East rows in the requested order."
    keys = [sort_key(by_serial[s]) for s in seq1]
    assert keys == sorted(keys, reverse=(direction == "desc"))
    # "Rows with equal sort keys appear in the same relative order on repeated exports."
    _, rows2 = parse_csv(export2)
    seq2 = [row["serial"] for row in rows2]
    assert seq2 == seq1
    # "Preserve current search and sort."
    assert [item["serial"] for item in listed.json()["items"]] == seq1[:20]


def _by_learner_name(row):
    return row["learner_name"].encode("utf-8")  # byte order = SQLite BINARY collation


def _by_issued_date(row):
    return (row["issued_local_date"], row["issued_local_time"])


def test_a6_export_growth_1k(make_env):
    """Default-run variant. With ~1k rows the response fits in socket buffers, so the
    mid-export list check is held open by the client; the 100k test is the real one."""
    run_a6(make_env, count=1_000, min_matches=100, sort="learner_name", direction="asc",
           sort_key=_by_learner_name)


@pytest.mark.slow
def test_a6_export_growth_100k(make_env):
    run_a6(make_env, count=100_000, min_matches=10_000, sort="learner_name", direction="asc",
           sort_key=_by_learner_name)


# ---------------------------------------------------------------------------
# A7 — Revocation audit
# ---------------------------------------------------------------------------

def test_a7_revocation_audit(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    x = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=? AND status='active'",
                  ids.east)
    assert x is not None, "precondition: East has an active certificate"
    a0 = db.audit_count()
    assert db.audit_count("certificate_id=? AND action='certificate.revoked'", x) == 0

    r1 = env.revoke(ALICE, x)
    a1 = db.audit_count()
    revoked_after_r1 = db.all("SELECT actor_admin_id FROM audit_events "
                              "WHERE certificate_id=? AND action='certificate.revoked'", x)
    status_after_r1 = db.scalar("SELECT status FROM certificates WHERE id=?", x)
    r2 = env.revoke(ALICE, x)

    # "The first request changes status and writes one revocation audit event."
    assert 200 <= r1.status < 300, r1.body
    assert r1.json()["status"] == "revoked"
    assert status_after_r1 == "revoked"
    assert [r["actor_admin_id"] for r in revoked_after_r1] == [ids.alice]
    assert a1 == a0 + 1
    # "The repeat does not create duplicate audit events."
    assert db.audit_count("certificate_id=? AND action='certificate.revoked'", x) == 1
    assert db.audit_count() == a1
    # "Alice revokes a valid certificate, then repeats the request."
    assert db.scalar("SELECT status FROM certificates WHERE id=?", x) == "revoked"


# ---------------------------------------------------------------------------
# Accepted extras
# ---------------------------------------------------------------------------

def test_extra_missing_or_unknown_token_is_401(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    x = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=? AND status='active'",
                  ids.east)
    c0, a0, b0 = db.cert_count(), db.audit_count(), db.bulk_op_count()
    cases = [
        ("GET", "/api/me", None, None),
        ("GET", "/api/courses", None, None),
        ("GET", "/api/certificates", None, None),
        ("GET", f"/api/certificates/{x}", None, None),
        ("POST", f"/api/certificates/{x}/revoke", None, None),
        ("POST", "/api/bulk-issues", {"course_id": ids.east_safety},
         {"Idempotency-Key": "extra-401-key-0001"}),
        ("GET", "/api/certificates/export.csv", None, None),
    ]
    failures = []
    for token in (None, "not-a-real-token"):
        for method, path, body, headers in cases:
            r = env.request(method, path, token, body, headers)
            if r.status != 401:
                failures.append((token, method, path, r.status))
    assert not failures, failures
    assert (db.cert_count(), db.audit_count(), db.bulk_op_count()) == (c0, a0, b0)
    assert db.scalar("SELECT status FROM certificates WHERE id=?", x) == "active"


def test_extra_bob_cannot_revoke_east_certificate(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    x = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=? AND status='active'",
                  ids.east)
    a0 = db.audit_count()
    r = env.revoke(BOB, x)
    assert r.status == 404, r.body
    assert db.scalar("SELECT status FROM certificates WHERE id=?", x) == "active"
    assert db.audit_count() == a0


def test_extra_rejected_cross_tenant_bulk_leaves_no_operation(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    key = "extra-xtenant-key-0001"
    b0 = db.bulk_op_count()

    r = env.bulk(BOB, key, {"course_id": ids.east_safety})
    assert 400 <= r.status < 500, r.status
    assert db.bulk_op_count() == b0

    # The rejected request did not consume the key in Bob's tenant...
    rb = env.bulk(BOB, key, {"course_id": ids.west_safety})
    assert rb.status == 200, rb.body
    # ...nor in Alice's.
    ra = env.bulk(ALICE, key, {"course_id": ids.east_safety})
    assert ra.status == 200, ra.body


def test_extra_same_key_in_two_tenants_is_independent(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    key = "extra-shared-key-0001"
    west_eligible = partition_enrollments(db, ids.west, ids.west_safety).completed_new
    west_c0 = db.cert_count("tenant_id=?", ids.west)

    ra = env.bulk(ALICE, key, {"course_id": ids.east_safety})
    rb = env.bulk(BOB, key, {"course_id": ids.west_safety})

    assert ra.status == 200, ra.body
    assert rb.status == 200, rb.body
    assert ra.json()["operation_id"] != rb.json()["operation_id"]
    assert rb.json()["course_id"] == ids.west_safety
    for entry in rb.json()["results"]:
        assert db.scalar("SELECT tenant_id FROM learners WHERE id=?",
                         entry["learner_id"]) == ids.west
    assert db.cert_count("tenant_id=?", ids.west) == west_c0 + len(west_eligible)
    tenants = {r[0] for r in db.all("SELECT tenant_id FROM bulk_operations "
                                    "WHERE idempotency_key=?", key)}
    assert tenants == {ids.east, ids.west}


def test_extra_concurrent_same_key_different_courses(make_env):
    key = "extra-race-key-0001"

    def build(env):
        db = env.db
        ids = seed_ids(db)
        other = other_east_course(db, ids)
        courses = [ids.east_safety if i % 2 == 0 else other for i in range(CONCURRENT_REQUESTS)]
        calls = [("POST", "/api/bulk-issues", ALICE, {"course_id": c},
                  {"Idempotency-Key": key}) for c in courses]
        st = SimpleNamespace(ids=ids, courses=courses,
                             per_course={c: db.cert_count("course_id=?", c) for c in set(courses)},
                             c0=db.cert_count(), a0=db.audit_count())
        return calls, st

    env, st, results = run_overlapping(make_env, build)
    db = env.db
    ok = [r for r in results if r.status == 200]
    assert ok, [(r.status, r.body[:200]) for r in results]
    assert len({r.body for r in ok}) == 1
    winner = ok[0].json()["course_id"]
    for course, r in zip(st.courses, results):
        assert r.status == (200 if course == winner else 409), (course, r.status, r.body[:200])
    assert db.bulk_op_count("tenant_id=? AND idempotency_key=?", st.ids.east, key) == 1
    for course, before in st.per_course.items():
        if course != winner:
            assert db.cert_count("course_id=?", course) == before
    issued = ok[0].json()["summary"]["issued"]
    assert db.cert_count() == st.c0 + issued
    assert db.audit_count() == st.a0 + issued


def test_extra_extra_payload_fields_rejected(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    key = "extra-payload-key-0001"

    r1 = env.bulk(ALICE, key, {"course_id": ids.east_safety})
    assert r1.status == 200, r1.body
    c1, a1, b1 = db.cert_count(), db.audit_count(), db.bulk_op_count()
    j1 = db.scalar("SELECT response_json FROM bulk_operations "
                   "WHERE tenant_id=? AND idempotency_key=?", ids.east, key)

    r2 = env.bulk(ALICE, key, {"course_id": ids.east_safety, "note": "different"})
    assert r2.status == 422, r2.body
    assert (db.cert_count(), db.audit_count(), db.bulk_op_count()) == (c1, a1, b1)
    assert db.scalar("SELECT response_json FROM bulk_operations "
                     "WHERE tenant_id=? AND idempotency_key=?", ids.east, key) == j1
    assert env.bulk(ALICE, key, {"course_id": ids.east_safety}).json() == r1.json()

    fresh = "extra-payload-fresh-0001"
    r3 = env.bulk(ALICE, fresh, {"course_id": ids.east_safety, "note": "x"})
    assert r3.status == 422, r3.body
    assert db.bulk_op_count("tenant_id=? AND idempotency_key=?", ids.east, fresh) == 0


def test_extra_repeat_revoke_returns_current_state(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    x = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=? AND status='active'",
                  ids.east)

    r1 = env.revoke(ALICE, x)
    revoked_at = db.scalar("SELECT revoked_at_utc FROM certificates WHERE id=?", x)
    r2 = env.revoke(ALICE, x)

    assert r1.status == 200, r1.body
    assert r2.status == 200, r2.body
    assert r2.json()["id"] == x
    assert r2.json()["status"] == "revoked"
    assert r2.json()["revoked_at_utc"] == r1.json()["revoked_at_utc"]
    assert revoked_at is not None
    assert db.scalar("SELECT revoked_at_utc FROM certificates WHERE id=?", x) == revoked_at


def test_extra_concurrent_revokes_write_one_audit_event(make_env):
    def build(env):
        db = env.db
        ids = seed_ids(db)
        x = db.scalar("SELECT MIN(id) FROM certificates WHERE tenant_id=? AND status='active'",
                      ids.east)
        call = ("POST", f"/api/certificates/{x}/revoke", ALICE, None, None)
        return [call] * CONCURRENT_REQUESTS, SimpleNamespace(x=x, a0=db.audit_count())

    env, st, results = run_overlapping(make_env, build)
    db = env.db
    assert [r.status for r in results] == [200] * CONCURRENT_REQUESTS, \
        [(r.status, r.body[:200]) for r in results]
    assert db.audit_count("certificate_id=? AND action='certificate.revoked'", st.x) == 1
    assert db.audit_count() == st.a0 + 1
    assert db.scalar("SELECT status FROM certificates WHERE id=?", st.x) == "revoked"


def test_extra_revoked_learner_is_skipped_on_bulk_issue(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    already = partition_enrollments(db, ids.east, ids.east_safety).already
    active = {lid: cid for lid, cid in already.items()
              if db.scalar("SELECT status FROM certificates WHERE id=?", cid) == "active"}
    assert active, "precondition: an East Safety 101 learner with an active certificate"
    learner_id, cert_id = next(iter(active.items()))

    assert env.revoke(ALICE, cert_id).status == 200
    r = env.bulk(ALICE, "extra-revoked-key-0001", {"course_id": ids.east_safety})
    assert r.status == 200, r.body
    entry = results_by_learner(r)[learner_id]
    assert (entry["outcome"], entry["reason"]) == ("skipped", "already_certified"), entry
    certs = certs_for(db, ids.east, ids.east_safety, learner_id)
    assert [(c["id"], c["status"]) for c in certs] == [(cert_id, "revoked")]


# Expected values cross-checked with zoneinfo when this file was written.
SYDNEY_DST_END_CASES = [  # DST ends 2026-04-05 03:00 AEDT (16:00Z) -> 02:00 AEST
    ("2026-04-04T12:30:00Z", "2026-04-04", "2026-04-04 23:30"),  # +11
    ("2026-04-04T13:30:00Z", "2026-04-05", "2026-04-05 00:30"),  # +11
    ("2026-04-04T15:30:00Z", "2026-04-05", "2026-04-05 02:30"),  # +11, before fallback
    ("2026-04-04T16:30:00Z", "2026-04-05", "2026-04-05 02:30"),  # +10, after fallback
    ("2026-04-05T13:30:00Z", "2026-04-05", "2026-04-05 23:30"),  # +10
    ("2026-04-05T14:30:00Z", "2026-04-06", "2026-04-06 00:30"),  # +10
]
PERTH_MIDNIGHT_CASES = [  # +8, no DST
    ("2026-01-10T15:59:00Z", "2026-01-10", "2026-01-10 23:59"),
    ("2026-01-10T16:00:00Z", "2026-01-11", "2026-01-11 00:00"),
]


def test_extra_local_date_around_dst_and_midnight(make_env):
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    failures = []
    for tenant_id, token, title, cases in (
            (ids.east, ALICE, "DST Course", SYDNEY_DST_END_CASES),
            (ids.west, BOB, "Midnight Course", PERTH_MIDNIGHT_CASES)):
        course_id, learners = add_completed_learners(
            db, tenant_id, title, [f"{title} Learner {i}" for i in range(len(cases))])
        for learner_id, (utc, date, local) in zip(learners, cases):
            cert_id = issue_via_helper(env.db_path, tenant_id=tenant_id, course_id=course_id,
                                       learner_id=learner_id, issued_at_utc=utc)
            stored = db.all("SELECT issued_at_utc, issued_local_date, issued_local_time "
                            "FROM certificates WHERE id=?", cert_id)[0]
            r = env.detail(token, cert_id)
            got = (stored["issued_at_utc"], stored["issued_local_date"],
                   stored["issued_local_time"], r.status,
                   r.json().get("issued_local_date") if r.status == 200 else None,
                   r.json().get("issued_local_time") if r.status == 200 else None)
            want = (utc, date, local, 200, date, local)
            if got != want:
                failures.append((utc, got, want))
    assert not failures, failures


def test_extra_export_with_no_matches_is_header_only(make_env):
    env = make_env()
    ids = seed_ids(env.db)
    q = "zzqx-no-match"
    assert matching_east_rows(env.db, ids.east, q) == []
    header, rows = parse_csv(env.export(ALICE, q=q))
    assert header == CSV_HEADER
    assert rows == []


def test_extra_a6_issued_date_desc_1k(make_env):
    run_a6(make_env, count=1_000, min_matches=100, sort="issued_date", direction="desc",
           sort_key=_by_issued_date)


def test_extra_failed_outcome_when_one_insert_fails(make_env):
    """A trigger makes one learner's insert fail with a non-duplicate error. The app must
    tell this apart from the unique-constraint case (both are IntegrityError in sqlite3)."""
    env = make_env()
    db = env.db
    ids = seed_ids(db)
    part = partition_enrollments(db, ids.east, ids.east_safety)
    assert part.completed_new, "precondition: an eligible East Safety 101 learner"
    victim = min(part.completed_new)
    others = part.completed_new - {victim}
    db.write(f"CREATE TRIGGER test_inject_failure BEFORE INSERT ON certificates "
             f"WHEN NEW.learner_id = {int(victim)} "
             f"BEGIN SELECT RAISE(ABORT, 'injected test failure'); END")
    a0 = db.audit_count("action='certificate.issued'")

    r = env.bulk(ALICE, "extra-failed-key-0001", {"course_id": ids.east_safety})
    assert r.status == 200, r.body
    by_learner = results_by_learner(r)
    entry = by_learner[victim]
    assert entry["outcome"] == "failed", entry
    assert entry["reason"] and entry["reason"] not in ("not_completed", "already_certified")
    assert entry["certificate_id"] is None
    for learner_id in others:
        assert by_learner[learner_id]["outcome"] == "issued"
    assert r.json()["summary"]["failed"] == 1
    assert r.json()["summary"]["issued"] == len(others)
    assert certs_for(db, ids.east, ids.east_safety, victim) == []
    assert db.audit_count("action='certificate.issued'") == a0 + len(others)


QUICK_START_TABLES = ["tenants", "admins", "courses", "learners", "enrollments",
                      "certificates", "audit_events", "bulk_operations"]


def test_extra_gen_large_keeps_quick_start_dataset(make_env):
    env = make_env(start=False)
    db = env.db
    ids = seed_ids(db)
    count = 1_000

    def snapshot():
        return {t: {tuple(r) for r in db.all(f"SELECT * FROM {t}")} for t in QUICK_START_TABLES}

    before = snapshot()
    env.gen_large(count)
    after = snapshot()

    for table in QUICK_START_TABLES:
        assert before[table] <= after[table], f"gen_large changed or removed rows in {table}"
    new_certs = db.all(
        f"SELECT tenant_id, course_title FROM certificates "
        f"WHERE id NOT IN ({','.join(str(r[0]) for r in before['certificates']) or 'NULL'})")
    assert len(new_certs) == count
    assert {(r["tenant_id"], r["course_title"]) for r in new_certs} == {(ids.east, "Load Test 101")}
    new_course_ids = {r[0] for r in after["courses"] - before["courses"]}
    titles = {db.scalar("SELECT title FROM courses WHERE id=?", c) for c in new_course_ids}
    assert titles <= {"Load Test 101"}
    lt_course = db.scalar("SELECT id FROM courses WHERE tenant_id=? AND title='Load Test 101'",
                          ids.east)
    new_enroll_courses = {db.scalar("SELECT course_id FROM enrollments WHERE id=?", r[0])
                          for r in after["enrollments"] - before["enrollments"]}
    assert new_enroll_courses <= {lt_course}
    new_learner_tenants = {db.scalar("SELECT tenant_id FROM learners WHERE id=?", r[0])
                           for r in after["learners"] - before["learners"]}
    assert new_learner_tenants <= {ids.east}

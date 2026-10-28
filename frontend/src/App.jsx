import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

const SORTS = [
  ["issued_date", "Issued"],
  ["learner_name", "Learner"],
  ["course_title", "Course"],
  ["serial", "Serial"],
  ["status", "Status"],
];
const PAGE_SIZE = 20;
const TOKEN_KEY = "actorToken";

function readStoredToken() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // storage unavailable; the picker still works for this page load
  }
}

function ErrorBox({ error, onRetry }) {
  if (!error) return null;
  return (
    <div className="error" role="alert">
      {error.message}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export default function App() {
  const [actors, setActors] = useState(null);
  const [actorsError, setActorsError] = useState(null);
  const [token, setToken] = useState(readStoredToken);
  const [me, setMe] = useState(null);
  const [view, setView] = useState("list");
  const [selectedId, setSelectedId] = useState(null);

  const loadActors = useCallback(() => {
    setActorsError(null);
    api
      .actors()
      .then((list) => {
        setActors(list);
        setToken((t) => (list.some((a) => a.token === t) ? t : list[0]?.token ?? null));
      })
      .catch(setActorsError);
  }, []);
  useEffect(loadActors, [loadActors]);

  useEffect(() => {
    setMe(null);
    setSelectedId(null);
    if (!token) return;
    storeToken(token);
    api.me(token).then(setMe, () => setMe(null));
  }, [token]);

  if (actorsError) return <ErrorBox error={actorsError} onRetry={loadActors} />;
  if (!actors) return <p className="muted">Loading…</p>;

  return (
    <div className="app">
      <header>
        <h1>Certificates</h1>
        <label>
          Acting as{" "}
          <select value={token ?? ""} onChange={(e) => setToken(e.target.value)}>
            {actors.map((a) => (
              <option key={a.token} value={a.token}>
                {a.email} ({a.tenant_name})
              </option>
            ))}
          </select>
        </label>
        {me && (
          <span className="muted">
            {me.tenant_name} · times shown in {me.timezone}
          </span>
        )}
      </header>
      <nav>
        <button type="button" className={view === "list" ? "active" : ""} onClick={() => setView("list")}>
          Certificates
        </button>
        <button type="button" className={view === "bulk" ? "active" : ""} onClick={() => setView("bulk")}>
          Bulk issue
        </button>
      </nav>
      {/* key=token: switching actor discards every per-tenant state and in-flight key */}
      {token && view === "list" && (
        <main key={token}>
          <CertificateList token={token} selectedId={selectedId} onSelect={setSelectedId} />
          {selectedId && (
            <CertificateDetail token={token} id={selectedId} onClose={() => setSelectedId(null)} />
          )}
        </main>
      )}
      {token && view === "bulk" && (
        <main key={token}>
          <BulkIssue token={token} />
        </main>
      )}
    </div>
  );
}

function CertificateList({ token, selectedId, onSelect }) {
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("issued_date");
  const [dir, setDir] = useState("desc");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(null);

  // Debounce typing into the applied query; a new query restarts at page 1.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setPage(1);
    }, 250);
    return () => clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const params = { sort, dir, page, page_size: PAGE_SIZE };
    if (q) params.q = q;
    api
      .listCertificates(token, params, ctrl.signal)
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((e) => {
        if (e.name === "AbortError") return;
        setError(e);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [token, q, sort, dir, page, reloadTick]);

  // Reload when a revoke elsewhere changes a row.
  useEffect(() => {
    const onChange = () => setReloadTick((n) => n + 1);
    window.addEventListener("certificate-changed", onChange);
    return () => window.removeEventListener("certificate-changed", onChange);
  }, []);

  function clickSort(key) {
    if (key === sort) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setDir(key === "issued_date" ? "desc" : "asc");
    }
    setPage(1);
  }

  async function doExport() {
    setExporting(true);
    setExportError(null);
    try {
      // Same applied q/sort/dir as the rows on screen.
      await api.exportCsv(token, { q, sort, dir });
    } catch (e) {
      setExportError(e);
    } finally {
      setExporting(false);
    }
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <section>
      <div className="toolbar">
        <input
          type="search"
          placeholder="Search learner name or serial…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
        />
        <button type="button" onClick={doExport} disabled={exporting}>
          {exporting ? "Exporting…" : "Export CSV"}
        </button>
      </div>
      <ErrorBox error={exportError} />
      <table>
        <thead>
          <tr>
            {SORTS.map(([key, label]) => (
              <th key={key}>
                <button type="button" className="sort" onClick={() => clickSort(key)}>
                  {label}
                  {sort === key ? (dir === "asc" ? " ▲" : " ▼") : ""}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data?.items.map((c) => (
            <tr
              key={c.id}
              className={c.id === selectedId ? "selected" : ""}
              onClick={() => onSelect(c.id)}
            >
              {/* Stored tenant-local values, never re-formatted in the browser's zone (A5). */}
              <td>{c.issued_local_time}</td>
              <td>{c.learner_name}</td>
              <td>{c.course_title}</td>
              <td className="mono">{c.serial}</td>
              <td>
                <span className={`badge ${c.status}`}>{c.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {loading && <p className="muted">Loading…</p>}
      <ErrorBox error={error} onRetry={() => setReloadTick((n) => n + 1)} />
      {!loading && !error && data?.items.length === 0 && (
        <p className="muted">{q ? `No certificates match "${q}".` : "No certificates yet."}</p>
      )}
      {data && data.total > 0 && (
        <div className="pager">
          <button type="button" disabled={page <= 1 || loading} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <span>
            Page {data.page} of {totalPages} · {data.total} certificates
          </span>
          <button
            type="button"
            disabled={page >= totalPages || loading}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </section>
  );
}

function CertificateDetail({ token, id, onClose }) {
  const [cert, setCert] = useState(null);
  const [error, setError] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState(null);

  useEffect(() => {
    let live = true;
    setCert(null);
    setError(null);
    setConfirming(false);
    setRevokeError(null);
    api.certificate(token, id).then(
      (c) => live && setCert(c),
      (e) => live && setError(e),
    );
    return () => {
      live = false;
    };
  }, [token, id]);

  async function revoke() {
    setRevoking(true);
    setRevokeError(null);
    try {
      setCert(await api.revoke(token, id));
      setConfirming(false);
      window.dispatchEvent(new Event("certificate-changed"));
    } catch (e) {
      setRevokeError(e);
    } finally {
      setRevoking(false);
    }
  }

  return (
    <aside className="detail">
      <button type="button" className="close" onClick={onClose} aria-label="Close">
        ×
      </button>
      {!cert && !error && <p className="muted">Loading…</p>}
      <ErrorBox error={error} />
      {cert && (
        <>
          <h2 className="mono">{cert.serial}</h2>
          <dl>
            <dt>Learner</dt>
            <dd>{cert.learner_name}</dd>
            <dt>Course</dt>
            <dd>{cert.course_title}</dd>
            <dt>Status</dt>
            <dd>
              <span className={`badge ${cert.status}`}>{cert.status}</span>
            </dd>
            <dt>Issued (tenant time)</dt>
            <dd>{cert.issued_local_time}</dd>
            <dt>Issued (UTC)</dt>
            <dd className="mono">{cert.issued_at_utc}</dd>
            {cert.revoked_at_utc && (
              <>
                <dt>Revoked (UTC)</dt>
                <dd className="mono">{cert.revoked_at_utc}</dd>
              </>
            )}
          </dl>
          {cert.status === "active" && !confirming && (
            <button type="button" className="danger" onClick={() => setConfirming(true)}>
              Revoke…
            </button>
          )}
          {confirming && (
            <div className="confirm">
              <p>
                Revoke <strong>{cert.serial}</strong> for {cert.learner_name}? This cannot be undone.
              </p>
              <button type="button" className="danger" onClick={revoke} disabled={revoking}>
                {revoking ? "Revoking…" : "Confirm revoke"}
              </button>
              <button type="button" onClick={() => setConfirming(false)} disabled={revoking}>
                Cancel
              </button>
            </div>
          )}
          <ErrorBox error={revokeError} />
        </>
      )}
    </aside>
  );
}

function BulkIssue({ token }) {
  const [courses, setCourses] = useState(null);
  const [coursesError, setCoursesError] = useState(null);
  const [courseId, setCourseId] = useState("");
  const [confirming, setConfirming] = useState(false);
  // { courseId, key } for the confirmed submission; kept until it succeeds so a
  // retry after a failure resends the same Idempotency-Key.
  const [pending, setPending] = useState(null);
  const [inFlight, setInFlight] = useState(false);
  const inFlightRef = useRef(false); // guards double clicks before the re-render disables the button
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  useEffect(() => {
    api.courses(token).then(
      (list) => {
        setCourses(list);
        if (list.length) setCourseId(String(list[0].id));
      },
      setCoursesError,
    );
  }, [token]);

  function chooseCourse(id) {
    setCourseId(id);
    setConfirming(false);
    setPending(null); // a different course is a new submission with a new key
    setError(null);
  }

  async function send(submission) {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setInFlight(true);
    setError(null);
    try {
      const res = await api.bulkIssue(token, submission.courseId, submission.key);
      setResult(res);
      setPending(null);
    } catch (e) {
      setError(e);
    } finally {
      inFlightRef.current = false;
      setInFlight(false);
    }
  }

  function confirm() {
    const submission = { courseId: Number(courseId), key: crypto.randomUUID() };
    setPending(submission);
    setConfirming(false);
    setResult(null);
    send(submission);
  }

  if (coursesError) return <ErrorBox error={coursesError} />;
  if (!courses) return <p className="muted">Loading…</p>;
  if (!courses.length) return <p className="muted">This tenant has no courses.</p>;

  const courseTitle = courses.find((c) => String(c.id) === courseId)?.title;

  return (
    <section>
      <div className="toolbar">
        <label>
          Course{" "}
          <select value={courseId} onChange={(e) => chooseCourse(e.target.value)} disabled={inFlight}>
            {courses.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
        {!confirming && !pending && (
          <button type="button" onClick={() => setConfirming(true)}>
            Issue certificates…
          </button>
        )}
      </div>
      {confirming && (
        <div className="confirm">
          <p>
            Issue certificates to every eligible learner in <strong>{courseTitle}</strong>?
          </p>
          <button type="button" onClick={confirm}>
            Confirm issue
          </button>
          <button type="button" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
      {pending && (
        <div className="confirm">
          <p className="muted">
            Request key <span className="mono">{pending.key}</span>
          </p>
          <button type="button" onClick={() => send(pending)} disabled={inFlight}>
            {inFlight ? "Issuing…" : "Retry"}
          </button>
          <button type="button" onClick={() => chooseCourse(courseId)} disabled={inFlight}>
            Discard
          </button>
        </div>
      )}
      <ErrorBox error={error} />
      {result && <BulkResult result={result} />}
    </section>
  );
}

function BulkResult({ result }) {
  const { issued, skipped, failed } = result.summary;
  return (
    <>
      <p>
        Operation #{result.operation_id}: <strong>{issued}</strong> issued, <strong>{skipped}</strong>{" "}
        skipped, <strong>{failed}</strong> failed.
      </p>
      <table>
        <thead>
          <tr>
            <th>Learner</th>
            <th>Outcome</th>
            <th>Reason</th>
            <th>Certificate</th>
          </tr>
        </thead>
        <tbody>
          {result.results.map((r) => (
            <tr key={r.learner_id}>
              <td>{r.learner_name}</td>
              <td>
                <span className={`badge ${r.outcome}`}>{r.outcome}</span>
              </td>
              <td>{r.reason ?? "—"}</td>
              <td>{r.certificate_id ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

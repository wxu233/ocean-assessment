// Thin fetch wrapper. Every request except /api/actors carries X-Actor-Token.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(path, { token, method = "GET", headers = {}, body, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      signal,
      headers: {
        ...(token ? { "X-Actor-Token": token } : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "network_error", "Network error: the server could not be reached.");
  }
  if (!res.ok) {
    let code = "http_error";
    let message = `Request failed (${res.status}).`;
    try {
      const data = await res.json();
      code = data.error?.code ?? code;
      message = data.error?.message ?? message;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new ApiError(res.status, code, message);
  }
  return res;
}

const json = (p) => p.then((r) => r.json());

export const api = {
  actors: () => json(request("/api/actors")),
  me: (token) => json(request("/api/me", { token })),
  courses: (token) => json(request("/api/courses", { token })),
  listCertificates: (token, params, signal) =>
    json(request(`/api/certificates?${new URLSearchParams(params)}`, { token, signal })),
  certificate: (token, id) => json(request(`/api/certificates/${id}`, { token })),
  revoke: (token, id) => json(request(`/api/certificates/${id}/revoke`, { token, method: "POST" })),
  bulkIssue: (token, courseId, key) =>
    json(
      request("/api/bulk-issues", {
        token,
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: { course_id: courseId },
      }),
    ),
  // The export needs the actor header, so a plain <a href> can't fetch it:
  // download via fetch, then hand the blob to the browser as a file.
  async exportCsv(token, { q, sort, dir }) {
    const params = new URLSearchParams({ sort, dir });
    if (q) params.set("q", q);
    const res = await request(`/api/certificates/export.csv?${params}`, { token });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "certificates.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

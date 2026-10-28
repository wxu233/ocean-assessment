1. The release fails because the rolling deployment allows both old and new versions to access the same database, while the migration removed a column that the old version still relies on. Dropping display_name also makes rollback unsafe while the old version is redeployed. 

The proposed safe version is: 
1. Add the new full_name schema and backfills it with display_name. 
2. Deploy a backward compatible version that uses both display_name and full_name (also select limited number of users to the new version first, for testing).
3. Migrate new users & old continously, and drop the old schema only after rollback window expires. 
4. Fully move to new version. 

---

2. A The stale value was inserted after request B invalidates the cached value. A shorter TTL reduces the potential of stale entries, but does not guarantee correctness in this situation. The point is request A cannot write back a staled value to cache after B's invalidation. I'd use a versioned cache and atomic cache write. IE. the initial state would be (ACTIVE, version=1). Request A reads from database. Request B then revokes, updating database and cache with (REVOKED, version=2). Request A now attempts to cache version=1, and fails because it is older. 

---

3. The sudden increase in latency from B causes every request from A to fail, while retries multiply the out bound requests. With 1000 requests persecond, A's worker quickly depletes, and no workers are left to process unrelated endpoints. 

I'd add a circuit breaker so requests can fail fast. If B becomes unhealthy, /report should return a 503 immediately. Set up a background task to repeatedly probe Service B, and only direct traffic to B once it becomes healthy again. If stale data is acceptable, then returning a cached, possibly stale data layer can be introduced, and /report would return the staled data when B is unhealthy. 
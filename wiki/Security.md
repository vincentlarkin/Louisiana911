# Public-data access protections

History remains public. These controls discourage bulk harvesting; they cannot make records already shown to visitors uncopyable. Browser JavaScript is public, and a public GitHub repository exposes its source independently of the site.

## Request limits

History requests require the signed, expiring browser-session cookie and the UI request header. Origin/fetch headers reject ordinary cross-site browser use, but are not authentication against a custom client.

Default per-IP limits:

- 10 distinct History dates per minute.
- 20 distinct dates per hour and 40 per day.
- 60 History requests per minute, including repeated pages for the same date.
- At most 2,000 incidents per response; a date is required.

The UI honors Retry-After, including hour/day waits. Calendar-count requests do not consume the distinct-date allowance. Responses exclude internal geocoder queries, timestamps, versions, and any future database fields outside the explicit public allowlist.

## Deployment requirements

- Keep database files, backups, private environment files, and deployment checkouts outside static roots. Block database/backup extensions and private paths at the proxy. The app serves static files only from `public/`.
- Do not publish the web container's port to the internet. Only the reverse proxy should reach it.
- Leave `LOUISIANA911_TRUSTED_PROXY_HOPS=0` on directly reachable apps. Set it to `1` only when the trusted immediate proxy overwrites X-Forwarded-For. It reads the rightmost trusted address, never an arbitrary client-supplied first entry.
- At the reverse proxy, trust Cloudflare's client-IP header only from the actual tunnel peer. Overwrite forwarding headers and preserve HTTPS only from trusted ingress so cookies receive Secure correctly.
- Use a proxy rate-limit zone shared across workers. The production API limit is 2 requests/second with a burst allowance of 30 and a 429 response.
- Set a stable private `LOUISIANA911_HISTORY_UI_SECRET` and `LOUISIANA911_ACCESS_LIMIT_DB` pointing to a separate writable quota database. Hour/day date limits then survive process restarts and are shared by workers. It stores keyed hashes of IP addresses, not raw IPs, and expires entries after 24 hours. If the quota database is unavailable, History fails closed with a retry response. Without this setting, date counters are held in process memory.
- Configure quotas with `LOUISIANA911_HISTORY_HOURLY_DATE_LIMIT`, `LOUISIANA911_HISTORY_DAILY_DATE_LIMIT`, and `LOUISIANA911_HISTORY_REQUEST_RATE_LIMIT` if measured normal usage calls for changes.

Public access cannot prevent collection through distributed clients or sufficiently slow requests. Stronger access restrictions would require authentication or removing data from public responses. Robots directives are indexing guidance, not access control.

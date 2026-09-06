# Data Sources

Louisiana911 combines public information from Caddo Parish, Baton Rouge, Lafayette Parish, New Orleans, and Lake Charles.

Each source has different timing and detail:

- Caddo publishes active events.
- Baton Rouge publishes traffic incidents and approximate map information.
- Lafayette publishes active traffic and public-safety incidents.
- New Orleans publishes a delayed daily calls-for-service record.
- Lake Charles publishes LCPD closed police calls with a 24-hour delay, including traffic, crime-related reports, service calls and officer activity. These are not confirmed crimes or coverage of every Calcasieu agency.

Source labels and public descriptions are normalized for a shared display while retaining the source jurisdiction. Records may be delayed, revised, reclassified, generalized, or unavailable.

Louisiana911 does not claim to reproduce a complete dispatch record or final investigative outcome.

## Live snapshot contract

Caddo, Baton Rouge, and Lafayette adapters return an empty list only for a recognized, fully parsed empty snapshot. Missing pages, failed requests, changed schemas, or partially parsed records raise an error; the collector wrappers return `None` for those failures. Baton Rouge also verifies the page's published incident count. Lafayette preserves its full source timestamp in `occurred_at` instead of discarding the date.

The collector enables empty-snapshot removal only for sources in `LIVE_SNAPSHOT_SOURCES`. A new live adapter must validate complete snapshots and distinguish failures from empty results before joining that set. Delayed publication feeds have different retention rules and must not inherit live-feed removal automatically.

## Lake Charles import

The adapter reads the [public Police-to-Citizen search](https://lakecharles.policetocitizen.com/cadcalls) using its normal anonymous session and CSRF cookie. This is the site's public web API, not a guaranteed partner API. No account, API key, open calls, unrestricted date search or incident-detail lookup is used.

Every 30 minutes it pages the agency's current published window in batches of 200, with a hard ceiling of 2,000 rows. Unexpected responses, partial pages and errors fail the import without replacing stored results. Repeated imports update stable LCPD event identities rather than adding duplicates; the source's current public coordinates also replace or clear previous points.

All records remain closed (`is_active=0`) and enter History immediately. The Latest API also selects at most 150 closed calls from the last seven days using the existing source/status/date index. This does not inflate active incident counts. Original start times are retained in UTC and displayed in Louisiana time, including the date. The importer enforces a minimum 24 hours after closure.

Missing or withheld map points are not geocoded. Published points are approximate; the original public location label is preserved. Unit counts are unavailable and must not be invented. Historical data before the first import is limited to the agency's rolling window; the site does not claim a complete retrospective archive.

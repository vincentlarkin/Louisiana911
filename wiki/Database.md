# Database behavior and sweep notes

SQLite remains suitable for the current collector and read-oriented UI. Startup enables WAL and creates the History index automatically; no destructive migration is required.

- History reads an ordered, bounded prefix from the main database and relevant monthly archive, then merges the requested page. Equal timestamps use stable IDs. Counts and rows use one read snapshot per database.
- Source filters retain compatibility with archives created before the source column existed. Invalid dates return 400; an unreadable archive returns a retryable error rather than a misleading partial result.
- The collector releases its write transaction before possible remote geocoding. Authoritative feed coordinates remain batched.
- Archiving updates an existing archive copy with corrected fields and deletes the main copy only if it is still inactive and its observation/geocoding timestamps have not changed.
- Routine archiving no longer runs VACUUM. SQLite reuses freed pages, avoiding a full database rewrite and extra writer contention; the file may retain its high-water size. Explicit file compaction can be scheduled separately during maintenance if disk reclamation is needed.

## Validation (2026-09-04)

The local database contained 301 incidents and passed SQLite's quick integrity check. These observations do not establish production database size or health.

In an in-memory synthetic fixture with 100,000 rows and 10,800 matching one day, requesting the first 100 rows materialized 100 rows instead of 10,800. Median query time over seven runs changed from 37.17 ms to 0.37 ms, with the same page returned. Production results depend on data volume, hardware, caches, source filters, and requested offset. Deep offsets still require a larger prefix; cursor pagination is a possible future improvement if production measurements justify it.

Regression checks cover pagination across archives, equal timestamps, legacy schemas, source filtering, Central-time DST boundaries, archive failures, writer availability during geocoding, batching of published coordinates, and concurrent archive updates. Flask and Requests updates were tested in an isolated Windows environment; Gunicorn's Linux runtime was not exercised here.

Longer-term, profile report-month discovery against the production archive volume before adding more indexes or caches. Do not add speculative indexes: each adds storage and collector write work.

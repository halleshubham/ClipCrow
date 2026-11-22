# Plan: everything structured in PostgreSQL

Status: PLAN, nothing built. Written 2026-11-22. Decisions needed are at the end.

## Where the data is today

| What | Where | How often it is written |
|---|---|---|
| People, sessions, usage ledger, credit log, sign-in codes, webhook ids, SocioBird links, view-as, admin audit | `accounts.py` on the `db.py` layer: SQLite file by default, PostgreSQL with `DATABASE_URL` (`clipcrow migrate-db` copies SQLite to PostgreSQL) | per request |
| **Jobs**: `job.json` per job (status, stage, params, clips), plus `out/posts.json`, `out/gpu_usage.json`, `out/edl.json` | files under the data directory (`jobs.py`, `JobStore`) | every progress tick while a job runs |
| Brands: `brands.json` per account, `settings.json` and `overlay.json` per brand | files (`brand.py`) | rare |
| Styles: `styles/<owner>/<id>.json` | files (`style_store.py`) | rare |
| Posting times: `schedule/<owner>.json` | files (`schedule.py`) | rare |
| Operator settings and the last 50 changes: `settings.json` | file (`settings.py`) | rare |
| Direct-upload sessions | files (`uploads.py`) | during an upload |
| Transcripts, recogniser output (`asr.json`), cleanup reports, speaker maps | files in each job's `out/` | once or twice per job |
| Videos, clips, brand images, pictures | disk, then R2 | not database data |

## Why move it

- **One machine only.** Jobs and brands live on one disk, so there is no second web replica and no failover. A restart or a lost disk is a data event.
- **Cross-record changes are not atomic.** A job, its usage row and a refund are three different stores. A crash between them has been survivable only because of care, not because of a transaction.
- **Listing walks the disk.** `JobStore.briefs()` stats every job folder on every request, and the operator People table, retention and the Storage card scan directories.
- **Backup is a file copy** that has to be consistent with the database and the secret file.

## What "full" means here

All **structured, queried or edited** data goes to PostgreSQL. **Media stays out**: videos, clips and images are files on disk and in R2. Large machine output (transcript, `asr.json`, cleanup reports; megabytes for a long talk) stays as files next to the job, referenced from the job row. The EDL (small, edited, and the contract of the pipeline) goes in the database as `jsonb`.

## Design

1. **One implementation on the existing `db.py` layer**, which already speaks SQLite and PostgreSQL. SQLite stays for development and the unit tests (no network, as CLAUDE.md requires); production uses PostgreSQL. No second, file-based implementation to keep in step.
2. **Pydantic stays the source of truth.** Each record is stored as `doc jsonb` plus the few columns we query: `jobs(id, owner, status, created_at, updated_at, keep, version, doc)`, `posts`, `gpu_usage`, `job_docs(job_id, kind, doc)` for the EDL, `brands`, `brand_settings`, `styles`, `schedules`, `operator_settings` and `operator_settings_history`, `upload_sessions`. A `schema_version` per doc lets old rows be upgraded on read.
3. **Optimistic writes instead of a process lock.** `UPDATE ... SET doc = ..., version = version + 1 WHERE id = ? AND version = ?` with a retry replaces `JobStore._lock`. This is what makes a second web replica possible later.
4. **`briefs()` becomes one indexed query.** Retention, queue position and the People table become queries, with the disk delete done after the row is marked.
5. **Ids and URLs unchanged.** Job ids, brand ids, `storage.REL` keys and every API shape stay as they are, so the UI and workers need no change.

## Phases (each one deployable and reversible)

0. **Prerequisites:** where PostgreSQL runs in production, nightly `pg_dump` to R2 with a restore drill, pool size, and a CI service container so the suite also runs against PostgreSQL (today it only does where `accounts_on_postgres` is set).
1. **Small stores:** operator settings (and history), posting times, styles, brands. Low write volume, so this proves the pattern. Switch per store with `CLIPCROW_STORE_<NAME>=files|db`.
2. **Posts and GPU usage** (the two JSON files inside each job).
3. **Jobs and EDLs:** the high-write store. Dual-write for one release (files remain authoritative), then reads from the database with the file as fallback, then the files are left untouched for a retention period.
4. **Upload sessions**, then retire the file readers.
5. **Operator Storage card and retention** move from directory scans to queries.

Each phase extends `clipcrow migrate-db --all`: idempotent, read-only on the files, counts and checksums in the report, no secret printed. Cutover runs when `/api/health` says `busy: false`; rollback is flipping the flag back, because the files are not deleted during the phase.

## Tests

- Every store's existing tests run against **both** backends (parametrised).
- A crash between the job save and the usage write leaves a consistent state (failure injection).
- Threaded writers on one job: the version check retries and no update is lost.
- Migration round trip: files in, rows out, counts and checksums equal, run twice with no duplicates.
- The suite runs on PostgreSQL in CI.

## Risks

- **The secret file** (`clipcrow.secret`) still decrypts stored SocioBird connections. A restore needs the database, R2 and that file together; the backup drill must include it.
- **Disk and database must agree.** Delete order: mark the row, remove the files, remove the row; a sweeper handles a crash in between.
- **Schema drift** in the pydantic models: handled by `schema_version`, tested with fixtures of old job files.
- **Worker and server writes.** Workers talk to the API, not the database, so they are unaffected. The home and rented GPU workers need no update for this migration.

## Not included

Media blobs, moving R2, a read replica, and sharding. Per-job transcript storage in the database is a later option if file reads become the bottleneck.

## Decisions needed

1. Where does production PostgreSQL run today (Coolify database service or managed), and is `DATABASE_URL` already set there for accounts?
2. Keep SQLite for development and tests (recommended), or PostgreSQL only?
3. Transcript and `asr.json` as files next to the job (recommended) or in the database?
4. Is a second web replica a goal? It decides whether the optimistic-write change in step 3 is required now or later.
5. Is a short maintenance window acceptable for the jobs cutover (a restart while no job runs, as for any deploy)?
6. Backup target and retention for `pg_dump` (R2 nightly, 14 days suggested).

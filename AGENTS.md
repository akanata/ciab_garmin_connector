# Cloud in a Bottle Health Producer for Garmin

This application implements a producer for the Cloud in a Bottle health data
spec from Garmin devices. The Garmin API itself is locked behind a developer
application portal, so data is extracted using GarminDB.

The service has two halves: an **ingest** side that drives GarminDB headlessly
on a schedule to keep a local SQLite corpus fresh, and a **serve** side that
maps those rows into `health_data_service` types over the spec's HTTP contract.

## Project Status

`plan.md` at the repo root is the design of record; read it before writing code.
The current iteration covers heart rate (`specific_types.py`) and sleep
(`sleep_types.py`); workouts are out of scope.

**Landed — all of plan.md Parts 1–3:** the toolchain (`pyproject.toml`,
`uv.lock`, `justfile` — note `just` itself may not be installed, in which case
run the underlying `uv run …` commands), the Garmin auth flow (`config.py`,
`garmin_config.py`, `auth.py`, `routes/owner.py`, `app.py`), containerization
(`Dockerfile`, `openhost.toml`), the timezone strategy (`timezones.py`,
`providers/garmindb/timezone_probe.py`), and the sync engine (`sync.py`,
`providers/garmindb/ingest.py`) with `POST /sync`, `GET /sync/status` and the interval loop.
`tests/providers/garmindb/fixtures.py` builds a real GarminDB SQLite corpus in a tmpdir.
`requirements.txt` is gone; dependencies live in `pyproject.toml` and are pinned
by `uv.lock`.

**Landed — plan.md Part 4, the serving layer:** `serialization.py`,
`registry.py`, `service.py`, `routes/service.py`, and
`providers/garmindb/{connection,sampling,vocabulary,heart_rate,daily,sleep}.py`. `/api/v1/metrics`,
`/api/v1/time-series` and `/api/v1/sleep-sessions` serve real data;
`/api/v1/workouts` is an empty `{"data": []}` and `/api/v1/workouts/{id}` a 404,
both deliberately.
`resolve_policy()` is now called once at boot by `providers/garmindb/connection.py`, and
again on every `GarminConnection.reset()`.

Four metrics are served: `heart_rate`, `hrv_rmssd`, `sleep_score`,
`readiness_resting_heart_rate`. `providers/garmindb/registry.py`'s docstring
records which spec metrics are deliberately *not* served, and why — read it
before adding one. Its `metrics_for(conn)` binds each entry to the corpus, so the
generic `registry.py` never names a `GarminConnection`.

**Also landed alongside Part 4:** `POST /rebuild` (owner-only), which deletes the
SQLite files and reimports the retained JSON/FIT corpus with `latest=False`. It
is the owner's only way out of a schema mismatch in a container, and it never
re-downloads. `/setup` shows the fault and the button only while there is one;
`GET /sync/status` grew a `serving` block.

**Owner-controlled import scope.** `preferences.py` persists the three knobs that
decide how long a sync runs — the earliest date to import from, and which of the
four downloadable statistics to fetch, and how often the background sync runs — to `import_preferences.json` under app
data. `GARMIN_BACKFILL_START_DATE` only *seeds* the default now; once the owner
saves, the file wins. `/setup` renders the form, a per-metric coverage table
(rows held, the window covered, and the gap against the chosen floor) and a live
progress line; `POST /setup/import` saves it and immediately re-renders
`GarminConnectConfig.json`.

**Backfill.** `download_plan()` only ever moves *forward* from a metric's newest
row, so lowering the floor would otherwise be a no-op on any metric that already
holds data. `backfill_plan()` is its complement — the older range each enabled
metric is missing — and `POST /backfill` runs it through the normal
import/analyze phases. A metric with **no** rows is deliberately excluded: an
empty table already starts at the floor, so backfilling it would duplicate a
normal sync at twice the cost. This is why the coverage table says "starts at
your date on the next sync" rather than "complete" for an empty metric.

**Progress.** `SyncEngine` owns a `SyncStep` behind a `threading.Lock` (written
on the worker thread, read on the event loop) and hands the ingest port a
`ProgressSink`. The engine labels each phase *before* handing off, so the page
says something the instant a sync starts rather than staying blank until the
adapter reaches a reportable step. `/sync/status` exposes `progress` and
`coverage`; `/setup` polls it and reloads once the run ends.

**Schedule.** The background sync runs hourly by default and the owner can change
it on `/setup`. Its start is persisted, so a restart syncs as soon as one is due
rather than waiting out a whole interval — see the Sync guardrails for how that
keeps the crash-loop protection the old sleep-first loop provided.

**Not built yet:** workouts, body battery (`daily_summary.bb_*`, available but
with no spec type — it would go under vendor-extension metric ids), and any
push/webhook ingest. There is still no full-reimport endpoint *other* than
`/rebuild`, which is a heavier hammer than a per-file retry.

**Environment knobs.** `config.py` reads exactly two: `HEALTH_PROVIDER` (default
`garmindb`) and `BOTTLE_APP_DATA_DIR` (or the legacy `OPENHOST_APP_DATA_DIR`).
Every knob below is read by `providers/garmindb/settings.py` instead, and a
second provider would bring its own: `GARMIN_HOME_TZ`, `GARMIN_IMPORT_TZ`,
`GARMIN_BACKFILL_START_DATE` (default `2019-12-31`; a full backfill is roughly
one second per day *per stat*, so the default is hours on first run),
`SYNC_INTERVAL_SECONDS` (default 1h; it only *seeds* the interval the owner sets on `/setup`), `GARMIN_DOMAIN`, `BOTTLE_APP_DATA_DIR` (or the legacy
`OPENHOST_APP_DATA_DIR`; set by the router, never by the image),
`GARMIN_FILL_STAGE_GAPS` and `GARMIN_DERIVE_RESTLESS_PERIODS` (both default
false; both make the service emit data Garmin did not record, so leave them off
unless a specific consumer needs them). Serving limits are constants in
`config.py`: `DEFAULT_LIMIT` 5 000, `MAX_LIMIT` 50 000, `MAX_ROWS_SCANNED`
1 000 000, `MAX_SESSION_SUBSERIES` 2 000.

## Important References

- Cloud in a Bottle Health Data service spec: https://github.com/cloud-in-a-bottle/health-data-service-spec
- Cloud in a Bottle - Creating an App: https://cloudinabottle.org/docs/creating_an_app/overview.html
- Cloud in a Bottle - App Manifest Spec: https://cloudinabottle.org/docs/creating_an_app/manifest_spec.html
- Cloud in a Bottle - Cross-App Services: https://cloudinabottle.org/docs/creating_an_app/cross_app_services.html
- GarminDB: https://github.com/tcgoetz/GarminDB

https://github.com/akanata/openhost_spec_mcp is a sibling Cloud in a Bottle app
that *consumes* this same spec. It is the reference for stack, layout,
Dockerfile, and test harness.

## Development Commands

- **Environment:** `uv sync` — manages the standard `.venv/`;
  `source .venv/bin/activate` still works. (The venv is `.venv/`, not `venv/`.)
- **Run local dev server:** `just run`
  → `uv run hypercorn garmin_health.app:app --bind 0.0.0.0:8080 --reload`
- **Lint, format, typecheck:** `just check`
  → `ruff check --fix . && ruff format . && uv run mypy`
- **Execute test suite:** `just test` → `uv run pytest -x`
- **Build container image:** `just build` → `docker build -t garmin-health .`

## Code Style & Architecture

- **Language:** Python 3.12 (`requires-python = "==3.12.*"`). GarminDB requires
  `>=3.12`; the sibling app and the Dockerfile pin 3.12.
- **Framework:** Litestar served by Hypercorn. This is a JSON API — there is no
  React, no Tailwind, and no frontend build. The only HTML is the owner-facing
  `/setup` page, which should be plain server-rendered markup.
- **Formatting:** 4-space indentation, double quotes, ruff `line-length = 100`.
  Lint rules `E,F,B,UP,I,PLC0415`; isort `force-single-line`. (plan.md §6 says
  119, matching the sibling app; this file wins and `pyproject.toml` uses 100.)
  `ruff format` reformats Python code blocks **inside Markdown**, which would
  rewrite the snippets in `plan.md` and this file — `extend-exclude = ["*.md"]`
  prevents that. Do not remove it.
- **Typing:** mypy `strict = true`, plus `follow_untyped_imports = true` — the
  spec package ships full annotations but no `py.typed` marker, so without it
  every import from it degrades to `Any`.
- **Wire types are attrs + cattrs, not pydantic.** All emitted timestamps are
  timezone-aware UTC, serialized as ISO 8601.
- **Isolation rule:** only the `providers/garmindb/` package may import `garmindb`,
  `idbutils`, `fitfile`, or `sqlalchemy`. Everything crossing that boundary is
  a `health_data_service` type or a stdlib type. GarminDB may be swapped later
  for the real Garmin API or a different unofficial API later; this must not
  impact the HTTP layer.

## Project Structure

```
src/garmin_health/
  config.py         Settings (app_data_dir, provider) from env. No I/O.
  ports.py          HealthReader - what a provider hands the serving layer.
  errors.py         ProviderUnavailable / ProviderNotReady. Both map to 503.
  limits.py         resolve_limit, decimate, check_scan_cap. No provider content.
  progress.py       SyncStep / ProgressSink - what a running acquisition reports.
  serialization.py  cattrs converter, hooks, the three response envelopes.
  registry.py       MetricEntry + metric_entry(). Declarative; no provider types.
  service.py        HealthDataService facade - the only thing routes/ imports.
  app.py            create_app(): wiring, the lifespans, the sync loop task.
  providers/
    garmindb/       settings, config_file (GarminConnectConfig.json), auth,
                    preferences, sync (the engine + the Ingest port), ingest,
                    timezones, timezone_probe, connection, reader, registry,
                    sampling, vocabulary, heart_rate, sleep, daily.
  routes/           service.py (/api/v1/*), owner.py (/setup, /sync, /health).
tests/
  providers/
    garmindb/       fixtures.py (build_fixture() - a real GarminDB SQLite in a
                    tmpdir), fakes.py, and every test needing either.
```

Dependency direction is strictly
`routes -> service -> ports/registry/limits/errors -> health_data_service types`.
**Nothing in that chain imports a provider.** The GarminDB reader is constructed
in `app.py`'s lifespan and reaches `service.py` only as a `ports.HealthReader`;
the ingest side reaches `sync.py` only as an `Ingest`. `timezones.py` is imported
only by `providers/garmindb/*`. (`app.py`, `sync.py`, `auth.py`,
`garmin_config.py` and `preferences.py` are still GarminDB-shaped and move under
the provider in `provider_refactor.md` Phase 3-4; nothing enforces any of this
until Phase 5.)

## Critical Guardrails & Gotchas

**General**
- ALWAYS IMPLEMENT UNIT TESTS BEFORE BUSINESS LOGIC (test-driven development).

**Timezones — the highest-risk area of this project.**

- GarminDB stores **naive** datetimes on four different clocks. FIT-sourced
  rows (`monitoring_hr`, `monitoring_hrv_value`, `sleep_events`) are
  device-local; `sleep.start`/`sleep.end` are rendered in the *importing
  container's* `TZ`. At `TZ=UTC` these disagree by hours.
- Container `TZ` must be the Garmin account's home timezone; `GARMIN_HOME_TZ`
  overrides. Never fall back to the container's local zone — a silent wrong
  answer corrupts every timestamp and will not be noticed for months.
- `import_offset` applies to `sleep.start`/`sleep.end` **only**. Everything
  else converts via `home_tz` alone.
- Query bounds must be **naive**. SQLAlchemy's SQLite `DATETIME` bind processor
  discards `tzinfo`, so an aware bound silently mis-filters with no error — it
  returns a plausible *subset*, not an error and not nothing
  (`test_an_aware_bound_silently_selects_the_wrong_rows` demonstrates it).
- `TimeZonePolicy.to_utc` / `.to_naive_local` **reject** inputs of the wrong
  awareness. Both would otherwise succeed silently: `.replace(tzinfo=...)`
  relabels an aware value, and `.astimezone()` on a naive one assumes the
  *system* zone.
- A learned scalar `import_offset` can only be right for one side of a DST
  transition when the import zone and the home zone change clocks on different
  dates. `GARMIN_IMPORT_TZ` names the import zone instead and is exact; prefer
  it whenever the corpus was imported under a known non-home `TZ`.
- The import-offset probe anchors its event search on `sleep.day`, not
  `sleep.start`, because `day` and `sleep_events` are both on the home clock —
  so the search window does not move with the very offset being learned.

**Serving.**

- `GarminConnection` is opened once in a lifespan hook and reset (never
  reconstructed by a crash) thereafter. Every read goes through `conn.read()`,
  which retries **exactly once** on `OperationalError` after a `reset()`.
- The two degraded states are different and must not be conflated. *No timezone
  and no data* is a never-synced container: serve empty, 200. *No timezone but
  data present* is a misconfiguration: 503, because serving those rows on the
  container's local clock is a silent hours-wide error. `service.py` draws that
  line; nothing else should.
- `/health` stays 200 through every degraded state. Failing it makes the router
  restart-loop the container the owner has to visit to fix the problem.
- Day-keyed samples (`sleep_score`, `readiness_resting_heart_rate`) convert local
  midnight through the same `to_utc` as everything else, so a Denver day is
  `T06:00Z`/`T07:00Z` rather than `00:00Z`. A consumer bucketing by *UTC* date is
  therefore off by one in western zones — inherent, since the spec has no
  date-valued sample type.
- `limit` means **even decimation across the requested window**, preserving the
  first and last readings. Never most-recent-N: that silently discards the
  `start` the consumer explicitly passed.

**Spec conformance.**

- Construct spec types with **keyword arguments only**. attrs moves overridden
  base fields to the end, so `HeartRate.__init__` is `(source, metric_id=...,
  ..., samples=[])` — positional construction produces garbage.
- Never serve an interval-valued metric on `/v1/time-series`. The client's
  `Sample` structure hook resolves by MRO and silently drops `end_timestamp`.
  Sleep stages reach consumers only via `SleepSession.stages`.
- The manifest must declare the service as
  `github.com/imbue-openhost/health-data-service-spec` — the pre-rename string
  the spec's client still hardcodes. Declaring the `cloud-in-a-bottle` URL
  means no consumer will ever route to us. Confirmed against
  `health-dashboard`'s `[[services.v2.consumes]]`, which asks for exactly this.
- **`endpoint` is a prefix the router PREPENDS, not a description of where the
  routes already are.** The docs: "Service requests land rooted at `endpoint` in
  the provider app, ie `app-name.your-domain.com/<endpoint>/<route>`". The spec's
  client requests `/v1/metrics`, so `endpoint = "/api/"` lands on
  `/api/v1/metrics` — which is what we serve, and what the working
  `apple-health-connector` declares. `endpoint = "/v1/"` lands on
  `/v1/v1/metrics`: a 404 that `_fan_out` reads as "this provider has nothing",
  giving a blank consumer and **no error in any log**. `TestManifestRoutingContract`
  parses `openhost.toml` and asserts every path the client requests is served
  where the router will look; keep it passing.
- The spec surface is mounted **only** at `/api/v1/*` — the path the router
  actually asks for. `SERVICE_ROOT` in `routes/service.py` and `endpoint` in
  `openhost.toml` have to move together, and `TestManifestRoutingContract` is
  what enforces that. To reach it without the router (for a manual check), call
  `/api/v1/metrics` directly; there is deliberately no second mount.
- `[resources]` takes `cpu_cores`, not `cpu_millicores` — the Apple connector
  uses the latter and silently gets the 0.1-core default. Do not copy it.
- **The Dockerfile must not set `BOTTLE_*` or `OPENHOST_*` variables.** The router
  `podman rm -f`s the container on every update, reload and restart, so only the
  bind mount at `/data/app_data/<app>` survives. Routers before 2026-08-27 export
  only `OPENHOST_APP_DATA_DIR`; a `BOTTLE_APP_DATA_DIR` baked into the image then
  wins `config.py`'s name precedence, and the token, the preferences and the whole
  corpus land on the container's own disk -- working perfectly until the next
  update wipes them. That is exactly what `ENV BOTTLE_APP_DATA_DIR=/app/data` did.
  A bare `docker run` needs no such line: `DEFAULT_APP_DATA_DIR` is `data`,
  relative to `WORKDIR /app`. `test_the_image_never_shadows_the_routers_volume`
  pins it.

**GarminDB.**

- `GarminConnectConfigManager` calls `sys.exit(-1)` on a missing or malformed
  config. Validate the JSON before constructing it.
- Its `homedir` and `temp_dir` are class attributes evaluated **at import**, so
  setting `HOME` after `import garmindb` has no effect. Use an absolute
  `directories.base_dir` with `relative_to_home: false`.
- Importers **swallow every per-file exception** — a totally failed sync looks
  successful. Verify by comparing row counts and `latest_time()`.
- `MonitoringFitFileProcessor` dereferences `plugin_manager` unconditionally;
  passing `None` raises `AttributeError`.
- Constructing a `DB` object **writes** (`create_all` + a version check), so
  `DBs/` cannot be mounted read-only. After any DB rebuild call
  `GarminConnection.reset()` — pooled handles otherwise point at the deleted
  inode and serve stale data silently.
- GarminDB and `garminconnect` are entirely synchronous. Never call them on the
  event loop; always `anyio.to_thread.run_sync`.
- Persist **both** the config dir (for `garmin_tokens.json`) and the HealthData
  tree. Retaining the raw JSON/FIT corpus means a schema rebuild needs no
  re-download.
- `Download.login()` returns **False** rather than raising on an auth failure.
  Unchecked, a sync carries on and "succeeds" having fetched nothing.
- `Download` builds its auth adapter with the default `mfa_prompt`, a blocking
  `input()` on stdin. `ingest.py` overwrites it with one that raises; in a
  container the default would hang a worker thread for ever.
- Importers are given `latest=True`, which means "files whose mtime is in the
  last 24h", not "the newest N". That is only safe because the download step has
  just rewritten those files. A file that failed to import on an earlier run is
  never retried — row counts in `/sync/status` are what make that visible, and
  there is no full-reimport endpoint yet.
- `download_days_overlap` is a hardcoded class attribute on `Download` (`3`),
  **not** read from config — `gc_config.download_days_overlap()` returns `None`
  with our config and is simply unused. Do not "fix" it.
- **GarminDB's own incremental rule never downloads today.** `garmindb_cli.py`
  computes `start = newest − 1 day`, `days = (today − start).days`, and every
  downloader loops `range(0, days)` — so the last day fetched is `start + days −
  1`, i.e. *yesterday*. `GarminConnectConfigManager.stat_start_date` has the same
  exclusive span for a first backfill. Last night's sleep carries **today's**
  calendarDate and this morning's heart rate is in today's wellness file, so
  neither arrived, and manual syncs could not help because each computed the same
  range. `incremental_range` counts through today inclusive and takes an explicit
  `floor`; `test_the_last_day_fetched_is_always_today` pins it. Do not "restore"
  GarminDB's arithmetic. (Backfill ranges `[floor, earliest)` are correctly
  exclusive — `earliest` is already held.)
- The plan's "today" is the **account's** calendar date (`_home_today`), never
  `date.today()`: the Dockerfile bootstraps `TZ=UTC`, which is a day ahead for a
  western evening and a day behind for an eastern morning. Before the first
  profile import stores a zone it falls back to the UTC date with a warning —
  acceptable only because this is a download range, not a timestamp conversion.
- `Download.get_monitoring` makes a `tempfile.mkdtemp()` per day, leaves that
  day's wellness zip in it, **never removes it**, and keeps only the last on
  `download.temp_dir`. Every sync re-fetches recent days, so `/tmp` grows without
  bound, faster the more often syncs run. `_fetch` therefore asks for monitoring
  one day per call and removes `download.temp_dir` after each.
- **plan.md §4b's `selectable=(table.time_col, column)` does not work.**
  `DbObject._s_query` hands its `selectable` to `session.query()` as a *single*
  entity, and SQLAlchemy 2.0 raises `ArgumentError` on a tuple there.
  `providers/garmindb/sampling.py`'s `period_rows`/`period_count` build the same query from
  `DbObject`'s public `during`/`after`/`before` expressions instead — still no
  raw SQL, still lightweight `Row` tuples rather than ORM instances.
- **`Analyze()` cannot be constructed until `attributes.measurement_system`
  exists.** Its `__init__` ends with `unit_strings[measurements_type(garmin_db)]`,
  and `measurements_type` returns an **unhashable** `UnknownEnumValue` when the
  row is missing — so that lookup raises a bare `TypeError`, not a `KeyError`.
  `ingest.analyze()` checks the precondition and skips with a warning: analyze
  only builds summary tables and views, none of which the serving layer reads, so
  skipping degrades nothing we serve, whereas raising would turn an otherwise
  complete sync into a reported failure. It is reachable in production right
  after a `/rebuild` on a corpus with no profile files.
- `GarminDbIngest` builds its DB handles **lazily**, precisely so that a corpus
  whose schema is too old to open can still be constructed — `rebuild()` has to
  exist in order to delete the files that would otherwise raise.

**Import scope and progress.**

- `config.py` is environment the operator sets and does **no I/O**;
  `preferences.py` is state the owner edits and persists. Do not move either into
  the other.
- `parse_preferences` (owner input) is strict ISO 8601; `load_preferences`
  (stored/env values) is lenient dateutil. Deliberate: dateutil reads
  `"03/01/2024"` as March 1st on a US default, and a start date silently three
  months off is not noticed until the download has already run.
- An unknown statistic is **refused** from form input and **dropped** from the
  stored file. Passing one through reaches `Statistics.from_string` deep inside
  GarminDB, far from anything that could explain it.
- `STAT_TABLES` in `providers/garmindb/ingest.py` is the single source of truth binding a
  statistic to its table. `download_plan` and `stat_coverage` both read it, so
  the gap shown on the page is measured against the same table the downloader
  uses to pick its range. Keep it that way or the two will disagree.
- The progress sink must never be the thing that fails a sync. It is
  fire-and-forget by design, and `run_once` clears the step in a `finally` — a
  progress line left standing reads as a sync that never finished.

**Serving a consumer, not a curl.**

- `get_sleep_sessions_merged` sends a `limit` and **no window**. An unbounded
  request over the whole corpus is therefore the *normal* case. `limit` has to
  bound the **work**, not just the answer: each session costs several monitoring
  queries, so "build every night, then keep 60" is thousands of queries, and the
  spec client's httpx timeout is 30s. `build_sleep_sessions` streams rows newest
  first and stops at `limit`; `TestBoundedWork` pins that.
- A cluster is only this night's if it overlaps `NIGHT_WINDOW` around `day`.
  Picking the *nearest* cluster with no candidacy check meant a night whose own
  events were missing silently wore its neighbour's window and stage timeline —
  the +/-24h search catches the previous night's tail.
- `health-dashboard` hard-wires several Oura-only metrics. Its "Today" panel
  renders only when `readiness_score` has a sample for today, and we deliberately
  do not serve that (GarminDB has no Training Readiness — see `registry.py`). Its
  heart-rate section hides itself entirely when `heart_rate` returns no samples in
  the **last 24 hours**, and its sleep panels drop any session whose
  `total_duration` is under 30 minutes. A blank dashboard is not by itself
  evidence of a fault on this side.

**Sync.**

- `sync.py` imports **no** `garmindb`: it drives an `Ingest` port that
  `providers/garmindb/ingest.py` implements. That is what keeps the whole sequence testable
  with no account and no network — keep it that way.
- The phase order is not optional: the profile importers must precede anything
  reading `measurement_system`, and `analyze` runs last.
- A sync that reports success but grows **no** table is the signature of
  GarminDB swallowing every per-file error. `SyncReport.changed` and `row_delta`
  exist for exactly that, and it is logged as a warning.
- A download sleeps a second per day and retries 5× with backoff, so it cannot
  be interrupted mid-call. The stop flag is checked between stats and phases;
  shutdown abandons the thread (`abandon_on_cancel=True`) rather than waiting
  minutes for it.
- `POST /sync` is fire-and-forget and returns `202` immediately — a first
  backfill runs for tens of minutes. Tests must poll `/sync/status`, not assume
  the next request sees a result.
- The schedule is **persisted, not slept**. Each forward sync writes its start to
  `sync_state.json` *before* any Garmin work; the loop waits until
  `last start + interval`, with a `STARTUP_GRACE_SECONDS` floor after a process
  start and `MIN_GAP_SECONDS` between runs. A crash-looping container therefore
  sees a recent start and waits out the interval — the protection the old
  sleep-first loop gave — while a healthy restart after a long gap syncs within a
  minute instead of waiting a whole interval. An unreadable state file counts as
  never synced rather than disabling scheduling.
- The interval is **re-read on every scheduling decision** from the owner's saved
  preferences, and `SyncEngine.reschedule()` wakes the loop, so a change on
  `/setup` applies at once rather than after the long wait already under way.
  `POST /setup/import` calls it.
- A **manual Sync now moves the schedule**; a **backfill does not**. A backfill
  fetches old history and says nothing about how fresh the corpus is.
- The form offers a **fixed set of intervals** (`SYNC_INTERVAL_CHOICES`, 15 min to
  24 h), so neither a typo nor a crafted POST can make the sync hit Garmin every
  minute. The operator's own `SYNC_INTERVAL_SECONDS` is always accepted even when
  it is not in the list, or re-saving the page would be refused. Fifteen minutes is
  already about as often as a watch syncs to Garmin Connect.

**Auth.**

- **Garmin's sign-in portal sits behind a Cloudflare bot challenge**, and a
  container on a datacenter IP frequently cannot pass it. garminconnect 0.3.11
  already exhausts the impersonation lane (curl_cffi TLS rotation, `ua_generator`,
  anti-WAF delays, a 5-strategy chain), so "impersonate harder" is not the fix.
- That challenge guards **only** the interactive credential exchange on
  `sso.garmin.com`. Refresh runs against `diauth.garmin.com` and data against
  `connectapi.garmin.com`; garminconnect refreshes proactively *precisely* to
  avoid the blocked endpoint (`__init__.py:703`), and GarminDB's own adapter tries
  the token store before credentials. **So a token minted once anywhere links the
  app for the life of its refresh token.** That is what `POST /setup/token` and
  `GarminAuthenticator.link_with_token` are for.
- `Garmin.login(tokenstore)` accepts **inline JSON** as well as a path, detected
  structurally by a leading brace (`__init__.py:182`). The import path uses that
  to verify a pasted token *before* persisting it -- a dead token written to disk
  is the exact "looks linked, every sync fails" trap.
- Do not "fix" the mint snippet on `/setup` to `g.garth.dump(...)`. That is the
  older library's spelling; 0.3.11 is `g.client.dump(...)`, and
  `test_the_mint_snippet_matches_the_installed_library` pins it.
- Two approaches that look promising and are **dead ends**: reverse-proxying the
  challenge through this app's domain (`cf_clearance` is scoped to `.garmin.com`
  and the challenge platform validates the host), and having the owner's browser
  drive the SSO with `fetch` (no CORS grant from `sso.garmin.com`).

- There is no OAuth consent flow for this API — `garminconnect` replays the
  owner's **real Garmin password** against `sso.garmin.com`. We log in ourselves
  and hand GarminDB the resulting token, so the password is never written to
  `GarminConnectConfig.json` at all; `credentials.password` stays empty.
- An unfinished MFA challenge lives on the `Garmin` client **instance**;
  `resume_login`'s `client_state` argument is ignored, so the object must be
  retained between the two requests.
- `return_on_mfa` is a **constructor** argument, not a `login()` argument.
  Omitting it falls through to `prompt_mfa`, a blocking `input()` on stdin that
  is fatal in a container.
- In `return_on_mfa` mode, `login()` returns early without setting
  `client._tokenstore_path` and **never dumps the token**, and `resume_login()`
  does not dump either. The app must call `client.client.dump(<token file>)`
  itself, or a login that looks successful writes nothing and every later sync
  re-prompts for MFA.
- `client.resume_login` clears the pending-MFA state in a `finally`, so a
  **rejected code consumes the challenge**. There is no retrying the code; the
  owner must re-enter their credentials.
- The token path must be exactly `<config_dir>/garmin_tokens.json`, which is
  what `GarminConnectConfigManager.get_token_store_file()` returns.
- A sign-in failure is a **one-shot flash** (`take_error()`), not part of
  `AuthStatus`. `AuthStatus.detail` is derived from the link state alone and must
  stay idempotent: anything sticky stored there is re-rendered on every later GET
  of `/setup`, so one mistyped password would accuse the owner forever. `/setup`
  consumes the flash; the pollable `/setup/status` only peeks.

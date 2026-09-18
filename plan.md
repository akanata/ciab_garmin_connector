# Garmin → Cloud in a Bottle Health Data Producer

## Execution phasing

**Approved. Doing now — nothing else:**

1. Copy this plan to `plan.md` at the repo root, for persistence across sessions.
2. Apply the `AGENTS.md` rewrite (§6) only. `CLAUDE.md` is a symlink to it and needs no separate change.

Everything else in this document — `pyproject.toml`, the manifest, `src/`, and the tests — is **deferred** until explicitly prompted. In particular, §6 documents the uv/just/pytest workflow as the project's intended commands, but no dependency or tooling change is made in this step; `requirements.txt` and the existing `.venv` are left untouched.

The open question from §7 (keep uv, or stay on pip + `requirements.txt`) does not block the AGENTS.md rewrite, but it does determine whether §6's command block is correct as written. Flagging it now so it can be settled before the next phase.

## Context

This repo is scaffolding only — `AGENTS.md`, `README.md`, `LICENSE`, `requirements.txt`, and a `.venv`. There is no Python source yet.

The goal is a **Cloud in a Bottle app that *provides* the health-data service**, sourcing from a Garmin account. Garmin's official API is gated behind a developer portal, so data comes from **GarminDB 3.9.0** (already pinned in `requirements.txt` and installed in `.venv`), which logs into Garmin Connect, downloads JSON + FIT files, and imports them into local SQLite databases.

The service therefore has two halves:

1. **Ingest** — drive GarminDB headlessly on a schedule to keep local SQLite fresh.
2. **Serve** — answer the spec's HTTP contract by mapping GarminDB rows into `health_data_service` attrs types.

This iteration covers **heart rate** (`specific_types.py`) and **sleep** (`sleep_types.py`). Workouts are explicitly out of scope.

### Decisions locked in

| Question | Decision |
|---|---|
| Garmin auth | Owner-only setup page; persist `garmin_tokens.json` to app data so MFA is a one-time prompt |
| Stack | Match the sibling `openhost_spec_mcp` app: uv + litestar + hypercorn + ruff + mypy strict + pytest; rewrite `AGENTS.md` |
| Extra metrics | `sleep_score`, `hrv_rmssd`, `readiness_resting_heart_rate` alongside `heart_rate` + sleep sessions |
| Sync trigger | Background interval loop **plus** an owner-only manual trigger and status endpoint |

---

## The contract we must satisfy

The spec ships **only a consumer client** (`health_data_service/client.py`) — there is no provider base class. The producer contract is read off that client:

| Method | Path | Query params | Response body |
|---|---|---|---|
| GET | `/v1/metrics` | — | `{"metrics": [MetricType, …]}` |
| GET | `/v1/time-series` | `metric, start, end, limit` | a **bare** `TimeSeries` object |
| GET | `/v1/sleep-sessions` | `start, end, limit` | `{"data": [SleepSession, …]}` |
| GET | `/v1/workouts`, `/v1/workouts/{id}` | — | out of scope; `{"data": []}` / 404 |

Types are **attrs + cattrs, not pydantic**. All timestamps must be **timezone-aware UTC**, serialized ISO 8601.

Status codes matter, because the client's `_fan_out` treats a non-200 as "this provider has nothing": **404** for an unknown `metric_id`, **200 with `samples: []`** for a known metric with no data in range, **413** for a window too large to scan.

### Service identity — verify before first deploy

The spec's client hardcodes `SERVICE_URL = "github.com/imbue-openhost/health-data-service-spec"` (the pre-rename identifier) even on the `cloud-in-a-bottle` repo's `main`. The router matches a provider's `service` string against the consumer's, so our manifest must declare **exactly that string**, not the `cloud-in-a-bottle` URL, or no consumer will ever route to us. `openhost_spec_mcp/openhost.toml` consumes the same `imbue-openhost` string, which corroborates it. Confirm against a live router before trusting it.

---

## Repository layout

> **Superseded by Addendum A** (end of this file). The layout below is the original design; `garmin/` is now `providers/garmindb/`, the boundary is a set of ports in `ports.py`, and the isolation rule is mechanically enforced. The *reasoning* below still holds — Addendum A records what changed and why.

The rule that keeps this maintainable: **`garmin/` is the only package that may import `garmindb`, `idbutils`, `fitfile`, or `sqlalchemy`.** Everything crossing that boundary is a `health_data_service` type or a stdlib type. That is what lets GarminDB be swapped for the real Garmin API later without touching the HTTP layer.

```
src/garmin_health/
  config.py         Settings (frozen attrs) from env. No I/O.
  timezones.py      TimeZonePolicy — the ONLY place naive↔aware conversion happens.
  serialization.py  cattrs converter, hooks, the three response envelopes.
  registry.py       METRICS: dict[str, MetricEntry]. Declarative; one block per metric.
  service.py        HealthDataService facade — the only thing routes/ imports.
  garmin_config.py  Renders/validates GarminConnectConfig.json.
  auth.py           Garmin login + MFA state machine.
  sync.py           download → import → analyze; the background loop.
  garmin/
    connection.py   GarminConnection: DbParams, the two DB handles, paired sessions.
    sampling.py     decimate(), column_series() builder factory, limit resolution.
    vocabulary.py   sleep-event string → SleepStage. Pure dict lookup.
    heart_rate.py   build_heart_rate()
    sleep.py        build_sleep_sessions() and helpers.
    daily.py        day-keyed series builders.
  routes/
    service.py      /v1/* — the spec surface.
    owner.py        /setup, /sync, /health.
tests/
  fixtures.py       build_fixture() → a real GarminDB SQLite in a tmpdir.
  test_*.py
```

Dependency direction is strictly `routes → service → registry → garmin/* → garmindb`, with `timezones.py` imported only by `garmin/*`. The `src/app/`, `src/components/ui/`, `src/hooks/` layout in the current AGENTS.md is Next.js convention and does not apply.

---

## 1. Manifest and platform integration

`cloudinabottle.toml` (the docs renamed it from `openhost.toml`; both sibling apps still use the old name — **check which the router accepts** and keep the other as a symlink if both are honoured):

```toml
[app]
name = "garmin-health"
version = "0.1.0"
description = "Serves Garmin Connect health data over the Cloud in a Bottle health-data service"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
health_check = "/health"
# No public_paths: /setup and /sync stay behind the router's owner gate, and
# /v1/* is reached only via the router's internal service proxy.
public_paths = []

[resources]
# GarminDB parses FIT files in-process; the import step is the memory peak.
memory_mb = 1024
cpu_cores = 0.5

[data]
# GarminConnectConfig.json, garmin_tokens.json, the downloaded JSON/FIT corpus,
# and the GarminDB SQLite files.
app_data = true

[[services.v2.provides]]
service = "github.com/imbue-openhost/health-data-service-spec"
version = "0.1.0"
endpoint = "/v1/"
```

**Environment** (`BOTTLE_*`; older deployments used `OPENHOST_*` — `fitpub_oh` still reads `$OPENHOST_APP_DATA_DIR`, so `config.py` should read `BOTTLE_APP_DATA_DIR` with an `OPENHOST_APP_DATA_DIR` fallback). We don't need `BOTTLE_APP_TOKEN`; as a provider the router calls *us*.

**Headers.** The router strips any client-supplied `X-OpenHost-*` before stamping its own, so they are trustworthy. `X-OpenHost-Is-Owner: true` gates `/setup` and `/sync` in-app (belt and braces with `public_paths`). `X-OpenHost-Consumer-Name`/`-Id`/`-Permissions` arrive on service-proxied `/v1/*` calls — log the consumer id, don't gate on it initially.

**Verify with the test harness:** service calls arrive as an internal router→app proxy to `<endpoint>/<rest>`, not as external browser traffic, so `/v1/*` should not need `public_paths`. If the harness shows otherwise, the fix is an in-app guard accepting *either* `X-OpenHost-Is-Owner` or a present `X-OpenHost-Consumer-Id`, with `public_paths = ["/v1"]`.

---

## 2. Garmin authentication (owner-only setup flow)

### There is no OAuth consent dialog, and there cannot be one

`garminconnect` 0.3.11 authenticates by **replaying the user's password against Garmin's SSO web form**: it POSTs email + password to `sso.garmin.com/sso/signin` (also `/portal/api/login` and a mobile flow), scrapes a service ticket out of the response, and exchanges it at `diauth.garmin.com/di-oauth2-service/oauth/token`. OAuth2 tokens are the *output* of that exchange, not the mechanism. Garmin's actual OAuth API is what sits behind the developer portal — the reason this project uses GarminDB at all.

**Consequence to surface, not bury: this app handles the owner's real Garmin Connect password.** Say so on the `/setup` page and in the README. Mitigations, all of which the design already implies: owner-gated behind the router, credentials never leave the app, and the password is blanked from disk the moment a token exists (below). If that trade is unacceptable, there is no alternative short of Garmin granting developer-portal access.

### Login mechanics

From `garmindb/garmin_connect_auth_adapter.py`:

- `Download.login()` first tries the token cache at `<config_dir>/garmin_tokens.json`. If that works, **no credentials and no MFA are needed** — this is the steady state.
- Otherwise it reads `credentials.user`/`credentials.password` from `GarminConnectConfig.json` and calls `garminconnect.Garmin(...).login(tokenstore)`.

`Garmin.login()` accepts **`return_on_mfa=True`**, which returns `("needs_mfa", None)` instead of prompting, paired with **`resume_login(client_state, mfa_code)`**. Use this — it is a clean non-blocking flow, and it avoids GarminDB's default `prompt_mfa`, a blocking `input()` on stdin that is fatal in a container. Reach it by passing a `garmin_factory` to `GarminConnectAuthAdapter`, or by driving `garminconnect` directly for the login step and letting `Download` pick up the resulting token file.

> **Constraint:** `resume_login`'s `client_state` parameter is *ignored* (`client.py:1608` takes `_client_state`); the pending-MFA state lives on the `Garmin` **instance**. So the same client object must be held in memory between the two calls, and an unfinished MFA challenge does not survive a process restart. That is acceptable — it is a short interactive flow — but it rules out a stateless two-request design.

### Flow

`auth.py` owns an in-memory state machine (the login attempt does not survive a restart; the *token* does):

1. `GET /setup` — owner-only page: linked / not-linked / awaiting-MFA / needs-reauth, plus last sync status.
2. `POST /setup/credentials` — email + password. Calls `login(return_on_mfa=True)` via `anyio.to_thread.run_sync` (**GarminDB and `garminconnect` are entirely synchronous — never call them on the event loop**). On `("needs_mfa", …)`, retain the client instance and flip to `awaiting_mfa`.
3. `POST /setup/mfa` — calls `resume_login(...)` on that retained instance; `garminconnect` writes `garmin_tokens.json`.
4. State becomes `linked`; the sync loop unblocks.

**Blank the password back out of the config file once `garmin_tokens.json` exists**, so no long-lived plaintext Garmin password sits on disk. On token expiry, flip to `needs_reauth` and re-prompt.

---

## 3. GarminDB configuration and the sync engine

### Config file

`GarminConnectConfigManager(config_dir)` reads `<config_dir>/GarminConnectConfig.json` and **calls `sys.exit(-1)`** if it is missing or malformed. `garmin_config.py` must render and validate the file *before* constructing the manager — never let a fresh install take that exit path.

```json
{
  "db": { "type": "sqlite" },
  "garmin": { "domain": "garmin.com" },
  "credentials": { "user": "", "password": "", "secure_password": false },
  "data": { "sleep_start_date": "...", "monitoring_start_date": "...",
            "rhr_start_date": "...", "hrv_start_date": "..." },
  "directories": { "relative_to_home": false, "base_dir": "<APP_DATA>/HealthData" },
  "enabled_stats": { "monitoring": true, "sleep": true, "rhr": true, "hrv": true,
                     "steps": false, "itime": false, "weight": false, "activities": false }
}
```

- `relative_to_home: false` + an absolute `base_dir` puts everything under app data. **This matters**: `GarminConnectConfigManager.homedir` and `temp_dir` are *class attributes evaluated at import*, so setting `HOME` after `import garmindb` has no effect.
- Disable `activities`/`weight`/`steps` — out of scope, and activities are by far the slowest download.
- `_date`-suffixed values are auto-parsed by a `json.load` object hook into `datetime.date`; the example format is US `M/D/Y`.
- Every directory getter mkdirs as a side effect.

### `TZ` and the timezone strategy

This is the single highest-risk part of the project. GarminDB stores **naive** datetimes on **four different clocks**:

| Column(s) | Writer | The naive value means |
|---|---|---|
| `monitoring_hr.timestamp`, `monitoring_hrv_value`, `monitoring_rr`, `monitoring_pulse_ox` | `monitoring_fit_file_processor.py` via `fit_file.utc_datetime_to_local()` | **device-local**, using the offset recorded in the FIT file itself |
| `sleep_events.timestamp` (both FIT and JSON paths) | `sleep_fit_file_processor.py:38`, `import_monitoring.py:216` | **device-local** |
| `sleep.start`, `sleep.end` | `import_monitoring.py:196` → `Conversions.epoch_ms_to_dt` = `datetime.fromtimestamp(ms/1000.0)` | **the importing container's `TZ`** |
| `sleep.day`, `resting_hr.day`, `hrv.day`, `daily_summary.day` | `_parse_date` | naive local midnight of the Garmin calendar date |

So with the Docker default `TZ=UTC`, `sleep.start`/`end` land in UTC while `sleep_events` land in the user's local time — a multi-hour skew between two tables written by the *same* importer.

**Strategy: one clock, learned skew.** `timezones.py` owns a frozen `TimeZonePolicy` and is the only module allowed to convert:

```python
def to_utc(self, naive_local):      # every value we emit goes through this
    return naive_local.replace(tzinfo=self.home_tz).astimezone(timezone.utc)

def to_naive_local(self, aware):    # every query bound goes through this
    """SQLAlchemy's SQLite DATETIME bind_processor formats .year/.hour/... and
    DISCARDS tzinfo -- an aware bound silently compares as naive wall clock
    against naive local rows, with no error."""
    return aware.astimezone(self.home_tz).replace(tzinfo=None)

def sleep_column_to_utc(self, naive_import):
    return self.to_utc(naive_import - self.import_offset)
```

**Scope of `import_offset` — two columns, not a global conversion.** Everything we emit is aware UTC, but the path there differs:

| Columns | Conversion |
|---|---|
| `monitoring_hr`, `monitoring_hrv_value`, `monitoring_rr`, `sleep_events`, and every `*.day` | `to_utc()` — device-local wall clock via `home_tz`. **No offset applied.** |
| `sleep.start`, `sleep.end` **only** | `sleep_column_to_utc()` — subtract `import_offset` first, because only these two were rendered in the importing container's `TZ`. |

And because sleep windows prefer `sleep_events` whenever events exist (§4d), `sleep_column_to_utc` runs only for event-less sessions. `import_offset` is a targeted repair for two columns on a fallback path, not the main conversion.

**Resolving `home_tz`**, in order: `GARMIN_HOME_TZ` env → `Attributes.get_string(garmin_db, 'time_zone')` **validated through `ZoneInfo()`** → raise `TimeZoneUnresolved` at startup. The validation is required because two importers write that key in incompatible formats under last-writer-wins: `GarminPersonalInformation` writes IANA (`import_monitoring.py:347`), while `fit_file_processor.py:214` writes a stringified FIT enum. **Do not fall back to the container's local tz** — a silent wrong answer corrupts every timestamp we emit and won't be noticed for months; failing at boot with "set `GARMIN_HOME_TZ`" is a five-second fix.

**Learning `import_offset`** makes the design self-correcting. `sleep.start` and the first `sleep_events` row of the same night describe the same instant on two different clocks, so their naive difference *is* the importer's offset. Probe the last ~90 nights, keep differences that are a whole 15-minute step, and take the mode. Overridable by `GARMIN_IMPORT_TZ`. Computed once at startup.

**But the primary rule makes this mostly moot**: for sleep sessions we prefer the `sleep_events`-derived window whenever events exist, because those share a clock with `monitoring_hr` and `monitoring_hrv_value` — exactly the consistency that matters when slicing sub-series. `import_offset` is only needed for event-less sessions.

Still set the container `TZ` to the account's home zone so future imports are self-consistent. Bootstrap: default UTC, read `time_zone` after the first sync, persist it, use it thereafter.

**Assumption this rests on:** the account has one home timezone and the watch was in it. Wrong for travellers; the failure mode is a fixed hours-shift on travel days. Document it; per-row offsets simply are not in the schema. Likewise DST — `.replace(tzinfo=...)` resolves the repeated autumn hour via `fold=0`. Two nights a year, on data Garmin itself recorded ambiguously. Accept, document, and **test the chosen behaviour so it doesn't drift**.

### Sync sequence

Replicates `garmindb_cli.py` in-process on a worker thread. The ordering is not optional — the profile import must run first so `measurement_system` exists before anything reads it.

```
download:  login → get_daily_summaries → get_monitoring → get_sleep → get_rhr → get_hrv
import:    GarminUserSettings / GarminPersonalInformation / GarminSocialProfile
           → measurement_system = Attributes.measurements_type(GarminDb(dbp))
           → GarminSummaryData → GarminHydrationData
           → GarminMonitoringFitData.process_files(MonitoringFitFileProcessor(dbp, plugin_manager))
           → GarminSleepData (JSON), falling back to GarminSleepFitData if no JSON
           → GarminRhrData → GarminHrvData
analyze:   Analyze(cfg).summary(); .create_dynamic_views()
```

**Incremental range** — reuse the CLI's rule (`garmindb_cli.py:63`): per stat, take `Table.latest_time(db, col)`, start one day before it, run to today; with no rows yet, fall back to the configured `<stat>_start_date`. This is what makes routine syncs cheap.

**Pitfalls to code around:**
- `MonitoringFitFileProcessor.write_file` dereferences `plugin_manager` unconditionally — pass a real `PluginManager(cfg.get_plugins_dir(), dbp)`; `None` raises `AttributeError`.
- `JsonFileProcessor._process_files` **swallows every per-file exception**. A totally failed sync looks successful. Compare `latest_time()` / `row_count_for_period()` before and after, and surface that in `/sync/status`.
- `Download.__get_stat` sleeps 1s per day and retries 5× with backoff. A multi-year backfill is tens of minutes. The loop must be cancellable.
- `latest=True` on importers means "files with mtime in the last 24h", not "the newest N".
- `DB.__init__` runs `create_all` plus a version check that raises `RuntimeError` on mismatch. A GarminDB upgrade means `DB.delete_db(dbp)` then re-import from the retained JSON/FIT files — **no re-download needed**, which is why the raw corpus stays in app data.

### Loop and endpoints

- Lifespan-managed `asyncio.Task`: sleep `SYNC_INTERVAL_SECONDS` (default 6h), run one sync, repeat. Skip unless state is `linked`. Hold a lock so a manual trigger can't overlap the scheduled run.
- `POST /sync` (owner-only) — kick one now; `202` if already running.
- `GET /sync/status` (owner-only) — link state, last start/finish, duration, per-table row counts and `latest_time`, last error.
- `GET /health` — the router's probe. Return `200 {"status": "ok"}` whenever the process is up. **Do not** fail it on an unlinked account or a stale sync, or the router will restart a container that is working correctly.

---

## 4. The serving layer

### 4a. `GarminConnection` — the gateway

Two facts from `idbutils/db.py` and `idbutils/db_object.py` force this design:

1. `DB.__init__` runs `create_all()` **and** a `version_check()` that writes `_attributes` rows — **constructing a DB object is a write.** A read-only bind mount of `DBs/` kills the service at startup with `attempt to write a readonly database`.
2. `DbObject.setup(db)` — which installs `cls.time_col`, `cls.col_names`, etc. — is **only** called from `DB.init_table()` inside `DB.__init__`. `MonitoringHeartRate.get_for_period(...)` raises `AttributeError` if no `MonitoringDb` was ever constructed in the process.

So `GarminConnection` constructs both `GarminDb` and `MonitoringDb` once at startup (translating `RuntimeError` into a typed `GarminSchemaMismatch`), resolves the `TimeZonePolicy`, and exposes a **paired** session context manager — `garmin.db` and `garmin_monitoring.db` are separate files with separate engines, so "one session for multi-table work" means one session *per database*:

```python
@contextmanager
def sessions(self) -> Iterator[tuple[Session, Session]]:
    with self.garmin_db.managed_session() as g, \
         self.monitoring_db.managed_session() as m:
        yield g, m
```

`managed_session()` is `sessionmaker(engine, expire_on_commit=False).begin()`, so ORM instances stay usable after the block.

**Connection lifecycle — no crash-and-restart anywhere.** The connection is constructed once at startup and explicitly managed thereafter:

| Situation | Behaviour |
|---|---|
| Fresh install, never synced | `GarminDb(params)` **creates** an empty valid schema, so every query returns no rows. `/v1/*` serves empty results (`200` with `samples: []`, `{"data": []}`), not `500`. `/setup` says not-linked. |
| Steady state, sync writing concurrently | SQLAlchemy pools connections against the same files. Register a `busy_timeout` (~5s) connect listener so readers wait out the writer's lock instead of raising `database is locked`. |
| DB rebuilt (GarminDB version bump → `DB.delete_db` + reimport) | Pooled handles point at the **deleted inode** and would silently serve stale data. The sync engine calls `GarminConnection.reset()` — `engine.dispose()` on both, then reconstruct — under a lock that excludes readers for the swap. This is the one case that genuinely needs coordination. |
| Schema version mismatch at startup | `DB.__init__` raises `RuntimeError` → typed `GarminSchemaMismatch`. **Start anyway, degraded**: `/health` stays `200` (so the router doesn't restart-loop a container whose only problem is a stale schema), `/v1/*` returns `503`, and `/setup` + `/sync/status` show the fault with a rebuild button. The owner fixes it through the UI. |
| Transient `OperationalError` on a query | One `reset()` and retry, then fail the request. Do not retry indefinitely. |

The startup path must tolerate the DB directory being absent entirely — `get_db_dir()` mkdirs as a side effect, and constructing the DB objects populates the schema, so first boot before any sync is a normal, serviceable state rather than an error.

### 4b. Metric registry

```python
@attr.s(auto_attribs=True, frozen=True)
class MetricEntry:
    descriptor: MetricType          # verbatim what /v1/metrics emits
    series_cls: type[TimeSeries]    # spec class instantiated for /v1/time-series
    build: Builder                  # (conn, start_utc, end_utc, limit) -> list[Sample]
    provenance: str                 # "garmin_monitoring.db:monitoring_hr.heart_rate"
    probe: Callable[[GarminConnection], bool] = _has_any_row
```

`build` comes from one generic factory in `garmin/sampling.py`, so an entry is declarative data rather than code:

```python
def column_series(table, column, *, db: Literal["garmin", "monitoring"],
                  cast=float, skip_none=False) -> Builder:
    """Half-open [start, end) on table.time_col, ascending, decimated to `limit`.
    Uses only idbutils.DbObject helpers -- no raw SQL."""
    def build(conn, start_utc, end_utc, limit):
        lo, hi = conn.tz.to_naive_local(start_utc), conn.tz.to_naive_local(end_utc)
        with conn.sessions() as (g, m):
            rows = table.s_get_for_period(
                g if db == "garmin" else m, lo, hi,
                selectable=(table.time_col, column),      # Row tuples, not ORM objects
                not_none_col=column if skip_none else None)
        return [Sample(timestamp=conn.tz.to_utc(ts), value=cast(v))
                for ts, v in decimate(rows, limit)]
    return build
```

The four entries this iteration:

| `metric_id` | `series_cls` | Source | Note |
|---|---|---|---|
| `heart_rate` | `HeartRate` | `garmin_monitoring.db:monitoring_hr.heart_rate` | Integer column → `Sample[float]`, cast |
| `hrv_rmssd` | `HRV_RMSSD` | `garmin_monitoring.db:monitoring_hrv_value.hrv` | Override `unit="ms"` — spec defaults it to `None`, but it is RMSSD in ms |
| `sleep_score` | `SleepScore` | `garmin.db:sleep.score` | day-keyed; `skip_none=True` |
| `readiness_resting_heart_rate` | `ReadinessRestingHeartRate` | `garmin.db:resting_hr.resting_heart_rate` | Override `unit="bpm"`; fall back to `daily_summary.rhr` |

`/v1/metrics` filters on `entry.probe(conn)` with a short TTL cache — `list_metrics_merged` uses the catalog to decide what to request, so advertising an empty metric costs the consumer a wasted round trip.

> **Construct every spec type with keyword arguments.** attrs' `_transform_attrs` drops overridden base attributes and appends the subclass's, so overridden fields move to the *end*: `HeartRate.__init__` is `(source, metric_id="heart_rate", …, samples=[])` — `source` first — and `Duration.__init__` is `(value, source, …)`. Positional construction silently produces garbage the moment the spec adds a field.

### 4c. Heart rate and the `limit` policy

`monitoring_hr` is ~1 row/2 min ≈ 720/day ≈ 263k/year, so this needs care on two axes.

`selectable=(timestamp, heart_rate)` makes `_s_query` select two columns instead of the mapped entity, returning lightweight `Row` tuples rather than constructing 263k ORM instances — the difference between ~50 MB and ~500 MB on a one-year request. Guard the window with `s_row_count_for_period` and raise `WindowTooLarge` → **413** above `MAX_ROWS_SCANNED`.

**`limit` means even decimation across the requested window**, preserving first and last:

```python
def decimate(rows, limit):
    """Keep `limit` evenly-spaced rows. Selection, not aggregation: every
    emitted value is a real reading."""
    n = len(rows)
    if limit is None or n <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    return [rows[round(i * (n - 1) / (limit - 1))] for i in range(limit)]
```

Justification: a consumer writing `TimeSeriesRequest(metric="heart_rate", start=week_ago, end=now, limit=200)` asked for a week and said 200 points is enough resolution. *Most-recent-N* would return the last 6.7 hours, silently discarding the `start` they explicitly passed; *truncate-from-start* returns the same-sized slice of the least useful end. The requested window is the primary selector; `limit` is a resolution knob. The spec's own client corroborates this: `get_time_series_merged` concatenates and sorts but — unlike the sleep and workout merges — **does not re-apply `limit`**, treating a series as "the shape of this window", never "the newest N".

**This is pure selection — nothing is interpolated, resampled, or averaged.** Every emitted `Sample` is a real Garmin reading carrying its own real recorded timestamp; `decimate` is "drop samples" in the signal-processing sense, not "resample onto a grid". `Sample(timestamp, value)` asserts "the reading at this instant", and a bucket mean would carry a timestamp at which it was never measured.

The honest cost: a short excursion between two kept samples disappears — a 2-minute HR spike is invisible at `limit=200` over a week. Consumers needing peaks narrow the window or raise the limit; at `MAX_LIMIT = 50_000` a full day (720 rows) is never decimated in practice, so this only bites on multi-week requests.

Defaults in `config.py`: `DEFAULT_LIMIT = 5_000` (applied when `limit is None`, so an unbounded request can't OOM), `MAX_LIMIT = 50_000`, `MAX_ROWS_SCANNED = 1_000_000`.

*Later optimization, not for v1:* push decimation into SQL with `func.row_number().over(...)` filtered on `(rn-1) % stride == 0` — SQLAlchemy Core over the mapped table, needs SQLite ≥ 3.25. Only once `row_count_for_period` shows the fetch is the bottleneck.

### 4d. Sleep sessions

Iterate `Sleep.s_get_for_period(g, lo, hi)`, build one `SleepSession` per row, sort by `start` descending to match the client's merge. `source = "garmin"` — vendor-level, matching the spec's own `"oura"`/`"apple_watch"` examples; not `"garmindb"` (our ingest tool, an implementation detail) and not `"garmin_connect"` (data can arrive from FIT files off the watch).

**`Container.id`** = `f"garmin:sleep:{row.day.date().isoformat()}"`. Namespaced because the client documents ids as provider-unique. Derived from `day`, **not** `start`/`end`, deliberately: `day` is the primary key and is stable across re-imports, whereas `start`/`end` are the tz-suspect columns — correcting `import_offset` later would change every id and break consumers' stored references.

**Window** (`start`/`end` are non-optional in `Container` but nullable in `sleep`), in priority order:

1. **Events present → derive from events**, even when `start`/`end` are non-null: `start = min(ev.timestamp)`, `end = max(ev.timestamp + ev.duration)`. Same device-local clock as `monitoring_hr`, so the window and its sub-series are guaranteed self-consistent. That consistency is the whole reason for preferring it.
2. No events, both columns present → `sleep_column_to_utc()` on each.
3. No events, one column, `total_sleep + awake > 0` → derive the missing endpoint.
4. **Otherwise skip the session** with a WARNING. Also skip when `end <= start`.

Skipping over fabricating: there is no honest default for a sleep window, and a synthesized one looks valid, merges into `get_sleep_sessions_merged`, and silently poisons downstream aggregates. A gap in the list is detectable; a plausible lie is not.

Query events over a wide window (`day ± 24h`, since Garmin's `calendarDate`-to-bedtime semantics aren't worth guessing at), cluster on gaps > 3h, and pick the cluster nearest `row.start` when available, else the one whose midpoint is closest to `day + 03:00` local.

**`stages`** — `garmin/vocabulary.py` maps both event vocabularies, since the JSON and FIT paths use different enums (both store `enum.name`, so values are always lowercase snake_case):

```python
_STAGE_BY_EVENT = {
    # fitfile.field_enums.SleepActivityLevel — FIT path
    "unknown": UNKNOWN, "awake": AWAKE, "light_sleep": LIGHT,
    "deep_sleep": DEEP, "rem_sleep": REM,
    # garmindb.import_monitoring.SleepActivityLevels — JSON, non-REM device
    "more_awake": AWAKE,
    # garmindb.import_monitoring.RemSleepActivityLevels — JSON, REM device
    "unmeasurable": UNKNOWN,
    # legacy rows; still queried by SleepEvents.get_wake_time()
    "wake_time": AWAKE,
}
```

Unmapped tokens fall back to `UNKNOWN` **and log a one-shot WARNING per distinct token** — that is what makes new Garmin vocabulary visible instead of silently degrading.

Intervals are `[timestamp, timestamp + duration)` per `IntervalSample`'s contract, with `duration` through `fitfile.conversions.time_to_secs()`. Drop zero-length rows (`duration` is `NOT NULL DEFAULT time.min`). **Overlaps: clamp to the next event's start** — for the FIT path `duration` is literally `next_ts - this_ts`, so overlap only arises from clock adjustments or duplicate rows, and a monotone timeline is what any consumer summing stage durations needs. **Gaps: leave them** — a gap means Garmin recorded nothing, and `UNKNOWN` filler would be inventing data. Offer `Settings.fill_stage_gaps` (default `False`) for consumers needing contiguity. Emit `None`, not an empty `SleepStages`, when there are no intervals — they are different claims.

**Sub-series** `heart_rate` and `hrv`: slice `monitoring_hr` and `monitoring_hrv_value` to the window. An 8-hour night is ~240 and ~96 rows, so no pagination is needed; still cap at `MAX_SESSION_SUBSERIES = 2_000` through the same `decimate()`.

**Duration scalars** via `time_to_secs(t) / 60` into `Duration`:

| Spec field | Column |
|---|---|
| `total_duration` | `sleep.total_sleep` |
| `deep_sleep_duration` / `light_sleep_duration` / `rem_sleep_duration` | `sleep.deep_sleep` / `light_sleep` / `rem_sleep` |
| `awake_time` | `sleep.awake` |

All five are `NOT NULL DEFAULT time.min`, so **"no data" and "zero minutes" are indistinguishable**. Policy: if `total_sleep == 0`, treat the whole breakdown as absent and recompute from the stage intervals (the same arithmetic as `SleepEvents.get_day_stats`, `garmin_db.py:281`), emitting `None` if that also yields nothing. If `total_sleep > 0`, a zero stage is real data (a night with genuinely no REM) — emit `Duration(value=0.0, …)`. Document the asymmetry.

**Session scalars:**

- `average_heart_rate` / `lowest_heart_rate` ← `MonitoringHeartRate.get_stats(mon, lo, hi)`, which already passes `ignore_le_zero=True`. Test `is not None`, not truthiness.
- `average_hrv` ← the **`monitoring_hrv_value` average over exactly `[start, end)`**, not `Hrv.last_night_avg`. Three reasons: it is computed over the identical window as the `hrv` sub-series and `average_heart_rate`, so a consumer averaging `session.hrv.samples` gets the same number back; `hrv.day` is Garmin's own day-keying, which silently attaches the *wrong night's* HRV to any session whose calendar attribution is off by one; and `Hrv.last_night_avg` is `Integer`, losing sub-ms precision. Fall back to `MonitoringHrvStatus.last_night_average`, then `Hrv.last_night_avg` — the tables come from **different ingest paths** (`monitoring_hrv_*` FIT-only, `hrv` JSON-only) and neither is universally present. Log when the window-inconsistent fallback fires.
- `average_breath` ← `sleep.avg_rr`, falling back to the `monitoring_rr` window average. Deliberately the opposite preference to HRV, and that's fine *because* `SleepSession` has no respiration sub-series for it to disagree with — nothing constrains it to our window, so matching what the Connect app shows wins.
- `sleep_score` ← `Score(value=float(sleep.score))`. `Score.unit is None`, correct for a 0–100 score.

**The four with no direct source:**

- **`time_in_bed` — derive** as `(end - start)` in minutes. Garmin's detected sleep window *is* the in-bed span. Document that it is "detected sleep window", so shorter than true bed time by the pre-sleep reading period.
- **`efficiency` — derive** as `100 * total_sleep_min / time_in_bed_min`, only when both are `> 0`. Textbook definition, matches Oura, so it merges sensibly. If the ratio exceeds 100, **clamp to 100 and log a WARNING with the raw value** — a >100% efficiency is precisely the canary that `total_sleep` and the event-derived window disagree, i.e. the tz skew is still present. Log it, don't swallow it.
- **`latency` — `None`.** Latency is lights-out → sleep onset, and GarminDB has no lights-out marker: `sleep.start` *is* the detected onset and the first event is already a sleep stage, so any derivation is structurally `0` or noise. Garmin Connect does expose `sleepLatencySeconds`, but GarminDB 3.9.0 reads exactly ten keys from `dailySleepDTO` (`import_monitoring.py:196–209`) and that is not one. Worth an upstream PR.
- **`restless_periods` — `None` by default**, with an opt-in `Settings.derive_restless_periods` that counts `AWAKE` intervals strictly interior to the session. Off by default because Oura's "restless periods" is a *movement*-derived count, not an awakening count — different physiological quantities. `Count` carries `unit=None` and `display_name="Count"`, so a consumer cannot tell which one they got, and `_fan_out` merges providers into one list. A wrong-semantics number under another vendor's metric id is exactly what corrupts cross-provider merges; `None` costs the consumer nothing.

### 4e. Metrics deliberately NOT served

Worth recording so nobody re-litigates it:

- **All eight `sleep_score_*` sub-scores** — Garmin's payload has them, but GarminDB imports only `sleepScores.overall.value` (`import_monitoring.py:186`). No column to read.
- **`readiness_hrv_balance`** — mapping `hrv.status`'s 4-value ordinal to a 0–100 score is an invention, and `get_time_series_merged` would concatenate our synthesized numbers with Oura's real ones into one series mixing incomparable scales.
- **`readiness_score`** — GarminDB doesn't import Training Readiness. Body Battery is 0–100 and superficially close but measures energy reserve, not recovery.
- **`temperature_deviation`, `temperature_trend_deviation`, `readiness_body_temperature`** — GarminDB 3.9.0 has no skin-temperature column anywhere.
- **The remaining `Readiness*` family** — Oura-proprietary composites with no Garmin analogue.

Body battery (`daily_summary.bb_charged`/`bb_max`/`bb_min`) is a README priority and *is* available. It has no spec type, but `/v1/metrics` is a free-form list, so it can be advertised later under vendor-extension ids (`body_battery_charged`, …) that unknowing consumers simply ignore. Out of scope this iteration.

### 4f. Serialization

```python
converter = cattrs.preconf.json.make_converter()
converter.register_unstructure_hook(SleepStage, lambda v: v.value)
converter.register_unstructure_hook(MetricKind, lambda v: v.value)
```

Those two hooks are cheap insurance: `SleepStage` is a `(str, Enum)`, and cattrs' `MultiStrategyDispatch` checks `_single_dispatch` first, where `(str, identity)` is registered — singledispatch resolves by MRO, so `unstructure()` returns the **enum member**, not `"deep"`. `json.dumps` renders it correctly by accident (str subclass), but `orjson` or any `type(v) is str` check downstream will not.

Two behaviours confirmed against cattrs source rather than assumed:

- **`IntervalSample` round-trips with no custom hook.** `SleepStages.samples` is `list[IntervalSample[SleepStage]]`, a parametrized generic; `_single_dispatch` raises on it, dispatch falls through to `_function_dispatch` → `gen_unstructure_attrs_fromdict`, which emits all three fields including `end_timestamp`.
- **`datetime` → ISO 8601** via the preconf `register_unstructure_hook(datetime, ...isoformat())`.

**The one real trap:** the client registers `structure_hook(Sample, _structure_sample)`, which resolves by MRO. `TimeSeries.samples` is declared as bare `list[Sample]`, so on `/v1/time-series` that hook fires for an `IntervalSample` too and **silently discards `end_timestamp`**.

> **Never put an interval-valued metric in `METRICS`.** `sleep_stages` is reachable only through `SleepSession.stages`, where the parametrized-generic path preserves `end_timestamp`.

Enforce it with a test, not a comment — see §5.

Assert at the boundary that every emitted timestamp is aware UTC, because `isoformat()` on a naive value emits an offsetless string and the client's `fromisoformat` would hand the consumer a naive datetime.

Three envelopes, matching `client.py` exactly:

```python
def metrics_payload(ds):       return {"metrics": [converter.unstructure(d) for d in ds]}
def time_series_payload(s):    return converter.unstructure(s)          # BARE, no envelope
def sleep_sessions_payload(x): return {"data": [converter.unstructure(s) for s in x]}
```

`Count.value` stays `int` (spec is `ScalarMetric[int]`); everything else casts to `float`.

**Day-keyed samples** (`sleep_score`, `readiness_resting_heart_rate`) go through the same `to_utc`, so local midnight in Denver becomes `T07:00:00+00:00` — not `00:00Z`, which would be a second inconsistent convention placing the sample on the wrong local day. Document that a consumer bucketing by *UTC* date will be off by one in western timezones; that is inherent, since the spec has no date-valued sample type.

### 4g. Coexisting with Oura / Apple Watch providers

The user may run several health providers at once. **Reconciling them is the consumer's job by design, not ours** — the spec's fan-out returns everything: `get_time_series_merged` concatenates all providers' samples and sorts by timestamp, `get_sleep_sessions_merged` sorts by `start` descending, and **neither dedupes**. Two watches produce two `SleepSession` objects for the same night, each correct according to its own device. We must not try to be clever about that.

What we owe the merge is only this, and all three are already in the design:

1. **A consistent `source`** (`"garmin"`) on every object, so a consumer *can* filter or group by provider.
2. **Namespaced `Container.id`** (`garmin:sleep:<date>`), so ids never collide with another provider's.
3. **No metric_id whose semantics differ from other providers'.** This is the abstract principle behind the concrete `restless_periods` decision in §4d: emitting a Garmin awakening-count under Oura's movement-derived id is exactly what corrupts a merged list, because `Count` carries no unit or qualifier that would let a consumer tell them apart.

**One genuine spec hazard, for §8:** `source` is a field on `TimeSeries`, **not on `Sample`**. So `get_time_series_merged` interleaves Garmin and Oura heart-rate readings into a single sorted list with provenance stripped — a consumer computing a daily average double-counts every minute both devices were worn, and has no way to detect it. Sleep sessions are unaffected (`source` is per-session). Nothing we can fix provider-side; report it.

---

## 5. Testing

The enabling fact: **`GarminDb(DbParams(db_type='sqlite', db_path=tmpdir))` builds a complete, valid, empty `garmin.db` from nothing**, because `DB.__init__` runs `create_all`. No Garmin account, no checked-in binary fixture, no schema SQL to keep in sync across GarminDB upgrades. The "constructing a DB is never read-only" wart is a liability in production and a gift in tests.

`tests/fixtures.py` exposes `build_fixture(*, import_tz=HOME)` — the `import_tz` parameter simulates the TZ of the container that ran the import, so passing `timezone.utc` reproduces the `sleep.start`-vs-`sleep_events` skew on demand. It writes one 8-hour night: a `Sleep` row (7h total → 87.5% efficiency), 8 contiguous `SleepEvents`, a `RestingHeartRate` row, 240 `MonitoringHeartRate` rows at 2-minute spacing, and 96 `MonitoringHrvValue` rows at 5-minute spacing. Variants on the same base: `no_events()`, `null_start()`, `unknown_event_vocabulary()`, `fit_vocabulary()` vs `json_rem_vocabulary()`, `zero_total_sleep()`, `overlapping_events()`.

Three constraints the fixture must respect: construct **both** DB objects before touching any table classmethod (`setup()` runs inside `DB.__init__`, and without it `cls.time_col` doesn't exist); use `with db.managed_session() as s: Table.s_insert_or_update(s, …)` for bulk rows, since `insert_or_update(db, …)` opens a session per call; and write **naive** datetimes, mirroring production.

| Test | Asserts |
|---|---|
| `test_timezones` | Build with `import_tz=UTC` and `import_tz=HOME`; `SleepSession.start` is the **same aware UTC instant** in both. The single test that proves the whole strategy. |
| `test_naive_bounds` | Patch `s_get_for_period` and assert every captured bound has `tzinfo is None`. Guards the silent SQLite mis-filter. |
| `test_heart_rate_limit` | 240 rows, `limit=10` → exactly 10, first and last preserved, strictly increasing, every value a `float`. |
| `test_sleep_stages` | 8 events → 8 non-overlapping contiguous intervals; `"banana"` → `UNKNOWN` **and** `assertLogs` catches the one-shot warning; overlapping fixture truncates at the successor's start. |
| `test_window_fallbacks` | Each of the four window branches, including "session absent + WARNING logged". |
| `test_derived_scalars` | `time_in_bed == 480.0`; `efficiency ≈ 87.5`; `latency is None`; `restless_periods` `None` by default and `Count(value=1)` when opted in. |
| `test_serialization_contract` | Unstructure → `json.dumps` → `json.loads` → **structure through the spec's own `client.converter`**, then assert `stages.samples[0]` is an `IntervalSample` with `end_timestamp` intact and `start` round-trips exactly. |
| `test_registry` | Ids unique and equal to their key; `descriptor.unit` matches the series class; and no entry ever yields an `IntervalSample` (§4f). |

`test_serialization_contract` is the highest-value test here: an executable contract against the real consumer, so a cattrs upgrade that changes dispatch order breaks the build instead of production.

Auth-state-machine tests drive the MFA flow with a fake `garmin_factory`, no network.

---

## 6. AGENTS.md rewrite

`CLAUDE.md` is a symlink to `AGENTS.md`, so this file is the single source of instruction for every future session — and it currently mandates semicolons, single quotes, named exports, Tailwind, Shadcn/radix, and React hooks for a Python project, alongside dev commands that don't work (`venv/` vs the real `.venv/`, bare `pip install`, `python -m http.server 8080` as the dev server for an API).

Replace with the real stack:

```
Environment:      uv sync          (manages the standard .venv/; `source .venv/bin/activate`
                                   still works — the current `venv/` path is simply wrong)
Dev server:       just run         → uv run hypercorn garmin_health.app:app --bind 0.0.0.0:8080 --reload
Lint/format/type: just check       → ruff check --fix . && ruff format . && uv run mypy
Tests:            just test        → uv run pytest -x
Build image:      just build       → docker build -t garmin-health .
```

Style, matching `openhost_spec_mcp/pyproject.toml`: 4-space indent, double quotes, ruff `line-length = 119` with `E,F,B,UP,I,PLC0415` and isort `force-single-line`, mypy `strict = true` plus `follow_untyped_imports = true` (the spec package ships annotations but no `py.typed`).

Fill in the empty **Critical Guardrails** section — these are exactly what belongs there:

- Container `TZ` must be the Garmin account's home timezone; `GARMIN_HOME_TZ` overrides (§3).
- Query bounds must be **naive**; aware bounds silently mis-filter in SQLite.
- Construct spec types with **keyword arguments only** — attrs reorders overridden fields (§4b).
- Never serve an interval-valued metric on `/v1/time-series` (§4f).
- `GarminConnectConfigManager` calls `sys.exit(-1)` on a bad config; validate first.
- GarminDB importers swallow per-file exceptions; verify syncs by row count.
- `MonitoringFitFileProcessor` requires a non-`None` `PluginManager`.
- Constructing a GarminDB `DB` object **writes**; `DBs/` cannot be mounted read-only.
- After any DB rebuild, call `GarminConnection.reset()` — pooled handles otherwise point at the deleted inode and serve stale data silently.
- The app handles the owner's **real Garmin password** (no OAuth consent flow exists for this API); blank it from `GarminConnectConfig.json` once `garmin_tokens.json` exists.
- An unfinished MFA challenge lives on the `Garmin` client **instance** — `resume_login`'s `client_state` argument is ignored, so the object must be retained between the two requests.
- Never block the event loop with GarminDB calls; always `anyio.to_thread.run_sync`.
- Persist both the config dir (for `garmin_tokens.json`) and the HealthData tree.

---

## 7. Dependencies

### Why uv, and what it does *not* change

To be precise, since this is a change to existing habits and the case for it is narrower than it first looks:

- **uv does not replace `venv`.** `uv sync` creates and manages an ordinary `.venv/`; `source .venv/bin/activate` works exactly as before. The AGENTS.md correction is `venv/` → `.venv/` (the wrong path today), not the removal of a step.
- **The repo is already on uv.** `.venv/pyvenv.cfg` records `uv = 0.12.6`. Introducing pip would be the change here, not keeping uv.
- **Nothing in this design requires it.** pip installs the git dependency fine.

The one substantive argument: the spec is an **unpinned git dependency** (`@main`), so without a lockfile it silently drifts between a developer's machine and a container build, and `main` moving would break a deploy with no diff to point at. `uv.lock` + `uv sync --frozen` makes the image reproducible, which is what `openhost_spec_mcp/Dockerfile` already does. The secondary argument is one toolchain across both CiaB apps.

**If you'd rather stay on pip + `requirements.txt`, that works** — the cost is pinning the spec to a commit SHA by hand and re-pinning deliberately. Say so and I'll write the plan that way; everything else is unaffected.

### Package metadata

`pyproject.toml` replaces `requirements.txt` (a flat `pip freeze` of GarminDB's tree, which notably lacks `attrs`, `cattrs`, and the spec package — all three are absent from `.venv` today):

```toml
requires-python = "==3.12.*"       # matches the sibling app and GarminDB's floor (>=3.12)
dependencies = [
  "litestar>=2.24", "hypercorn>=0.18",
  "attrs>=24.1", "cattrs>=24.1",   # >=24.1: FunctionDispatch takes_converter
  "GarminDb==3.9.0",
  "health-data-service @ git+https://github.com/cloud-in-a-bottle/health-data-service-spec@main",
]
```

with `[tool.hatch.metadata] allow-direct-references = true`.

Dockerfile follows `openhost_spec_mcp/Dockerfile`: `python:3.12-slim`, `uv` from `ghcr.io/astral-sh/uv:latest`, `apt-get install git` (uv shells out to real git for the spec dependency), `uv sync --frozen --no-dev`, `CMD [… hypercorn garmin_health.app:app --bind 0.0.0.0:8080]`. Use **slim, not Alpine** — `curl_cffi` ships glibc binary wheels and `garminconnect` prefers it for TLS fingerprint impersonation, degrading to `requests` and less reliable logins without it. Set `ENV TZ` per §3. GarminDB needs no non-Python system binaries at runtime.

The current `.venv` is Python **3.14**; the target is 3.12 to match the sibling app and the Dockerfile. `uv sync` rebuilds it.

---

## 8. Known spec/Garmin mismatches to report upstream

1. **Naps are unrepresentable** — `sleep.day` is the primary key and `insert_or_update` overwrites, so at most one session per calendar day, though `SleepSession`'s docstring says "one night or nap". Needs a GarminDB schema change.
2. **Duplicate `metric_id` in the spec** — `TemperatureDeviation` (a `ScalarMetric`) and `BodyTemperatureDeviation` (a `TimeSeries`) both declare `"temperature_deviation"`; a single `/v1/metrics` list can disambiguate them only by `kind`.
3. **Unitless quantities that have units** — `HRV_RMSSD.unit` and `ReadinessRestingHeartRate.unit` default to `None` but the values are ms and bpm. We override; an upstream fix would make providers agree.
4. **`sleepLatencySeconds` not imported** by GarminDB 3.9.0 — worth a PR there, and it would let us populate `latency`.
5. **`hr_max` during sleep is computed and discarded** — `MonitoringHeartRate.get_stats` returns it, but `SleepSession` has no field (`HeartRateMax` exists only in `workout_types.py`).
6. **Merged time series lose provenance** — `source` is on `TimeSeries`, not `Sample`, so `get_time_series_merged` interleaves multiple providers' readings with no way to attribute them (§4g). Moving `source` onto `Sample`, or having the merge return per-provider series, would fix it.

---

## Verification

1. **Unit** — `just test`. Mapping tests run against the tmpdir fixture (no account, no network); auth tests use a fake `garmin_factory`.
2. **Contract** — `test_serialization_contract` round-trips every response through the spec's own converter. This is the real check that we match the client.
3. **Local** — `just run`, then `curl localhost:8080/v1/metrics`, `'…/v1/time-series?metric=heart_rate&start=…&end=…&limit=100'`, `'…/v1/sleep-sessions?start=…&end=…'`.
4. **Against the real client** — point `openhost_spec_mcp` at this app via its `consumes` entry and confirm `HealthDataClient.get_sleep_sessions()` and `.get_time_series()` deserialize without error. This is the acceptance test that matters.
5. **Containerized** — the `openhost[test-harness]` `OpenhostStack` fixture (see `openhost_spec_mcp/tests/conftest.py`) builds the Dockerfile under podman behind the real router. Confirms `/v1/` is reachable through the service proxy and `/setup` is owner-gated.
6. **Live sync** — link a real Garmin account through `/setup`, watch `/sync/status` row counts climb, re-run step 3 against real data, and spot-check one night's sleep session against the Garmin Connect app. Specifically check that `efficiency` is not clamped and that session `start` matches local bedtime — that pair is the end-to-end proof the timezone strategy holds.

---

## Addendum A — The provider boundary (2026-09)

Parts 1–8 describe a single-provider app: GarminDB, scraped on a schedule. This addendum records what changed when that assumption was removed. It supersedes **Repository layout**; the rest of the plan still stands.

### Why

Terra, ROOK and Spike all offer free Garmin access, and all three deliver it by **webhook** rather than polling. Triaging the existing seam found the read path well isolated and the write path not isolated at all: `Settings` carried GarminDB's disk layout and polling knobs, `registry.py` and `service.py` were typed against `GarminConnection`, the HTTP exception classes lived inside the provider package, and roughly 60% of `routes/owner.py` was Garmin's. Nothing enforced the isolation rule the docs already claimed.

### What the aggregators actually do (verified from their docs)

- **Terra** — hosted widget/OAuth to link; webhooks *carry normalised data* in Terra's own schema; HMAC `terra-signature`; must 2xx within 8 s, so accept and process asynchronously. Historical data is a REST request whose results also arrive by webhook.
- **Spike** — hosted redirect to link; webhooks are *notifications only* (`record_change`, `X-Body-Signature`) and the app must then fetch. Backfill after linking is automatic, announced by webhook.
- **ROOK** — OAuth authorizer URL; data pulled by date; webhooks notify.

**"Polling vs. webhook" is therefore the wrong axis.** What all four have in common is *provider-owned acquisition* — a timer, a signed payload, or notify-then-fetch — writing to a *provider-owned store*, read by a *provider-owned reader* that yields spec types. None of the GarminDB readers, the SQLite corpus or the timezone machinery would be reused by any aggregator. The ports are shaped around that, not around how often anything runs.

`source` stays `"garmin"` for every provider: it names the device the data came off, not the route it took to get here.

### Decisions

1. **One app, provider chosen by config.** Both providers live under `providers/`, selected by `HEALTH_PROVIDER` (default `garmindb`). Same app name, URL, app data and metric ids.
2. **An honest boundary plus enforcement, with no second real provider** until one is wanted. The seam is proved instead by a test-only `FakeProvider` and a contract test run against both it and the real provider.
3. **`garmin/` → `providers/garmindb/`**, as its own mechanical commit.

### The ports (`ports.py`)

```python
SOURCE = "garmin"

class LinkState(StrEnum):        # PENDING, not AWAITING_MFA: an aggregator
    NOT_LINKED; PENDING; LINKED; NEEDS_REAUTH   # links by redirect, not MFA

LinkStatus(state, account, detail)   # detail derived from state ALONE
SetupView(link, error, serving_fault, provider_error, today)

class HealthReader(Protocol):    # fault, metrics, has_any_data,
    ...                          # sleep_sessions, close

class AccountLink(Protocol):     # status, peek_error, take_error, unlink

class Provider(Protocol):
    name; display_name; link
    open_reader() -> HealthReader          # called once, by the app's lifespan
    lifespans() -> list[Lifespan]          # garmindb: the sync loop
    owner_routes() / public_routes()       # mounted by the app, gated or not
    async status() -> dict                 # MUST carry running + progress
    async render_setup(view) -> str        # its half of /setup
    subscribe_data_changed(callback)
```

`MetricEntry.build`/`.probe` are **bound** — no connection argument — which is what lets `registry.py` stay free of any provider's types.

### Layout

`app.py` wires; `providers/build_provider()` is the only place a concrete provider is named; `GarminDbProvider` owns the engine, the authenticator, the connection and its own half of the owner page. The page's generic shell is `setup_page.py`, deliberately *not* under `routes/`, because a provider re-renders the whole page when one of its own forms fails validation and a provider may not import `routes.*`.

Dependency direction: `routes → service → ports/registry/limits/errors → spec types`, and `app → providers → providers/garmindb`.

### Enforcement

`TID251` in `pyproject.toml` bans `garmindb`, `garminconnect`, `idbutils`, `fitfile`, `sqlalchemy`, `garth` and the string `garmin_health.providers.garmindb`, with `per-file-ignores` exempting the provider package, the selector and `tests/providers/`. A global banned-api table cannot express the *reverse* rule, so `tests/test_boundary.py` walks the import graph with `ast` — catching lazy imports inside functions — asserts that nothing under `providers/` imports the app layer, and cross-checks the ruff table so the two cannot drift.

### Deliberate wire changes

`/setup/status` reports `state: "pending"` rather than `"awaiting_mfa"`, and the account field is `account` rather than `email`. `/sync/status` gained a provider-owned `corpus` block (`db_dir`, `timezone`, `timezone_error`) and `SyncEngine.status()` no longer reports `link_state`, which now comes from the provider's `AccountLink` alone.

### What a second provider should cost

The falsifiable claim this whole exercise rests on: adding one is a new package under `providers/`, plus one string in `PROVIDER_NAMES`. It should touch **no existing source file**. If it turns out to need more, the boundary is still in the wrong place — and that is worth finding out cheaply.

### Deliberately not done

Three follow-ups were scoped and left unstarted. They are recorded here because the working document that held them has been removed now that Phases 1–6 have landed.

- **A uniform scan cap.** `MAX_ROWS_SCANNED` currently guards one of the four read paths. Applying it to `build_resting_heart_rate` and `build_sleep_sessions` as well is a **behaviour change** — windows that succeed today would start answering 413 — so it deserves a deliberate decision rather than being swept up as tidying. `check_scan_cap` already reads the cap at call time, so the tests for it can monkeypatch. `heart_rate._window_samples` should stay uncapped either way: a per-night count per sub-series doubles every session's query cost, and the night window already bounds the work.
- **Lifting sleep assembly.** `clean_intervals`, `stage_totals`, `resolve_durations`, `efficiency`, `restless_periods` and `window_from_intervals` are assembly *policy* rather than anything GarminDB-specific, and could move to a provider-agnostic `sleep_assembly.py` behind pure-function tests. Worth doing only when a second provider actually arrives — until then it is churn, and the 57 corpus tests are the regression net that makes it safe later.
- **`MAX_SESSION_SUBSERIES` as a parameter** of `session_heart_rate`/`session_hrv`, rather than a module-level constant read directly.

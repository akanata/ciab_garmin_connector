# Provider boundary refactor: encapsulate GarminDB and polling under `providers/garmindb/`

**Status:** accepted 2026-09-15, not started. Accepted against `main` at `106f8d6`
(clean tree). This document is the design of record for the refactor; `plan.md` remains
the design of record for the app itself and gets an addendum in Phase 5.

## Context

The app serves Garmin health data over the health-data-service spec. Today it acquires
that data by driving GarminDB, a polling scraper that writes a SQLite corpus. The owner
may later swap to a webhook-based aggregator (Terra, ROOK, Spike) and asked for a triage
of how robust the boundary between "main application logic" and the GarminDB-specific
code really is, on the premise that *anything related to polling must be encapsulated in
the GarminDB-specific directory*.

Decisions the owner has made:
1. **One app, provider chosen by config.** GarminDB and a future aggregator both live
   in this app under `providers/`. Same app name, URL, app_data and metric ids; the
   provider is selected by an env var, default `garmindb`.
2. **Honest boundary + enforcement, no second real provider.** Move every polling and
   GarminDB module under the provider package, define the ports, lift generic logic out,
   add a ruff banned-import rule, and prove the seam with a test-only `FakeProvider`.
3. **Rename `garmin/` → `providers/garmindb/`** as its own mechanical commit.

Commits are the owner's; each phase below ends green and is a natural commit boundary.
TDD is mandatory: in every phase the tests named under "tests first" are written and
seen to fail before the business logic changes.

### What the aggregators actually look like (verified from their docs)
- **Terra**: hosted widget/OAuth redirect to link; webhooks *carry normalised data* in
  Terra's own schema; HMAC `terra-signature`; must 2xx within 8 s (accept, process
  async); historical data is a REST request whose results *also* arrive via webhook.
- **Spike**: hosted redirect to link; webhooks are *notifications only* — the app must
  then fetch via Spike's API; backfill is automatic after linking, announced by webhook.
- **ROOK**: OAuth authorizer URL; data pulled by date; webhooks for notification.

So "poll vs. webhook" is the wrong axis. The generic model is **provider-owned
acquisition** (a timer, a signed payload, or notify-then-fetch) writing to a
**provider-owned store**, read by a **provider-owned reader** that yields spec types.
None of the GarminDB readers, the SQLite corpus, or the timezone machinery would be
reused by any of the three. A webhook provider also needs *ungated* routes and a
`public_paths` manifest entry, and links via redirect rather than credentials/MFA.
`source` stays `"garmin"` for every provider — the device source is still Garmin.

## Triage: where the boundary stands today

**The read path is well isolated; the write path is not isolated at all.**

Outside `garmin/`, the generic side uses only twelve names from it: a connection handle
(`GarminConnection(settings)`, `.fault`, `.reset()`, `.close()`, `.has_any_data()`,
`.status()`), four metric builders + four probes, `build_sleep_sessions`, three
exceptions mapped to 400/413/503, and `GarminDbIngest`. `serialization.py`,
`routes/service.py` and the *shape* of `registry.py` survive a swap untouched.

| Leak | Where | Direction |
|---|---|---|
| `garmindb` imported outside the package (violates the stated rule) | `garmin_config.py:20` | out |
| `garminconnect` imported outside the package; **not declared** in pyproject (transitive via GarminDb) | `auth.py:38` | out |
| `registry.py` / `service.py` typed against `GarminConnection` (`Builder`/`Probe` defined in `garmin/sampling.py:43-47`) | `registry.py:44-45,59,61`; `service.py:53` | out |
| HTTP exception classes defined in the provider package | `routes/service.py:42-44,50` | out |
| `Settings` encodes GarminDB's disk layout and polling knobs (17 symbols) | `config.py:57-118` | out |
| `preferences.py` is GarminDB's statistic vocabulary + download scope | whole file | out |
| `sync.py` is the scheduler + a scraper-shaped `Ingest` port; added for testability, never designed as a provider abstraction | whole file | out |
| ~60 % of `routes/owner.py` (credentials/MFA/token, mint snippet, import scope, coverage, `/sync`, `/backfill`, `/rebuild`) | `routes/owner.py` | out |
| `garmin/ingest.py` imports **upward** into `sync.py` (incl. private `_no_progress`, `incremental_range`) and `preferences.STAT_LABELS` | `garmin/ingest.py:49-59` | in |
| `garmin/sleep.py` reaches `conn.settings.fill_stage_gaps` / `derive_restless_periods` | `garmin/sleep.py:415,496` | in |

Generic logic buried inside the provider package: `resolve_limit`, `decimate`,
`InvalidLimit`, `WindowTooLarge` (`garmin/sampling.py:50-109`); the `MAX_ROWS_SCANNED`
guard applied on **one of four** read paths (latent bug); `vocabulary.py`'s warn-once
registry; most of `sleep.py`'s assembly policy; `SOURCE = "garmin"` triplicated.

Nothing enforces the isolation rule (no TID rule, no import-linter, no test); the mypy
override makes `garmindb.*` `Any`, so a leaked object typechecks silently. Tests: 19
files, 652 collected; ~470 bound to a real corpus via `tests/fixtures.py:build_fixture`;
the only port double is `FakeIngest`; `create_app` has no serve-side seam.

Verified: ruff 0.16.6 `TID251` with one `banned-api` table catches `garmindb`,
`garminconnect`, `sqlalchemy` (incl. submodules) and the first-party prefix
`garmin_health.providers.garmindb`; `per-file-ignores` exempts the package. Repeated
`--config` flags do not merge — declare the table once. `garminconnect==0.3.11` is the
lock's version; `garth` is not in the lock. The rename touches 88 import sites in 22
files. `_notify_corpus_changed` runs on the **event loop** (called directly from the
async `run_once`) — `app.py:83`'s "worker thread" docstring is wrong.

## Target architecture

```
src/garmin_health/
  app.py             create_app(*, settings=None, provider=None); /health; lifespans; routers
  config.py          Settings(app_data_dir, provider) + serving limits + ConfigError + path_from/flag_from
  ports.py           SOURCE, LinkState, LinkStatus, SetupView, HealthReader, AccountLink, Provider, Lifespan
  errors.py          ProviderUnavailable (→503), ProviderNotReady(ProviderUnavailable)
  limits.py          resolve_limit, decimate, InvalidLimit, WindowTooLarge, check_scan_cap
  progress.py        SyncStep, ProgressSink, no_progress
  registry.py        MetricEntry (bound Builder/Probe), metric_entry(); re-exports SOURCE
  service.py         HealthDataService(reader: HealthReader)
  serialization.py   unchanged
  warn_once.py       WarnOnce registry                         (Phase 6)
  routes/service.py  /api/v1/* — exception map over generic errors
  routes/fragments.py STYLE, BUSY_SCRIPT, button(), progress_line()
  routes/owner.py    owner_guard + shell: GET / and /setup, GET /setup/status, POST /setup/unlink, GET /sync/status
  providers/__init__.py  build_provider(settings) — the ONLY importer of providers.garmindb
  providers/garmindb/
    settings.py      GarminDbSettings + from_env(app_data_dir, env)   (no I/O)
    config_file.py   ex garmin_config.py
    auth.py          GarminAuthenticator (implements AccountLink)
    preferences.py   ex preferences.py, whole
    sync.py          TableStat, StatCoverage, SyncPhase, SyncReport, Ingest, incremental_range, SyncEngine
    ingest.py, timezones.py, timezone_probe.py, connection.py, sampling.py (SQLAlchemy half),
    heart_rate.py, daily.py, sleep.py, vocabulary.py
    registry.py      metrics_for(conn) -> dict[str, MetricEntry]  (the four entries, bound)
    reader.py        GarminDbReader(connection) — implements HealthReader (+ provider-only reset)
    owner.py         fragments + gated handlers: /setup/import /setup/credentials /setup/mfa /setup/token /sync /backfill /rebuild
    provider.py      GarminDbProvider — implements Provider; owns engine, authenticator, connection, sync-loop lifespan
tests/
  conftest.py, fakes.py (FakeReader, FakeLink, FakeProvider), test_*.py   — generic contract tests only
  providers/test_contract.py   TestProviderContract over FakeProvider and GarminDbProvider
  providers/garmindb/          conftest.py, fixtures.py, fakes.py (FakeGarmin, RecordingFactory, FakeIngest, ReportingIngest), test_*.py
```

Dependency direction: `routes → service → ports/registry/limits/errors/progress ←
providers/garmindb → garmindb`; `app → providers (selector) → providers/garmindb`.
Nothing under `providers/garmindb/` imports `app`, `service` or `routes.*`.
`GARMIN_*` and `SYNC_INTERVAL_SECONDS` become variables read **only** by the garmindb
provider; the core reads `BOTTLE_APP_DATA_DIR`/`OPENHOST_APP_DATA_DIR` and
`HEALTH_PROVIDER` (default `garmindb`).

## Ports (`src/garmin_health/ports.py`)

```python
SOURCE = "garmin"

class LinkState(StrEnum): NOT_LINKED, PENDING, LINKED, NEEDS_REAUTH   # PENDING replaces AWAITING_MFA

@attrs.frozen
class LinkStatus:            # ex auth.AuthStatus
    state: LinkState
    account: str | None = None   # ex email
    detail: str | None = None    # derived from state only; never a sticky error
    def as_dict(self) -> dict[str, str | None]: ...

@attrs.frozen
class SetupView:             # what the shell hands a provider when rendering /setup
    link: LinkStatus
    error: str | None            # the consumed one-shot flash
    serving_fault: str | None    # HealthDataService.fault
    provider_error: str | None   # a provider form's validation message re-rendered inline
    today: dt.date

# in registry.py — bound: no connection argument
Builder = Callable[[dt.datetime | None, dt.datetime | None, int | None], list[Sample[Any]]]
Probe = Callable[[], bool]

@runtime_checkable
class HealthReader(Protocol):
    @property
    def fault(self) -> str | None: ...
    @property
    def metrics(self) -> Mapping[str, MetricEntry]: ...
    def has_any_data(self) -> bool: ...
    def sleep_sessions(self, start, end, limit: int) -> list[SleepSession]: ...
    def close(self) -> None: ...

@runtime_checkable
class AccountLink(Protocol):
    def status(self) -> LinkStatus: ...
    def peek_error(self) -> str | None: ...
    def take_error(self) -> str | None: ...
    async def unlink(self) -> LinkStatus: ...

Lifespan = Callable[[Litestar], AbstractAsyncContextManager[None]]

@runtime_checkable
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def link(self) -> AccountLink: ...
    def open_reader(self) -> HealthReader: ...              # called once by the app's serving lifespan
    def lifespans(self) -> list[Lifespan]: ...              # garmindb: [sync_loop]
    def owner_routes(self) -> list[HTTPRouteHandler]: ...   # mounted under owner_guard by the app
    def public_routes(self) -> list[HTTPRouteHandler]: ...  # ungated; each path must be in openhost.toml public_paths
    async def status(self) -> dict[str, Any]: ...           # merged into /sync/status; MUST carry "running": bool, "progress": dict|None
    async def render_setup(self, view: SetupView) -> str: ...  # HTML fragment placed inside the shell
    def subscribe_data_changed(self, callback: Callable[[], None]) -> None: ...
```

Decisions:
- **`MetricEntry.build/probe` are bound.** The garmindb registry becomes `metrics_for(conn)`
  using `functools.partial` over the existing unbound `hr.build_heart_rate(conn, …)` etc.;
  `providers/garmindb/sampling.py` keeps conn-typed `ConnBuilder`/`ConnProbe` aliases.
- **No `reset`/`status` on `HealthReader`.** Reset is provider-internal
  (`GarminDbProvider._corpus_changed` resets its own connection *before* firing subscribers).
  `HealthDataService.status()` becomes exactly `{available, fault, metrics}`; `db_dir`,
  `timezone`, `timezone_error` move under `GarminDbProvider.status()["corpus"]`.
- **No per-provider `source`.** One constant, `ports.SOURCE`, re-exported by `registry.py`.
- **Exceptions subclass, no translation layer.** `GarminUnavailable(ProviderUnavailable)`,
  `TimeZoneUnresolved(ProviderNotReady)`. `service.py`'s 200/503 rule catches
  `ProviderNotReady`; routes map `ProviderUnavailable`; messages (e.g. "Set GARMIN_HOME_TZ") survive.
- **`AWAITING_MFA → PENDING`** is the one deliberate wire change (`/setup/status.state`,
  `/sync/status.link_state`, the page's `data-state`). The MFA wording lives in
  `LinkStatus.detail`; the shell's headline per state is generic ("Linked as …",
  "Link pending.", "Not linked yet.", "No longer linked; sign in again.").
- **Progress types stay generic** (`{label, done, total}` is the wire shape `BUSY_SCRIPT`
  depends on). Everything else in `sync.py` is scraper vocabulary and moves.
- Owner handlers read `state.provider` via a typed `_provider(state) -> GarminDbProvider`
  helper (mirrors today's `_authenticator(state)`); the provider exposes `.settings`,
  `.authenticator`, `.engine`.

## Symbol moves (old → new)

| Old | New |
|---|---|
| `garmin/*` | `providers/garmindb/*` (Phase 1, `git mv`) |
| `garmin_config.py` | `providers/garmindb/config_file.py` |
| `auth.py` | `providers/garmindb/auth.py`; `LinkState`/`AuthStatus` → `ports.LinkState`/`LinkStatus` (`email`→`account`) |
| `preferences.py`, `timezones.py` | `providers/garmindb/` whole; `TimeZoneUnresolved(ProviderNotReady)` |
| `sync.py` | `providers/garmindb/sync.py` minus `SyncStep`, `ProgressSink`, `_no_progress` → `progress.py` (`no_progress` public) |
| `config.py`: `garmin_domain, home_tz, import_tz, backfill_start_date, sync_interval_seconds, fill_stage_gaps, derive_restless_periods, config_dir, garmin_config_file, token_file, sync_state_file, preferences_file, health_data_dir, db_dir, is_cn, SUPPORTED_DOMAINS, DEFAULT_SYNC_INTERVAL_SECONDS, DEFAULT_BACKFILL_START_DATE, _zone_from, _sync_interval_from, _backfill_start_date_from` | `providers/garmindb/settings.py: GarminDbSettings` + `from_env(app_data_dir, env)`. **On-disk paths unchanged** (`GarminDb/`, `HealthData/`, `sync_state.json`, `import_preferences.json`) — no data migration |
| `config.py` keeps | `Settings(app_data_dir, provider)`, `DEFAULT_APP_DATA_DIR`, four serving limits, `ConfigError`, public `path_from`/`flag_from` |
| `garmin/sampling.py: resolve_limit, decimate, InvalidLimit, WindowTooLarge` | `limits.py` (+ `check_scan_cap(rows, cap=MAX_ROWS_SCANNED)`) |
| `garmin/sampling.py: Builder, Probe` | `registry.py` (bound); provider keeps `ConnBuilder`/`ConnProbe` |
| `registry.py: METRICS, _entry` | `providers/garmindb/registry.py: metrics_for(conn)`; `registry.py: metric_entry()` public. The "deliberately not served" docstring moves with the provider registry |
| `SOURCE` ×3 | `ports.SOURCE` (Phase 6) |
| `service.py: HealthDataService(connection, metrics=…)` | `HealthDataService(reader, *, clock, catalog_ttl)`; `.connection` removed; new `.fault` |
| `app.py: _default_ingest_factory, SHUTDOWN_GRACE_SECONDS, serving_layer's GarminConnection, sync_loop` | `providers/garmindb/provider.py` |
| `app.py: create_app(settings, authenticator, ingest_factory)` | `create_app(*, settings=None, provider=None)` |
| `routes/owner.py`: `PASSWORD_NOTICE, MINT_SNIPPET, _interval_options, _import_scope_form, _token_import_form, _coverage_table, _describe_last_sync, _describe_schedule, submit_import_scope, submit_credentials, submit_mfa, submit_token, trigger_sync, trigger_rebuild, trigger_backfill` | `providers/garmindb/owner.py` (the rebuild control is GarminDB's) |
| `routes/owner.py: STYLE, BUSY_SCRIPT, _button, _progress_line` | `routes/fragments.py` |
| `routes/owner.py: owner_guard, PageView→SetupView, shell of _render, setup_page, setup_status, unlink, sync_status, _serving_fault` | stays; `_serving_fault` reads `service.fault` |
| `garmin/vocabulary.py: _warn_once, _warned, _lock, reset_unknown_event_log` | `warn_once.py: WarnOnce` (Phase 6) |
| `garmin/ingest.py` upward imports | sibling imports inside the provider package |

## Test moves

| Current | Destination |
|---|---|
| `fixtures.py`, `fakes.py` | `tests/providers/garmindb/` (a new generic `tests/fakes.py` appears in Phase 2) |
| `test_connection, test_daily, test_heart_rate, test_sleep, test_timezone_probe, test_vocabulary, test_ingest` | `tests/providers/garmindb/`, imports only |
| `test_sampling.py` | `TestColumnSeries`/`TestHasRows` → provider; `TestDecimate`/`TestResolveLimit` → `tests/test_limits.py` |
| `test_registry.py` | 27–99 rewritten generically against `metric_entry()` with dummy callables; 102–149 → provider, using `metrics_for(conn)` |
| `test_service.py` | rewritten generically against `FakeReader` (same test names/contracts); corpus `TestDegradedStates` cases → `tests/providers/garmindb/test_reader.py` |
| `test_service_routes.py` | generic (FakeReader mounted directly on `v1_router`): metrics/time-series format, 400/404, sleep envelope, `TestWorkouts`, `TestDegraded`, `TestNeverSynced`, `TestManifestRoutingContract`; provider: window sample counts, 413, `TestRebuildFromTheOwnerPage`, one end-to-end never-synced case |
| `test_auth, test_garmin_config→test_config_file, test_preferences, test_sync, test_timezones` | provider |
| `test_config.py` | generic keeps env precedence, defaults, Dockerfile tests, serving limits, new `HEALTH_PROVIDER` tests; provider `test_settings.py` gets derived paths, token-file pin, `is_cn`, interval, zones, backfill date, flags |
| `test_owner_routes.py` | generic keeps `/health`, gating (incl. forged header), page renders per state, flash consumed-vs-peeked, unlink, `/sync/status` envelope; provider gets credentials/MFA/token/mint snippet/import scope/coverage/backfill/rebuild/`POST /sync`/interval/progress display (`"awaiting_mfa"` → `"pending"`) |
| `test_serialization.py` | unchanged |

New generic tests: `test_limits.py`, `test_errors.py`, `test_progress.py`, `test_app.py`,
`test_boundary.py`, `test_warn_once.py`; shared `tests/providers/test_contract.py`.
`tests/providers/__init__.py` and `tests/providers/garmindb/__init__.py` are required
(duplicate basenames such as `test_service_routes.py`).

`tests/providers/garmindb/conftest.py` consolidates the seven copies of `corpus_settings`:
`settings` → `GarminDbSettings(app_data_dir=tmp_path/"appdata")` (overrides the root
fixture by scope); `corpus_settings`, `corpus` (`build_fixture(nights=3, heart_rate=True,
hrv=True, sleep_score=82, resting_hr=True, avg_rr=14.5)`); `garmindb_provider(settings,
*, ingest=None, linked=False)` (wraps `GarminAuthenticator(settings,
garmin_factory=RecordingFactory(needs_mfa=False))` and `lambda: ingest or FakeIngest()`;
`linked=True` plants the token file); `app_for(provider)`; `client`, `broken_client`
(`db.version` tampering), `wait_for_sync`.

## Phases — each ends with ruff, format, mypy and pytest green; tests are written first

### Phase 1 — Mechanical rename. Risk: low.
1a. `git mv src/garmin_health/garmin src/garmin_health/providers/garmindb`; add
`providers/__init__.py` (docstring only); rewrite `garmin_health.garmin.` →
`garmin_health.providers.garmindb.` (88 sites); update the package docstring and the two
`# noqa: PLC0415` lazy imports in `app.py`; sed the path mentions in AGENTS.md.
1b. `git mv` the seven corpus-bound test files + `fixtures.py` + `fakes.py` into
`tests/providers/garmindb/`; add the two `__init__.py`; fix `from tests.fixtures` /
`from tests.fakes` in the not-yet-moved `test_owner_routes.py`, `test_service_routes.py`,
`test_sync.py`, `test_auth.py`; create `tests/providers/garmindb/conftest.py` with
`corpus_settings`/`corpus` only.
Pinned by: the whole suite, count unchanged (652).

### Phase 2 — Generic lifts and the `HealthReader` seam. Risk: medium (every serving path retyped; no behaviour change intended).
2a tests first: `tests/test_limits.py` (moved `TestDecimate`/`TestResolveLimit`;
`check_scan_cap` raises `WindowTooLarge` above the cap, passes at it),
`tests/test_errors.py` (`ProviderNotReady` is a `ProviderUnavailable`),
`tests/test_progress.py` (`SyncStep.as_dict`; `no_progress` accepts the sink signature).
2a changes: create `limits.py`, `errors.py`, `progress.py`; provider `sampling.py`
imports from `limits`; `sync.py`/`ingest.py` import from `progress` (drop `_no_progress`);
re-base the two exceptions; `routes/service.py` map becomes `UnknownMetric 404`,
`WindowTooLarge 413`, `InvalidLimit`/`BadRequest 400`, `ProviderUnavailable 503` (drop the
provider imports); `service.py` catches `ProviderNotReady` and re-raises the original
when `has_any_data()` (so `match="GARMIN_HOME_TZ"` assertions keep passing).
2b tests first: `tests/fakes.py::FakeReader` (in-memory: ~10 heart-rate samples, one
`SleepSession`; ctor flags `fault`, `ready`, `has_data`; `metrics` via
`metric_entry(HeartRate, build=…, probe=…, provenance="fake:heart_rate")`;
`sleep_sessions` raises `ProviderNotReady` when not ready; records `probe_calls`,
`closed`). Rewrite `tests/test_service.py` against it (catalog filtered by probe, TTL
identity, `invalidate`, expiry with injected clock, unknown metric, empty series not
error, default limit, invalid limit, degraded: fault → `ProviderUnavailable`;
not-ready + no data → empty; not-ready + data → 503; catalog empty when degraded;
`status()` keys exactly `available/fault/metrics`). Generic `test_service_routes.py`
builds `Litestar(route_handlers=[v1_router], state=State({"health_service":
HealthDataService(FakeReader(...))}))`. Generic `test_registry.py`: `metric_entry()`
derives descriptor from the spec class; unit override; `series()` uses `SOURCE`; frozen;
the no-`IntervalSample` structural guard.
2b changes: `ports.py` with `SOURCE` + `HealthReader`; `registry.py` retyped (bound
aliases, `metric_entry` public, `METRICS` removed); `providers/garmindb/registry.py:
metrics_for(conn)`; `providers/garmindb/reader.py: GarminDbReader(connection)`
(`fault`, `metrics` built once, `has_any_data`, `sleep_sessions` →
`sleep.build_sleep_sessions(conn, …)`, `close`, provider-only `reset`); `service.py`
over `HealthReader`; `app.py` lifespan constructs `GarminDbReader(GarminConnection(settings))`
and `on_corpus_changed` calls `reader.reset()`; `routes/owner.py:_serving_fault` reads
`service.fault`. Move the corpus-bound registry/service tests per the table.
Pinned by: `TestManifestRoutingContract`, `TestWorkouts`, `TestNeverSynced`,
`TestDegraded`, `test_serialization`, provider `test_service_routes` end-to-end, `test_connection`.

### Phase 3 — Move the acquisition side; split config. Risk: medium-low (mostly moves; the config split is the delicate part).
3a. `git mv` `auth.py`, `garmin_config.py`→`config_file.py`, `preferences.py`, `sync.py`,
`timezones.py` into `providers/garmindb/`; fix imports (`ingest.py` now imports
`incremental_range` from sibling `sync`, `STAT_LABELS` from sibling `preferences`,
`load_manager` from sibling `config_file`); move their tests. `app.py` and
`routes/owner.py` temporarily import from `garmin_health.providers.garmindb.*` (fixed in
Phase 4; enforcement arrives in Phase 5).
3b tests first: generic `test_config.py` — `HEALTH_PROVIDER` default `"garmindb"`, read
from env, blank/whitespace → default; `attrs.fields(Settings)` names are exactly
`{app_data_dir, provider}`; Dockerfile tests unchanged. Provider `test_settings.py` — the
moved field tests; `GarminDbSettings.from_env(app_data_dir, env)` reads `GARMIN_*` and
`SYNC_INTERVAL_SECONDS`; **pin the four on-disk paths explicitly** (the no-migration guarantee).
3b changes: `GarminDbSettings` in `providers/garmindb/settings.py` (`dateutil` leaves
`config.py`); generic `Settings(app_data_dir, provider)`; every provider module's
`Settings` annotation → `GarminDbSettings`; provider conftest overrides `settings`;
`_clean_env` gains `HEALTH_PROVIDER`, `GARMIN_FILL_STAGE_GAPS`, `GARMIN_DERIVE_RESTLESS_PERIODS`.
Pinned by: `test_token_file_matches_garmindb_expectation`,
`test_the_image_never_shadows_the_routers_volume`, `test_derived_paths_all_live_under_app_data`,
all of `test_sync`/`test_auth`/`test_preferences`/`test_config_file`.

### Phase 4 — The `Provider` port, `GarminDbProvider`, `create_app(provider=)`, owner split. Risk: high (largest diff; 18 `create_app` call sites; the page). Mitigation: 4a keeps `routes/owner.py` monolithic so every existing owner test still runs against the real handlers before the split.
4a tests first: `tests/fakes.py::FakeLink` (state/account/error; `plant_error()`; peek
non-consuming, take consuming; `unlink` → NOT_LINKED) and `FakeProvider` (`name="fake"`;
`open_reader()` returns and records a `FakeReader`; one recording lifespan;
`owner_routes()` → `GET /fake/owner`; `public_routes()` → `GET /fake/public`;
`status()` → `{"running": False, "progress": None, "fake": True}`; `render_setup` →
`<p id='fake-setup' data-state='…'>`; `subscribe_data_changed` + `emit_data_changed()`).
`tests/test_app.py`: default provider via `build_provider(settings)` (asserted by
`provider.name`, not an import); unknown name → `ConfigError` at `create_app`; provider
lifespans run and unwind in order; `/fake/owner` 401 without the header, 200 with;
`/fake/public` 200 without; `emit_data_changed()` drops the catalog cache (via
`FakeReader.probe_calls`); reader closed on shutdown; `/sync/status` =
`{link_state, running, progress, fake, serving}`. `tests/providers/test_contract.py::
TestProviderContract` parametrised over `FakeProvider()` and `GarminDbProvider` on a tmp
corpus with `FakeIngest`: `isinstance(p, Provider)`; `open_reader()` is a `HealthReader`;
`reader.metrics` keys equal descriptor ids; `series([])` matches descriptor; no
`IntervalSample` in any `series_cls`; each `build(None, None, 5)` yields ≤5 `Sample`s;
`probe()` is bool; `sleep_sessions(None, None, 1)` ≤1; `link.status()` is a `LinkStatus`;
fresh `take_error()` is `None`; `await unlink()` → NOT_LINKED; every owner route answers
401 without the header when mounted under `owner_guard`; `await status()` has bool
`running` and a `progress` key; `render_setup(view)` non-empty for every `LinkState`.
4a changes: complete `ports.py`; `providers/garmindb/provider.py::GarminDbProvider(settings,
*, authenticator=None, ingest_factory=None, clock=…)` + `from_env(app_data_dir, env)`,
owning `SyncEngine(on_corpus_changed=self._corpus_changed, interval=lambda:
load_preferences(settings).sync_interval_seconds)`, the `_sync_loop` lifespan
(with `SHUTDOWN_GRACE_SECONDS`), `open_reader()` (constructs `GarminConnection` +
`GarminDbReader`, logs the fault, retains both), `_corpus_changed()` (reset if open,
then fire subscribers), `status()` (engine status + `coverage` + `corpus`),
`owner_routes()` (today's seven handlers, still in `routes/owner.py` for this commit),
`public_routes()` → `[]`, `render_setup` (stub returning today's body until 4b);
`providers/__init__.py::build_provider`; `app.py::create_app(settings, provider)` with
`state = {"settings", "provider"}`, a generic `serving_layer` using `provider.open_reader()`,
owner router `Router("/", [setup_page, setup_status, unlink, sync_status,
*provider.owner_routes()], guards=[owner_guard])`, a public router only when non-empty,
`lifespan=[serving_layer, *provider.lifespans()]`; `auth.py` returns `LinkStatus`,
`AWAITING_MFA` → `PENDING`; fix the stale "worker thread" docstring. Migrate the 18 call
sites mechanically: `create_app(settings=s, authenticator=A, ingest_factory=F)` →
`create_app(settings=Settings(app_data_dir=s.app_data_dir), provider=GarminDbProvider(s,
authenticator=A, ingest_factory=F))` — the conftest helpers absorb this.
4b tests first: generic `test_owner_routes.py` against `FakeProvider` (headline per
state; flash consumed by `/setup`, only peeked by `/setup/status`; `render_setup` output
embedded; unlink form only when LINKED; `/setup/unlink` calls the link; the
`provider_error` re-render path). Provider `test_owner_routes.py`: the moved tests.
4b changes: `routes/fragments.py`; `routes/owner.py` reduced to guard + shell + four
handlers + `render_setup_page(state, *, consume_flash, provider_error)`;
`providers/garmindb/owner.py` gets the fragments and seven handlers (calling
`render_setup_page` for the 400 re-render); `GarminDbProvider.render_setup` builds its own
view (coverage, preferences, step, summary) and returns MFA form / linked section (sync
summary, `progress_line`, Sync now, import scope, coverage table, rebuild control when
`view.serving_fault`) / credentials + token forms + `PASSWORD_NOTICE`.
Pinned by: `test_setup_surface_is_owner_gated`, `test_sync_surface_is_owner_gated`,
`TestProgressDisplay` (the `BUSY_SCRIPT` contract), `test_the_mint_snippet_matches_the_installed_library`,
`TestRebuildFromTheOwnerPage`, `test_health_*`, `TestProviderContract`.

### Phase 5 — Enforcement, dependency declaration, manifest test, docs. Risk: low.
Tests first: `tests/test_boundary.py` — AST walk of `src/garmin_health`: no
`garmindb|garminconnect|idbutils|fitfile|sqlalchemy|garth` import outside
`providers/garmindb/`; no `garmin_health.providers.garmindb` import outside `providers/`;
nothing under `providers/garmindb/` imports `garmin_health.app`, `.service`,
`.routes.owner`, `.routes.service` (ruff cannot express this reverse rule — the test is
the mechanism); the same rules for `tests/` vs `tests/providers/`. Extend
`TestManifestRoutingContract`: every `handler.paths` of the default provider's
`public_routes()` is in `openhost.toml public_paths`, and every `public_paths` entry is
served by some public route (both empty today; vacuous until a webhook route appears).
Changes — exact pyproject additions:

```toml
[project]
dependencies = [
    ...,
    "GarminDb==3.9.0",
    # Imported directly by providers/garmindb/auth.py; pinned to what GarminDb 3.9.0
    # resolves to, so the contracts AGENTS.md documents (return_on_mfa, resume_login) stay true.
    "garminconnect==0.3.11",
    ...,
]

[tool.ruff.lint]
select = ["E", "F", "B", "UP", "I", "PLC0415", "TID251"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"garmindb".msg = "Only garmin_health.providers.garmindb may import GarminDB (AGENTS.md: provider boundary)."
"garminconnect".msg = "Only garmin_health.providers.garmindb may import garminconnect."
"idbutils".msg = "Only garmin_health.providers.garmindb may import idbutils."
"fitfile".msg = "Only garmin_health.providers.garmindb may import fitfile."
"sqlalchemy".msg = "Only garmin_health.providers.garmindb may import SQLAlchemy; the core reads spec types through ports.HealthReader."
"garth".msg = "Only garmin_health.providers.garmindb may import garth."
"garmin_health.providers.garmindb".msg = "The core must not name a concrete provider; go through garmin_health.providers.build_provider."

[tool.ruff.lint.per-file-ignores]
"src/garmin_health/providers/garmindb/**" = ["TID251"]
"src/garmin_health/providers/__init__.py" = ["TID251"]   # the selector's lazy import
"tests/providers/**" = ["TID251"]                         # corpus fixtures, GarminDB fakes, the contract test
```

`PLC0415` still applies to the selector (keep its `# noqa: PLC0415`). The mypy override
block is unchanged; `TID251` + `test_boundary.py` are the guards, not mypy. Run `uv lock`
and commit the lockfile change. Add a comment on `public_paths` in `openhost.toml`.
Docs (see below). Pinned by: `test_boundary.py`, the manifest test, `ruff check`.

### Phase 6 — Warn-once registry and `SOURCE` dedupe. Risk: low.
Tests first: `tests/test_warn_once.py` (one warning per token across threads; `reset()`
re-arms; logger/template injected). Changes: `warn_once.py::WarnOnce(logger, message)`;
`vocabulary.py` uses it (`reset_unknown_event_log = _unmapped.reset` keeps the test seam);
`heart_rate.py`/`sleep.py` import `SOURCE` from `garmin_health.registry`.
Pinned by: `test_vocabulary.py` unchanged.

## Cross-cutting flows
- **Data changed → invalidate.** `create_app` registers
  `provider.subscribe_data_changed(lambda: _invalidate(state))`;
  `GarminDbProvider._corpus_changed` resets its retained `GarminConnection`, then calls
  subscribers; the engine's existing try/except still keeps a failing subscriber from
  failing a sync. A webhook provider fires the same subscription after writing its store.
- **`/sync/status` and the page JS.** Route returns `{"link_state":
  provider.link.status().state.value, **await provider.status(), "serving":
  service.status() if service else {"available": False}}`. `Provider.status()` is
  contract-tested to carry `running` and `progress`; `BUSY_SCRIPT` is untouched.
- **Guard.** One `Router(path="/", guards=[owner_guard], route_handlers=[shell…,
  *provider.owner_routes()])` — router-level, so a provider handler cannot forget it.
  `public_routes()` go on a second, guard-less router only when non-empty.
- **Reader lifetime.** Opened in the app's serving lifespan (first in the list so the
  sync loop's callbacks find a service), closed in its `finally`; provider lifespans nest inside.

## Docs
**AGENTS.md** — rewrite: the opening "two halves" paragraphs (provider-owned acquisition +
generic serving, chosen by `HEALTH_PROVIDER`); every path in "Project Status"; add a
"Landed — provider boundary" paragraph naming `ports.py`, `providers/garmindb/`,
`FakeProvider`, `TestProviderContract`; "Environment knobs" (+`HEALTH_PROVIDER`; note
`GARMIN_*`/`SYNC_INTERVAL_SECONDS` are garmindb-only); the "Isolation rule" bullet (what
is banned where, enforced by `TID251` + `test_boundary.py`; the selector is the only
importer of a concrete provider; `source` is always `"garmin"`); the "Project Structure"
tree and "Dependency direction" line; the "Serving", "GarminDB → reset()", "Import scope
and progress", "Sync → sync.py imports no garmindb" (the `Ingest` port stays as a
testability seam *inside* the provider; it is not the provider seam) and "Auth" guardrails'
paths; a new guardrail group **Provider boundary** (link-state vocabulary is `PENDING`, not
MFA; one-shot flash rule; public routes must be in `public_paths`; `Provider.status()`
must carry `running`/`progress`; providers must not import `app`/`service`/`routes.*`;
`LinkStatus.detail` stays idempotent).
**plan.md** — do not rewrite Parts 1–4. Add "Addendum A — Provider boundary (2026-09)":
the three decisions, the aggregator facts that shaped the port, the port signatures, the
layout, the enforcement rule; one line at the top of "Repository layout" pointing to it.

## Later / optional (separate commits, each behind its own tests)
- **Phase 7 — uniform scan cap (behaviour change).** Tests first: a window over
  `MAX_ROWS_SCANNED` candidate rows is refused with `WindowTooLarge` for
  `build_resting_heart_rate` and `build_sleep_sessions` (monkeypatch the cap; make
  `check_scan_cap` read it at call time). Leave `heart_rate._window_samples` alone (a
  per-night count per sub-series doubles every session's query cost and the night window
  already bounds it) — document that choice.
- **Phase 8 — sleep assembly lift (optional, last).** `sleep_assembly.py`:
  `clean_intervals(raw, *, fill_gaps)`, `stage_totals`, `resolve_durations`, `efficiency`,
  `restless_periods`, `window_from_intervals` with the end≤start rejection; pure-function
  tests first; the 57 corpus tests are the regression net. Skip if no aggregator is imminent.
- **Phase 9 — `MAX_SESSION_SUBSERIES` as a parameter** of `session_heart_rate`/`session_hrv`.

## Verification
Per phase: `uv run ruff check . && uv run ruff format --check . && uv run python -m mypy
&& uv run python -m pytest -q` (in some environments `uv run mypy` fails to spawn; use
`python -m mypy`). Expect the collected count to grow with the new generic tests and no
GarminDB test to lose an assertion.
End-to-end after Phase 4 and again after Phase 5:
1. `BOTTLE_APP_DATA_DIR=<tmp> uv run hypercorn garmin_health.app:app --bind 127.0.0.1:8080`;
   `GET /health` → 200; `GET /setup` without the owner header → 401, with
   `X-OpenHost-Is-Owner: true` → the "Not linked" page with the credentials and token
   forms; `GET /api/v1/metrics` → `{"metrics": []}` 200 (never-synced); `GET /sync/status`
   → `link_state`, `running`, `progress`, `coverage`, `corpus`, `serving`.
2. `HEALTH_PROVIDER=bogus …` → startup fails with `ConfigError` naming the value.
3. Against a **copy** of the owner's real app-data (constructing a `DB` writes):
   `GET /api/v1/time-series?metric=heart_rate&limit=5` and `/api/v1/sleep-sessions?limit=1`
   return the same payloads before and after the refactor (diff the JSON);
   `POST /sync` → 202 and the page's progress line still animates.
4. `docker build -t garmin-health .` succeeds; the container starts with no `BOTTLE_*`
   variable and writes under `/app/data`.
5. Sanity for the enforcement: temporarily add `import sqlalchemy` to `service.py` →
   `ruff check` fails with the banned-api message; revert.

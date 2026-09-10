"""Tests for the sync orchestration.

sync.py deliberately imports no garmindb, so the whole download -> import ->
analyze sequence, the loop, the lock and the status reporting are all testable
against a fake ingest with no Garmin account and no network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from threading import Event

import pytest

from garmin_health.auth import GarminAuthenticator
from garmin_health.auth import LinkState
from garmin_health.config import Settings
from garmin_health.sync import SyncEngine
from garmin_health.sync import SyncPhase
from garmin_health.sync import TableStat
from garmin_health.sync import incremental_range
from tests.fakes import FakeIngest
from tests.fakes import RecordingFactory

TODAY = dt.date(2026, 6, 15)


class TestIncrementalRange:
    """GarminDB's own rule (garmindb_cli.py __get_date_and_days): start one day
    before the newest row so a partially-downloaded day is refetched."""

    def test_falls_back_to_the_configured_start_when_the_table_is_empty(self) -> None:
        fallback = (dt.date(2019, 12, 31), 2444)
        assert incremental_range(latest=None, today=TODAY, fallback=fallback) == fallback

    def test_starts_one_day_before_the_newest_row(self) -> None:
        latest = dt.datetime(2026, 6, 14, 23, 0)
        assert incremental_range(latest=latest, today=TODAY, fallback=(TODAY, 1)) == (
            dt.date(2026, 6, 13),
            2,
        )

    def test_accepts_a_date_as_well_as_a_datetime(self) -> None:
        """Sleep.day and Hrv.day come back as dates on some SQLite paths."""
        assert incremental_range(latest=dt.date(2026, 6, 14), today=TODAY, fallback=(TODAY, 1)) == (
            dt.date(2026, 6, 13),
            2,
        )

    def test_a_table_current_to_today_still_refetches_yesterday(self) -> None:
        assert incremental_range(
            latest=dt.datetime(2026, 6, 15, 8, 0), today=TODAY, fallback=(TODAY, 1)
        ) == (
            dt.date(2026, 6, 14),
            1,
        )

    def test_a_future_row_yields_no_days_rather_than_a_negative_span(self) -> None:
        """A clock skew or a travelling watch must not ask Garmin for -3 days."""
        latest = dt.datetime(2026, 6, 20, 8, 0)
        _, days = incremental_range(latest=latest, today=TODAY, fallback=(TODAY, 1))
        assert days == 0


def make_engine(
    settings: Settings,
    *,
    ingest: FakeIngest | None = None,
    linked: bool = True,
    interval: int = 3600,
) -> tuple[SyncEngine, FakeIngest, GarminAuthenticator]:
    ingest = ingest or FakeIngest()
    auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory(needs_mfa=False))
    if linked:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        settings.token_file.write_text('{"di_refresh_token": "r"}')
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory(needs_mfa=False))
    engine = SyncEngine(
        settings=Settings(app_data_dir=settings.app_data_dir, sync_interval_seconds=interval),
        authenticator=auth,
        ingest_factory=lambda: ingest,
    )
    return engine, ingest, auth


class TestRunOnce:
    async def test_runs_the_phases_in_the_required_order(self, settings: Settings) -> None:
        """The profile import must precede everything that reads measurement_system,
        and analyze must run last over the imported rows."""
        engine, ingest, _ = make_engine(settings)
        report = await engine.run_once()
        assert ingest.calls == ["table_stats", "download", "import", "analyze", "table_stats"]
        assert report is not None
        assert report.error is None
        assert report.finished_at is not None

    async def test_records_a_duration(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        report = await engine.run_once()
        assert report is not None
        assert report.duration_seconds is not None
        assert report.duration_seconds >= 0

    async def test_reports_row_growth(self, settings: Settings) -> None:
        ingest = FakeIngest(
            stats_sequence=[
                {"sleep": TableStat(rows=1, latest="2026-06-14T23:00:00")},
                {"sleep": TableStat(rows=4, latest="2026-06-15T23:00:00")},
            ]
        )
        engine, _, _ = make_engine(settings, ingest=ingest)
        report = await engine.run_once()
        assert report is not None
        assert report.row_delta == {"sleep": 3}
        assert report.changed is True

    async def test_surfaces_a_sync_that_imported_nothing(self, settings: Settings) -> None:
        """GarminDB's importers swallow every per-file exception, so a totally
        failed sync looks successful. Row counts are the only evidence."""
        same = {"sleep": TableStat(rows=1, latest="2026-06-14T23:00:00")}
        engine, _, _ = make_engine(settings, ingest=FakeIngest(stats_sequence=[same, same]))
        report = await engine.run_once()
        assert report is not None
        assert report.error is None
        assert report.changed is False
        assert report.row_delta == {"sleep": 0}

    async def test_a_failing_phase_is_recorded_not_raised(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="download"))
        report = await engine.run_once()
        assert report is not None
        assert report.phase is SyncPhase.DOWNLOAD
        assert report.error is not None and "boom" in report.error
        assert report.finished_at is not None

    async def test_a_later_phase_failure_still_names_that_phase(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="analyze"))
        report = await engine.run_once()
        assert report is not None
        assert report.phase is SyncPhase.ANALYZE

    async def test_refuses_to_run_when_the_account_is_not_linked(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, linked=False)
        assert await engine.run_once() is None
        assert ingest.calls == []

    async def test_does_not_run_on_the_event_loop(self, settings: Settings) -> None:
        """GarminDB and garminconnect are entirely synchronous, and a real download
        blocks for tens of minutes."""
        engine, ingest, _ = make_engine(settings)
        await engine.run_once()
        main_thread = (
            asyncio.get_running_loop()._thread_id
            if hasattr(asyncio.get_running_loop(), "_thread_id")
            else None
        )
        assert ingest.thread_ids
        assert all(tid != main_thread for tid in ingest.thread_ids) or main_thread is None
        assert len(set(ingest.thread_ids)) == 1


class TestOverlap:
    async def test_a_second_run_is_refused_while_one_is_in_flight(self, settings: Settings) -> None:
        """A manual trigger must not overlap the scheduled run: two importers over
        one SQLite corpus is how you get a half-written night."""
        release = Event()
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(block_on=release))
        first = asyncio.create_task(engine.run_once())
        await asyncio.sleep(0.05)
        assert engine.is_running is True
        assert await engine.run_once() is None
        release.set()
        assert await first is not None
        assert ingest.calls.count("download") == 1

    async def test_the_lock_is_released_after_a_failure(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="import"))
        await engine.run_once()
        assert engine.is_running is False
        assert await engine.run_once() is not None


class TestTrigger:
    async def test_trigger_returns_immediately_and_runs_in_the_background(
        self, settings: Settings
    ) -> None:
        """POST /sync must not hold the request open for a multi-minute backfill."""
        release = Event()
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(block_on=release))
        assert await engine.trigger() is True
        assert engine.is_running is True
        release.set()
        await engine.wait_for_idle()
        assert ingest.calls.count("download") == 1

    async def test_trigger_is_refused_while_running(self, settings: Settings) -> None:
        release = Event()
        engine, _, _ = make_engine(settings, ingest=FakeIngest(block_on=release))
        assert await engine.trigger() is True
        assert await engine.trigger() is False
        release.set()
        await engine.wait_for_idle()


class TestStatus:
    def test_reports_the_link_state_before_any_sync(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, linked=False)
        status = engine.status()
        assert status["link_state"] == LinkState.NOT_LINKED.value
        assert status["running"] is False
        assert status["last_sync"] is None

    async def test_reports_tables_and_timings_after_a_sync(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        await engine.run_once()
        last = engine.status()["last_sync"]
        assert last is not None
        assert last["error"] is None
        assert last["started_at"].endswith("+00:00")
        assert last["duration_seconds"] is not None
        assert "sleep" in last["tables"]
        assert last["tables"]["sleep"]["rows"] == 1

    async def test_reports_the_last_error(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="download"))
        await engine.run_once()
        last = engine.status()["last_sync"]
        assert last is not None
        assert "boom" in last["error"]
        assert last["phase"] == "download"

    async def test_timestamps_are_aware_utc(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        report = await engine.run_once()
        assert report is not None
        assert report.started_at.tzinfo is dt.UTC
        assert report.finished_at is not None and report.finished_at.tzinfo is dt.UTC


class TestLoop:
    async def test_sleeps_before_the_first_sync(self, settings: Settings) -> None:
        """Otherwise a crash-looping container would hammer Garmin's SSO on every
        restart, which is a good way to get an account throttled."""
        engine, ingest, _ = make_engine(settings, interval=3600)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.05)
        assert ingest.calls == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_syncs_once_per_interval(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, interval=0)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ingest.calls.count("download") >= 1

    async def test_skips_the_interval_while_unlinked(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, linked=False, interval=0)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ingest.calls == []

    async def test_a_failing_sync_does_not_kill_the_loop(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(fail_on="download"), interval=0)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ingest.calls.count("download") >= 2


class TestCancellation:
    async def test_requesting_stop_signals_the_running_ingest(self, settings: Settings) -> None:
        """Download sleeps a second per day and retries with backoff, so a long
        backfill cannot be interrupted mid-call. Between stats is the best that is
        actually available, and it is what keeps shutdown from hanging for minutes."""
        release = Event()
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(block_on=release))
        asyncio.create_task(engine.run_once())
        await asyncio.sleep(0.05)
        engine.request_stop()
        assert ingest.stop_event is not None
        assert ingest.stop_event.is_set()
        release.set()
        await engine.wait_for_idle()

    async def test_a_fresh_run_clears_a_previous_stop_request(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings)
        engine.request_stop()
        await engine.run_once()
        assert ingest.stop_event is not None
        assert ingest.stop_event.is_set() is False


class TestResilience:
    async def test_a_sync_survives_failing_to_read_stats_afterwards(
        self, settings: Settings
    ) -> None:
        """The corpus may be mid-rebuild; losing the evidence must not lose the run."""
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(fail_on="table_stats_after"))
        report = await engine.run_once()
        assert report is not None
        assert report.error is None
        assert report.after == report.before

    async def test_the_loop_survives_an_unexpected_engine_error(self, settings: Settings) -> None:
        """Belt and braces: a bug in the reporting path must not end the loop for
        the life of the container."""
        engine, ingest, _ = make_engine(settings, interval=0)
        calls = {"n": 0}
        original = engine.run_once

        async def flaky() -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("unexpected")
            return await original()

        engine.run_once = flaky  # type: ignore[method-assign]
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls["n"] >= 2

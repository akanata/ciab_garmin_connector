"""Tests for the sync orchestration.

sync.py deliberately imports no garmindb, so the whole download -> import ->
analyze sequence, the loop, the lock and the status reporting are all testable
against a fake ingest with no Garmin account and no network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from threading import Event

import pytest

from garmin_health.auth import GarminAuthenticator
from garmin_health.auth import LinkState
from garmin_health.config import Settings
from garmin_health.sync import StatCoverage
from garmin_health.sync import SyncEngine
from garmin_health.sync import SyncPhase
from garmin_health.sync import TableStat
from garmin_health.sync import incremental_range
from tests.providers.garmindb.fakes import FakeIngest
from tests.providers.garmindb.fakes import RecordingFactory
from tests.providers.garmindb.fakes import ReportingIngest

TODAY = dt.date(2026, 6, 15)
FLOOR = dt.date(2019, 12, 31)


class TestIncrementalRange:
    """Which days a routine sync fetches.

    GarminDB's own rule (garmindb_cli.py __get_date_and_days) starts a day before
    the newest row but computes ``days = (today - start).days``, and its
    downloaders loop ``range(0, days)`` -- so the last day it ever fetches is
    *yesterday*. Last night's sleep (calendarDate today) and today's heart rate
    were never downloaded, and no number of manual syncs could change that,
    because every sync computed the same range. The range here includes today.
    """

    def test_an_empty_table_starts_at_the_floor(self) -> None:
        start, _ = incremental_range(latest=None, today=TODAY, floor=dt.date(2026, 6, 1))
        assert start == dt.date(2026, 6, 1)

    def test_it_starts_one_day_before_the_newest_row(self) -> None:
        latest = dt.datetime(2026, 6, 14, 23, 0)
        assert incremental_range(latest=latest, today=TODAY, floor=FLOOR) == (
            dt.date(2026, 6, 13),
            3,
        )

    def test_accepts_a_date_as_well_as_a_datetime(self) -> None:
        """Sleep.day and Hrv.day come back as dates on some SQLite paths."""
        assert incremental_range(latest=dt.date(2026, 6, 14), today=TODAY, floor=FLOOR) == (
            dt.date(2026, 6, 13),
            3,
        )

    def test_a_table_current_to_today_refetches_yesterday_and_today(self) -> None:
        """Both can be partial: yesterday's file changes when the watch syncs
        overnight, and today's changes all day long."""
        assert incremental_range(
            latest=dt.datetime(2026, 6, 15, 8, 0), today=TODAY, floor=FLOOR
        ) == (dt.date(2026, 6, 14), 2)

    @pytest.mark.parametrize(
        "latest",
        [
            None,
            dt.date(2026, 5, 1),
            dt.datetime(2026, 6, 14, 23, 59),
            dt.datetime(2026, 6, 15, 0, 5),
        ],
    )
    def test_the_last_day_fetched_is_always_today(
        self, latest: dt.datetime | dt.date | None
    ) -> None:
        """Downloaders loop range(0, days), so the last day fetched is
        start + days - 1. That has to be today, whatever the table holds."""
        start, days = incremental_range(latest=latest, today=TODAY, floor=dt.date(2026, 1, 1))
        assert start + dt.timedelta(days=days - 1) == TODAY

    def test_a_future_row_yields_no_days_rather_than_a_negative_span(self) -> None:
        """A clock skew or a travelling watch must not ask Garmin for -3 days."""
        latest = dt.datetime(2026, 6, 20, 8, 0)
        _, days = incremental_range(latest=latest, today=TODAY, floor=FLOOR)
        assert days == 0

    def test_a_floor_in_the_future_yields_no_days(self) -> None:
        _, days = incremental_range(latest=None, today=TODAY, floor=dt.date(2026, 7, 1))
        assert days == 0


def linked_authenticator(settings: Settings) -> GarminAuthenticator:
    """An authenticator that reports LINKED, by planting the token GarminDB reads."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text('{"di_refresh_token": "r"}')
    return GarminAuthenticator(settings, garmin_factory=RecordingFactory(needs_mfa=False))


def make_engine(
    settings: Settings,
    *,
    ingest: FakeIngest | None = None,
    linked: bool = True,
    interval: int = 3600,
    startup_grace: float = 0.0,
    min_gap: float = 0.0,
) -> tuple[SyncEngine, FakeIngest, GarminAuthenticator]:
    ingest = ingest or FakeIngest()
    auth = (
        linked_authenticator(settings)
        if linked
        else GarminAuthenticator(settings, garmin_factory=RecordingFactory(needs_mfa=False))
    )
    engine = SyncEngine(
        settings=Settings(app_data_dir=settings.app_data_dir, sync_interval_seconds=interval),
        authenticator=auth,
        ingest_factory=lambda: ingest,
        startup_grace_seconds=startup_grace,
        min_gap_seconds=min_gap,
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


def plant_last_start(settings: Settings, when: dt.datetime) -> None:
    """What a previous process left behind."""
    settings.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.sync_state_file.write_text(json.dumps({"last_started_at": when.isoformat()}))


async def run_loop_for(engine: SyncEngine, seconds: float) -> None:
    task = asyncio.create_task(engine.run_forever())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestSchedule:
    """When the background loop syncs.

    It used to sleep a whole interval before its first sync and remember nothing
    across restarts, so every deploy waited out a full interval. The start of each
    sync is persisted instead. That keeps the one thing the delay was for -- a
    crash-looping container must not sign in on every restart -- without making a
    healthy restart wait.
    """

    async def test_a_never_synced_container_waits_only_the_grace(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, interval=3600, startup_grace=0.05)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.01)
        assert "download" not in ingest.calls
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "download" in ingest.calls

    async def test_a_restart_right_after_a_sync_waits_out_the_interval(
        self, settings: Settings
    ) -> None:
        """The crash-loop case. A start recorded seconds ago means no sign-in, however
        often the container restarts."""
        plant_last_start(settings, dt.datetime.now(dt.UTC) - dt.timedelta(seconds=10))
        engine, ingest, _ = make_engine(settings, interval=3600)
        await run_loop_for(engine, 0.1)
        assert ingest.calls == []

    async def test_an_overdue_restart_syncs_without_waiting_an_interval(
        self, settings: Settings
    ) -> None:
        """The deploy case. The last sync was hours ago, so there is nothing to
        protect and no reason to wait."""
        plant_last_start(settings, dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))
        engine, ingest, _ = make_engine(settings, interval=3600)
        await run_loop_for(engine, 0.1)
        assert "download" in ingest.calls

    async def test_the_start_is_recorded_before_any_garmin_work(self, settings: Settings) -> None:
        """A sync that takes the process down part-way still has to count, or the
        restart would sign in again at once."""
        release = Event()
        engine, _, _ = make_engine(settings, ingest=FakeIngest(block_on=release))
        assert await engine.trigger() is True
        await asyncio.sleep(0.05)
        try:
            assert settings.sync_state_file.exists()
        finally:
            release.set()
            await engine.wait_for_idle()

    async def test_the_recorded_start_carries_into_a_new_process(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, interval=3600)
        await engine.run_once()
        started = dt.datetime.fromisoformat(engine.status()["last_sync"]["started_at"])

        fresh, _, _ = make_engine(settings, interval=3600)
        next_at = dt.datetime.fromisoformat(fresh.status()["next_sync_at"])
        assert next_at == started + dt.timedelta(hours=1)

    async def test_a_backfill_does_not_move_the_schedule(self, settings: Settings) -> None:
        """The schedule is for fetching new data. A backfill fetches old history and
        says nothing about how fresh the corpus is."""
        engine, _, _ = make_engine(settings)
        await engine.run_once(backfill=True)
        assert engine.status()["next_sync_at"] is None
        assert not settings.sync_state_file.exists()

    async def test_a_manual_sync_pushes_the_next_scheduled_one_back(
        self, settings: Settings
    ) -> None:
        """Otherwise Sync now could be followed minutes later by a scheduled sync
        fetching exactly the same days."""
        engine, _, _ = make_engine(settings, interval=3600)
        assert await engine.trigger() is True
        await engine.wait_for_idle()
        next_at = dt.datetime.fromisoformat(engine.status()["next_sync_at"])
        assert next_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=59)

    async def test_an_interval_change_does_not_wait_out_the_old_one(
        self, settings: Settings
    ) -> None:
        """Shortening the interval on /setup must not first sleep through the long
        wait the loop had already begun."""
        interval = {"seconds": 3600}
        plant_last_start(settings, dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30))
        ingest = FakeIngest()
        engine = SyncEngine(
            settings=Settings(app_data_dir=settings.app_data_dir),
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: ingest,
            interval=lambda: interval["seconds"],
            startup_grace_seconds=0,
            min_gap_seconds=0,
        )
        task = asyncio.create_task(engine.run_forever())
        await asyncio.sleep(0.05)
        assert ingest.calls == []

        interval["seconds"] = 900
        engine.reschedule()
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "download" in ingest.calls

    async def test_status_reports_the_interval_in_force(self, settings: Settings) -> None:
        engine = SyncEngine(
            settings=Settings(app_data_dir=settings.app_data_dir),
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(),
            interval=lambda: 900,
        )
        assert engine.status()["interval_seconds"] == 900

    async def test_a_corrupt_state_file_counts_as_never_synced(self, settings: Settings) -> None:
        """A half-written file must not stop scheduling for the life of the container."""
        settings.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        settings.sync_state_file.write_text("{not json")
        engine, ingest, _ = make_engine(settings, interval=3600)
        await run_loop_for(engine, 0.1)
        assert "download" in ingest.calls

    async def test_it_keeps_syncing_once_per_interval(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, interval=0)
        await run_loop_for(engine, 0.1)
        assert ingest.calls.count("download") >= 2

    async def test_it_does_nothing_while_unlinked(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, linked=False, interval=0)
        await run_loop_for(engine, 0.05)
        assert ingest.calls == []

    async def test_a_failing_sync_does_not_kill_the_loop(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings, ingest=FakeIngest(fail_on="download"), interval=0)
        await run_loop_for(engine, 0.1)
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


class TestCorpusChangedCallback:
    """The serving layer holds pooled handles that a rebuild invalidates."""

    async def test_it_fires_after_a_successful_sync(self, settings: Settings) -> None:
        fired: list[int] = []
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(),
            on_corpus_changed=lambda: fired.append(1),
        )
        await engine.run_once()
        assert fired == [1]

    async def test_it_fires_after_a_failed_sync_too(self, settings: Settings) -> None:
        """A sync that died mid-import still changed the corpus, and the failure
        may itself be what a reset would clear."""
        fired: list[int] = []
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(fail_on="import"),
            on_corpus_changed=lambda: fired.append(1),
        )
        await engine.run_once()
        assert fired == [1]

    async def test_a_failing_callback_does_not_fail_the_sync(self, settings: Settings) -> None:
        def explode() -> None:
            raise RuntimeError("the serving layer is unhappy")

        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(),
            on_corpus_changed=explode,
        )
        report = await engine.run_once()
        assert report is not None
        assert report.error is None

    async def test_it_does_not_fire_when_no_sync_ran(self, settings: Settings) -> None:
        fired: list[int] = []
        engine = SyncEngine(
            settings=settings,
            authenticator=GarminAuthenticator(settings, garmin_factory=RecordingFactory()),
            ingest_factory=lambda: FakeIngest(),
            on_corpus_changed=lambda: fired.append(1),
        )
        assert await engine.run_once() is None
        assert fired == []


class TestRebuild:
    async def test_it_runs_the_rebuild_and_nothing_else(self, settings: Settings) -> None:
        """No download: the whole point is that the retained JSON/FIT corpus makes
        a schema rebuild a local reimport rather than hours of re-downloading."""
        ingest = FakeIngest()
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: ingest,
        )
        report = await engine.rebuild_once()
        assert report is not None
        assert report.error is None
        assert "rebuild" in ingest.calls
        assert "download" not in ingest.calls

    async def test_it_reports_a_failure_rather_than_raising(self, settings: Settings) -> None:
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(fail_on="rebuild"),
        )
        report = await engine.rebuild_once()
        assert report is not None
        assert report.error is not None
        assert report.phase is SyncPhase.REBUILD

    async def test_it_resets_the_serving_layer_afterwards(self, settings: Settings) -> None:
        """Without this the pooled handles still point at the deleted inode."""
        fired: list[int] = []
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(),
            on_corpus_changed=lambda: fired.append(1),
        )
        await engine.rebuild_once()
        assert fired == [1]

    async def test_it_needs_no_linked_account(self, settings: Settings) -> None:
        """A rebuild is purely local. Refusing it while unlinked would strand a
        container whose corpus is broken and whose token has expired."""
        engine = SyncEngine(
            settings=settings,
            authenticator=GarminAuthenticator(settings, garmin_factory=RecordingFactory()),
            ingest_factory=lambda: FakeIngest(),
        )
        report = await engine.rebuild_once()
        assert report is not None
        assert report.error is None

    async def test_it_refuses_to_start_while_a_sync_is_in_flight(self, settings: Settings) -> None:
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: FakeIngest(block_on=threading.Event()),
        )
        assert await engine.trigger() is True
        assert await engine.trigger_rebuild() is False
        engine.request_stop()


class TestStatCoverage:
    def test_a_gap_is_the_days_between_the_floor_and_the_oldest_row(self) -> None:
        coverage = StatCoverage(
            stat="sleep",
            enabled=True,
            rows=100,
            earliest="2024-03-01",
            latest="2026-09-10",
            floor="2020-01-01",
        )
        assert coverage.missing_days == 1521
        assert coverage.has_gap is True

    def test_a_corpus_reaching_the_floor_has_no_gap(self) -> None:
        coverage = StatCoverage(
            stat="sleep",
            enabled=True,
            rows=100,
            earliest="2020-01-01",
            latest="2026-09-10",
            floor="2020-01-01",
        )
        assert coverage.missing_days == 0
        assert coverage.has_gap is False

    def test_a_corpus_older_than_the_floor_has_no_gap(self) -> None:
        """Raising the floor does not make already-downloaded history a problem."""
        coverage = StatCoverage(
            stat="sleep",
            enabled=True,
            rows=100,
            earliest="2019-01-01",
            latest="2026-09-10",
            floor="2020-01-01",
        )
        assert coverage.missing_days == 0

    def test_an_empty_metric_reports_no_gap(self) -> None:
        """A normal sync already starts at the floor when a table is empty, so
        offering a backfill here would be a button that duplicates Sync now."""
        coverage = StatCoverage(
            stat="sleep", enabled=True, rows=0, earliest=None, latest=None, floor="2020-01-01"
        )
        assert coverage.missing_days == 0
        assert coverage.has_gap is False

    def test_it_serializes_for_the_status_endpoint(self) -> None:
        coverage = StatCoverage(
            stat="sleep",
            enabled=True,
            rows=100,
            earliest="2024-03-01",
            latest="2026-09-10",
            floor="2020-01-01",
        )
        assert coverage.as_dict() == {
            "stat": "sleep",
            "enabled": True,
            "rows": 100,
            "earliest": "2024-03-01",
            "latest": "2026-09-10",
            "floor": "2020-01-01",
            "missing_days": 1521,
        }


class TestProgress:
    async def test_no_progress_is_reported_while_idle(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        assert engine.status()["progress"] is None

    async def test_the_engine_reports_the_step_the_ingest_is_on(self, settings: Settings) -> None:
        """The whole point: a multi-hour download is otherwise invisible outside
        the container logs."""
        seen: list[dict[str, object] | None] = []
        ingest = ReportingIngest(seen_from=lambda: seen.append(engine.status()["progress"]))
        engine, _, _ = make_engine(settings, ingest=ingest)  # type: ignore[arg-type]
        await engine.run_once()

        labels = [s["label"] for s in seen if s]
        assert "Downloading sleep (452 days)" in labels
        assert any(s and s["total"] == 4 for s in seen)

    async def test_the_step_is_cleared_when_the_sync_ends(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        await engine.run_once()
        assert engine.status()["progress"] is None

    async def test_the_step_is_cleared_after_a_failure_too(self, settings: Settings) -> None:
        """A stuck progress line would read as a sync that never finished."""
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="import"))
        await engine.run_once()
        assert engine.status()["progress"] is None


class TestBackfill:
    async def test_it_downloads_the_older_range_then_imports(self, settings: Settings) -> None:
        ingest = FakeIngest()
        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=lambda: ingest,
        )
        report = await engine.run_once(backfill=True)
        assert report is not None
        assert report.error is None
        assert ingest.calls == ["table_stats", "backfill", "import", "analyze", "table_stats"]

    async def test_a_normal_sync_still_downloads_forward(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings)
        await engine.run_once()
        assert "download" in ingest.calls
        assert "backfill" not in ingest.calls

    async def test_it_needs_a_linked_account(self, settings: Settings) -> None:
        """Unlike a rebuild, this really does talk to Garmin."""
        engine, _, _ = make_engine(settings, linked=False)
        assert await engine.run_once(backfill=True) is None

    async def test_triggering_it_reports_started(self, settings: Settings) -> None:
        engine, ingest, _ = make_engine(settings)
        assert await engine.trigger(backfill=True) is True
        await engine.wait_for_idle()
        assert "backfill" in ingest.calls

    async def test_it_will_not_start_alongside_a_sync(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings, ingest=FakeIngest(block_on=threading.Event()))
        assert await engine.trigger() is True
        assert await engine.trigger(backfill=True) is False
        engine.request_stop()


class TestCoverageReporting:
    async def test_it_exposes_what_each_metric_holds(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        coverage = await engine.coverage()
        assert "sleep" in coverage
        assert coverage["sleep"].rows == 42

    async def test_it_is_cached_between_page_loads(self, settings: Settings) -> None:
        """Building an ingest re-renders the GarminDB config and opens both
        databases; /setup must not pay that on every refresh."""
        built: list[int] = []

        def factory() -> FakeIngest:
            built.append(1)
            return FakeIngest()

        engine = SyncEngine(
            settings=settings,
            authenticator=linked_authenticator(settings),
            ingest_factory=factory,
        )
        await engine.coverage()
        await engine.coverage()
        assert len(built) == 1

    async def test_a_sync_invalidates_the_cache(self, settings: Settings) -> None:
        engine, _, _ = make_engine(settings)
        await engine.coverage()
        await engine.run_once()
        assert engine._coverage is None

    async def test_a_broken_corpus_reports_no_coverage_rather_than_raising(
        self, settings: Settings
    ) -> None:
        """A schema mismatch makes stat_coverage raise, and /setup still has to
        render -- it is where the owner goes to press Rebuild."""
        engine, _, _ = make_engine(settings, ingest=FakeIngest(fail_on="stat_coverage"))
        assert await engine.coverage() == {}

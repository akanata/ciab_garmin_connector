"""``WarnOnce``: one warning per distinct token, ever.

Extracted from the sleep-event vocabulary, where warning per row would have
buried the signal in its own noise -- a night is hundreds of events. The rules
that matter are that it deduplicates, that a *new* token still gets through, and
that it stays correct when several reader threads meet an unmapped token at the
same instant.
"""

from __future__ import annotations

import logging
import threading

import pytest

from garmin_health.warn_once import WarnOnce

MESSAGE = "Unmapped token %r; reporting it as UNKNOWN."
LOGGER_NAME = "tests.warn_once"


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


@pytest.fixture
def warn(logger: logging.Logger) -> WarnOnce:
    return WarnOnce(logger, MESSAGE)


class TestDeduplication:
    def test_the_first_sighting_warns(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            warn("banana")
        assert len(caplog.records) == 1
        assert "banana" in caplog.text

    def test_the_same_token_never_warns_again(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            for _ in range(50):
                warn("banana")
        assert len(caplog.records) == 1

    def test_a_distinct_token_warns_again(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deduplicating on the token, not on "have I ever warned" -- otherwise a
        second new vocabulary would be hidden by the first."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            warn("banana")
            warn("kumquat")
        assert len(caplog.records) == 2

    def test_it_reports_whether_it_warned(self, warn: WarnOnce) -> None:
        assert warn("banana") is True
        assert warn("banana") is False

    def test_the_empty_token_is_a_token_like_any_other(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A null column normalizes to "", and it is reachable from real data."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert warn("") is True
            assert warn("") is False
        assert len(caplog.records) == 1


class TestInjection:
    def test_it_logs_through_the_logger_it_was_given(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """So the warning names the module that owns the vocabulary, not this one."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            warn("banana")
        assert caplog.records[0].name == LOGGER_NAME

    def test_the_token_is_passed_lazily_not_pre_formatted(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """%-style with the token as an argument, so a disabled WARNING level
        costs nothing and structured handlers still see the raw token."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            warn("banana")
        record = caplog.records[0]
        assert record.msg == MESSAGE
        assert record.args == ("banana",)

    def test_two_registries_do_not_share_state(self, logger: logging.Logger) -> None:
        """Each vocabulary gets its own; one going quiet must not silence another."""
        first, second = WarnOnce(logger, MESSAGE), WarnOnce(logger, MESSAGE)
        assert first("banana") is True
        assert second("banana") is True


class TestReset:
    def test_reset_re_arms_the_warning(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The seam the vocabulary tests need: the log is process-global, which is
        the point of it, so a test has to be able to forget it."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            warn("banana")
            warn.reset()
            warn("banana")
        assert len(caplog.records) == 2


class TestThreadSafety:
    def test_concurrent_first_sightings_warn_exactly_once(
        self, warn: WarnOnce, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Sleep sessions are built on worker threads, and a corpus-wide read can
        meet the same unmapped token from several of them at once. A check-then-add
        without a lock warns once per racing thread.
        """
        threads = 32
        start = threading.Barrier(threads)

        def hammer() -> None:
            start.wait()
            warn("banana")

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            workers = [threading.Thread(target=hammer) for _ in range(threads)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        assert len(caplog.records) == 1

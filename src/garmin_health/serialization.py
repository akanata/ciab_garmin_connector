"""The cattrs converter, its two hooks, and the three response envelopes.

The envelopes are read off ``health_data_service/client.py`` rather than off any
prose: ``/v1/metrics`` is ``{"metrics": [...]}``, ``/v1/sleep-sessions`` is
``{"data": [...]}``, and ``/v1/time-series`` is a **bare** ``TimeSeries`` with no
wrapper at all. Getting one of those wrong is a ``KeyError`` in the consumer.

Timestamps are checked on the way out. ``isoformat()`` on a naive datetime emits
an offsetless string, and the client's ``fromisoformat`` would hand the consumer a
naive datetime hours away from the real instant, with nothing raising anywhere
along the way.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import cattrs.preconf.json
from health_data_service import Container
from health_data_service import MetricKind
from health_data_service import MetricType
from health_data_service import Sample
from health_data_service import SleepSession
from health_data_service import SleepStage
from health_data_service import TimeSeries

converter = cattrs.preconf.json.make_converter()

# cattrs' MultiStrategyDispatch consults _single_dispatch first, and (str, identity)
# is registered there. Both of these are (str, Enum) subclasses, so singledispatch
# resolves them by MRO and unstructure() returns the enum MEMBER rather than its
# value. json.dumps renders that correctly by accident -- it is a str subclass --
# but orjson, or any ``type(v) is str`` check downstream, would not.
converter.register_unstructure_hook(SleepStage, lambda v: v.value)
converter.register_unstructure_hook(MetricKind, lambda v: v.value)

# IntervalSample deliberately has NO hook. SleepStages.samples is
# list[IntervalSample[SleepStage]], a parametrized generic, on which
# _single_dispatch raises; dispatch then falls through to _function_dispatch and
# gen_unstructure_attrs_fromdict, which emits all three fields including
# end_timestamp. A hook registered for Sample here would resolve by MRO and undo
# that -- which is exactly the bug the consumer's own structure hook has.


def assert_aware_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value``, or raise if it is not an aware UTC instant.

    Every timestamp this service emits passes through here. The spec says
    timezone-aware UTC, and both ways of getting it wrong are silent: a naive
    value serializes without an offset, and a non-UTC offset serializes to an
    instant a consumer's date bucketing will place on the wrong day.
    """
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise ValueError(f"expected an aware UTC datetime, got {value!r}")
    return value


def _check_samples(samples: Iterable[Sample[Any]]) -> None:
    for sample in samples:
        assert_aware_utc(sample.timestamp)
        end = getattr(sample, "end_timestamp", None)
        if end is not None:
            assert_aware_utc(end)


def _check_series(series: TimeSeries | None) -> None:
    if series is not None:
        _check_samples(series.samples)


def _check_container(container: Container) -> None:
    assert_aware_utc(container.start)
    assert_aware_utc(container.end)


def metrics_payload(descriptors: Iterable[MetricType]) -> dict[str, Any]:
    """``GET /v1/metrics``: ``{"metrics": [MetricType, ...]}``."""
    return {"metrics": [converter.unstructure(d) for d in descriptors]}


def time_series_payload(series: TimeSeries) -> dict[str, Any]:
    """``GET /v1/time-series``: a **bare** TimeSeries, deliberately unwrapped.

    ``client.get_time_series`` structures ``resp.json()`` itself; a ``{"data": ...}``
    wrapper here would make every field come back missing.
    """
    _check_series(series)
    payload: dict[str, Any] = converter.unstructure(series)
    return payload


def sleep_sessions_payload(sessions: Iterable[SleepSession]) -> dict[str, Any]:
    """``GET /v1/sleep-sessions``: ``{"data": [SleepSession, ...]}``."""
    materialized = list(sessions)
    for session in materialized:
        _check_container(session)
        _check_series(session.stages)
        _check_series(session.heart_rate)
        _check_series(session.hrv)
    return {"data": [converter.unstructure(s) for s in materialized]}

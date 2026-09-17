"""The wire contract, asserted against the spec's own consumer client.

These are executable contracts rather than golden strings: a cattrs upgrade that
changes dispatch order should break the build, not production.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from health_data_service import HRV_RMSSD
from health_data_service import Container
from health_data_service import Duration
from health_data_service import HeartRate
from health_data_service import IntervalSample
from health_data_service import MetricKind
from health_data_service import MetricType
from health_data_service import Sample
from health_data_service import SleepSession
from health_data_service import SleepStage
from health_data_service import SleepStages
from health_data_service import TimeSeries
from health_data_service.client import converter as consumer_converter

from garmin_health.serialization import assert_aware_utc
from garmin_health.serialization import converter
from garmin_health.serialization import metrics_payload
from garmin_health.serialization import sleep_sessions_payload
from garmin_health.serialization import time_series_payload

T0 = dt.datetime(2026, 6, 15, 5, 0, tzinfo=dt.UTC)


def roundtrip(payload: object) -> object:
    """Everything a real consumer does to our bytes, and nothing we control."""
    return json.loads(json.dumps(payload))


class TestSleepStageHook:
    def test_a_stage_serializes_as_its_string_value_not_the_enum_member(self) -> None:
        """cattrs' MultiStrategyDispatch checks _single_dispatch first, where
        (str, identity) is registered. SleepStage is a (str, Enum), so without the
        hook unstructure() returns the enum MEMBER. json.dumps renders it right by
        accident; orjson or any ``type(v) is str`` check downstream would not."""
        assert type(converter.unstructure(SleepStage.DEEP)) is str
        assert converter.unstructure(SleepStage.DEEP) == "deep"

    def test_a_metric_kind_serializes_as_its_string_value(self) -> None:
        assert type(converter.unstructure(MetricKind.TIME_SERIES)) is str
        assert converter.unstructure(MetricKind.TIME_SERIES) == "time_series"

    def test_stages_nested_in_a_session_are_plain_strings_too(self) -> None:
        stages = SleepStages(
            source="garmin",
            samples=[
                IntervalSample(
                    timestamp=T0, value=SleepStage.REM, end_timestamp=T0 + dt.timedelta(hours=1)
                )
            ],
        )
        raw = converter.unstructure(stages)
        assert type(raw["samples"][0]["value"]) is str


class TestTimestamps:
    def test_datetimes_render_as_iso_8601_with_an_offset(self) -> None:
        raw = converter.unstructure(Sample(timestamp=T0, value=60.0))
        assert raw["timestamp"] == "2026-06-15T05:00:00+00:00"

    def test_a_naive_timestamp_is_refused_at_the_boundary(self) -> None:
        """isoformat() on a naive value emits an offsetless string, and the
        client's fromisoformat would hand the consumer a naive datetime -- an
        hours-wrong instant with no error anywhere."""
        with pytest.raises(ValueError, match="aware UTC"):
            assert_aware_utc(dt.datetime(2026, 6, 15, 5, 0))

    def test_a_non_utc_aware_timestamp_is_refused_too(self) -> None:
        denver = dt.datetime(2026, 6, 15, 5, 0, tzinfo=dt.timezone(-dt.timedelta(hours=6)))
        with pytest.raises(ValueError, match="aware UTC"):
            assert_aware_utc(denver)

    def test_an_aware_utc_timestamp_passes_through_unchanged(self) -> None:
        assert assert_aware_utc(T0) is T0

    def test_the_series_envelope_checks_every_sample(self) -> None:
        series = HeartRate(source="garmin", samples=[Sample(timestamp=T0, value=60.0)])
        naive = HeartRate(
            source="garmin", samples=[Sample(timestamp=dt.datetime(2026, 6, 15), value=60.0)]
        )
        time_series_payload(series)
        with pytest.raises(ValueError, match="aware UTC"):
            time_series_payload(naive)

    def test_the_sleep_envelope_checks_the_container_bounds(self) -> None:
        session = SleepSession(start=dt.datetime(2026, 6, 14, 23), end=T0, id="x", source="garmin")
        with pytest.raises(ValueError, match="aware UTC"):
            sleep_sessions_payload([session])


class TestEnvelopes:
    """Shapes read straight off client.py: anything else and the consumer KeyErrors."""

    def test_metrics_are_wrapped_in_a_metrics_key(self) -> None:
        descriptor = MetricType(metric_id="heart_rate", display_name="Heart Rate", unit="bpm")
        payload = metrics_payload([descriptor])
        assert list(payload) == ["metrics"]
        assert consumer_converter.structure(roundtrip(payload)["metrics"], list[MetricType]) == [
            descriptor
        ]

    def test_a_time_series_is_bare_with_no_envelope_at_all(self) -> None:
        """client.get_time_series structures resp.json() directly, not resp.json()["data"]."""
        payload = time_series_payload(HeartRate(source="garmin", samples=[]))
        assert payload["metric_id"] == "heart_rate"
        assert "data" not in payload

    def test_sleep_sessions_are_wrapped_in_a_data_key(self) -> None:
        session = SleepSession(start=T0, end=T0 + dt.timedelta(hours=8), id="x", source="garmin")
        payload = sleep_sessions_payload([session])
        assert list(payload) == ["data"]
        assert len(payload["data"]) == 1


class TestConsumerRoundTrip:
    def test_a_heart_rate_series_survives_the_consumers_converter(self) -> None:
        series = HeartRate(
            source="garmin",
            samples=[Sample(timestamp=T0, value=60.0), Sample(timestamp=T0, value=61.0)],
        )
        restored = consumer_converter.structure(roundtrip(time_series_payload(series)), TimeSeries)
        assert restored.metric_id == "heart_rate"
        assert restored.unit == "bpm"
        assert restored.source == "garmin"
        assert [s.value for s in restored.samples] == [60.0, 61.0]
        assert restored.samples[0].timestamp == T0

    def test_an_hrv_series_keeps_the_unit_we_overrode(self) -> None:
        """The spec defaults HRV_RMSSD.unit to None, but the column is RMSSD in ms."""
        series = HRV_RMSSD(source="garmin", unit="ms", samples=[])
        restored = consumer_converter.structure(roundtrip(time_series_payload(series)), TimeSeries)
        assert restored.unit == "ms"

    def test_sleep_stages_keep_end_timestamp_through_the_session(self) -> None:
        """The parametrized-generic path reaches gen_unstructure_attrs_fromdict and
        emits all three fields. This is the route sleep stages MUST travel."""
        end = T0 + dt.timedelta(hours=8)
        session = SleepSession(
            start=T0,
            end=end,
            id="garmin:sleep:2026-06-15",
            source="garmin",
            stages=SleepStages(
                source="garmin",
                samples=[
                    IntervalSample(
                        timestamp=T0,
                        value=SleepStage.DEEP,
                        end_timestamp=T0 + dt.timedelta(hours=1),
                    )
                ],
            ),
            total_duration=Duration(value=420.0, source="garmin"),
        )
        payload = roundtrip(sleep_sessions_payload([session]))
        restored = consumer_converter.structure(payload["data"], list[SleepSession])[0]

        assert restored.start == T0
        assert restored.end == end
        assert restored.id == "garmin:sleep:2026-06-15"
        assert restored.total_duration is not None
        assert restored.total_duration.value == 420.0

        assert restored.stages is not None
        sample = restored.stages.samples[0]
        assert isinstance(sample, IntervalSample)
        assert sample.end_timestamp == T0 + dt.timedelta(hours=1)
        assert sample.value == SleepStage.DEEP

    def test_the_consumers_sample_hook_drops_end_timestamp_on_a_bare_time_series(self) -> None:
        """This is why an interval-valued metric must never appear in a catalog.

        TimeSeries.samples is declared as bare list[Sample], and the client
        registers a structure hook for Sample that resolves by MRO -- so it fires
        for an IntervalSample too and silently discards end_timestamp. Asserted
        here so the hazard is a fact under test rather than a comment.
        """
        stages = SleepStages(
            source="garmin",
            samples=[
                IntervalSample(
                    timestamp=T0, value=SleepStage.DEEP, end_timestamp=T0 + dt.timedelta(hours=1)
                )
            ],
        )
        restored = consumer_converter.structure(roundtrip(time_series_payload(stages)), TimeSeries)
        assert not isinstance(restored.samples[0], IntervalSample)
        assert not hasattr(restored.samples[0], "end_timestamp")


def test_container_subclassing_keeps_start_end_id_positional_first() -> None:
    """attrs moves overridden base fields to the end, so construction order is not
    what the class body suggests. Everything we build uses keyword arguments; this
    pins the one case where the base fields did NOT move."""
    fields = [f.name for f in SleepSession.__attrs_attrs__]
    assert fields[:3] == ["start", "end", "id"]
    assert fields[-1] == "source"
    assert [f.name for f in Container.__attrs_attrs__] == ["start", "end", "id"]

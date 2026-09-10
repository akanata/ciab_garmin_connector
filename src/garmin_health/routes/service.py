"""``/v1/*`` -- the spec surface.

Deliberately **not** owner-gated: these arrive through the router's internal
service proxy, which strips any client-supplied ``X-OpenHost-*`` and stamps
consumer headers instead. The consumer id is logged, not gated on.

Status codes are part of the contract, because the consumer's ``_fan_out`` treats
any non-200 as "this provider has nothing":

===========================================  ======
Situation                                    Status
===========================================  ======
Unknown ``metric_id``                        404
Known metric, no data in range               200 with ``samples: []``
Window too large to scan                     413
Malformed ``start``/``end``/``limit``        400
Corpus unavailable (stale schema, no tz)     503
===========================================  ======

Timestamps are parsed here rather than by Litestar's msgspec decoder, because the
spec's client sends ``str(datetime)`` -- which renders a **space** separator, not a
``T``. A strict RFC 3339 decoder rejects that, and the consumer would read the
resulting 400 as "this provider has nothing" forever.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar.datastructures import State
from litestar.status_codes import HTTP_400_BAD_REQUEST
from litestar.status_codes import HTTP_404_NOT_FOUND
from litestar.status_codes import HTTP_413_REQUEST_ENTITY_TOO_LARGE
from litestar.status_codes import HTTP_503_SERVICE_UNAVAILABLE

from garmin_health.garmin.connection import GarminUnavailable
from garmin_health.garmin.sampling import InvalidLimit
from garmin_health.garmin.sampling import WindowTooLarge
from garmin_health.serialization import metrics_payload
from garmin_health.serialization import sleep_sessions_payload
from garmin_health.serialization import time_series_payload
from garmin_health.service import HealthDataService
from garmin_health.service import UnknownMetric
from garmin_health.timezones import TimeZoneUnresolved

logger = logging.getLogger(__name__)


class BadRequest(Exception):
    """A query parameter this service cannot make sense of. -> 400."""


def _service(state: State) -> HealthDataService:
    service: HealthDataService | None = state.get("health_service")
    if service is None:  # pragma: no cover - only if the lifespan never ran
        raise GarminUnavailable("The serving layer is not initialised yet.")
    return service


def _timestamp(raw: str | None, name: str) -> dt.datetime | None:
    """Parse a query bound into an aware UTC instant.

    ``fromisoformat`` is used precisely because it is lenient about the separator:
    the spec's own client sends ``str(datetime)``. A naive value is read as UTC --
    the spec says timestamps are aware UTC, so a naive one is a consumer that
    forgot to say so, not one meaning this container's local zone.
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise BadRequest(f"{name} is not an ISO 8601 timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        logger.debug("Read naive %s=%r as UTC.", name, raw)
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _log_consumer(request: Request[Any, Any, Any]) -> None:
    consumer = request.headers.get("x-openhost-consumer-name")
    if consumer:
        logger.info("Serving %s for consumer %s.", request.url.path, consumer)


@get("/metrics", sync_to_thread=True)
def list_metrics(state: State, request: Request[Any, Any, Any]) -> dict[str, Any]:
    _log_consumer(request)
    return metrics_payload(_service(state).metrics())


@get("/time-series", sync_to_thread=True)
def time_series(
    state: State,
    request: Request[Any, Any, Any],
    metric: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    _log_consumer(request)
    series = _service(state).time_series(
        metric, _timestamp(start, "start"), _timestamp(end, "end"), limit
    )
    return time_series_payload(series)


@get("/sleep-sessions", sync_to_thread=True)
def sleep_sessions(
    state: State,
    request: Request[Any, Any, Any],
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    _log_consumer(request)
    sessions = _service(state).sleep_sessions(
        _timestamp(start, "start"), _timestamp(end, "end"), limit
    )
    return sleep_sessions_payload(sessions)


@get("/workouts", sync_to_thread=False)
def list_workouts() -> dict[str, Any]:
    """Out of scope this iteration.

    An empty 200 rather than a non-200: the consumer collapses any error to
    "nothing", but an empty list is the cheaper and more truthful answer.
    """
    return {"data": []}


@get("/workouts/{workout_id:str}", status_code=HTTP_404_NOT_FOUND, sync_to_thread=False)
def get_workout(workout_id: str) -> dict[str, Any]:
    """``client.get_workout`` returns None on a 404, which is exactly right here."""
    return {"detail": f"No workout {workout_id}; workouts are not served by this provider."}


def _problem(status: int) -> Any:
    def handler(_: Request[Any, Any, Any], exc: Exception) -> Response[dict[str, Any]]:
        if status >= HTTP_503_SERVICE_UNAVAILABLE:
            logger.warning("Serving unavailable: %s", exc)
        return Response(content={"detail": str(exc)}, status_code=status)

    return handler


v1_router = Router(
    path="/v1",
    route_handlers=[list_metrics, time_series, sleep_sessions, list_workouts, get_workout],
    exception_handlers={
        UnknownMetric: _problem(HTTP_404_NOT_FOUND),
        WindowTooLarge: _problem(HTTP_413_REQUEST_ENTITY_TOO_LARGE),
        InvalidLimit: _problem(HTTP_400_BAD_REQUEST),
        BadRequest: _problem(HTTP_400_BAD_REQUEST),
        GarminUnavailable: _problem(HTTP_503_SERVICE_UNAVAILABLE),
        TimeZoneUnresolved: _problem(HTTP_503_SERVICE_UNAVAILABLE),
    },
)

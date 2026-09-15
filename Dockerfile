FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git: health-data-service is a git dependency (see pyproject.toml) and uv shells
#      out to a real git binary to fetch it.
# tzdata: zoneinfo reads the system tz database. Without it, resolving the Garmin
#      account's home timezone raises ZoneInfoNotFoundError at startup, and every
#      timestamp this service emits depends on that resolution.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deliberately python:3.12-slim, not Alpine: curl_cffi ships glibc binary wheels and
# garminconnect prefers it for TLS fingerprint impersonation. On musl it would fall
# back to plain requests, which Garmin's sign-in rejects far more often.
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen --no-dev

# Bootstrap value only. Set TZ to the Garmin account's home timezone (or export
# GARMIN_HOME_TZ) before the first sync: GarminDB renders sleep.start/sleep.end in
# the importing container's TZ, so a wrong zone here silently skews stored data.
ENV TZ=UTC

# No BOTTLE_APP_DATA_DIR (or any BOTTLE_*/OPENHOST_* variable) here. The router
# exports the app-data path itself and removes this container on every update, so
# only its bind mount survives. Routers before 2026-08-27 export only
# OPENHOST_APP_DATA_DIR, and a BOTTLE_ value baked in here would outrank it and put
# every synced file on the container's own disk. A bare `docker run` with neither
# variable still writes to ./data, i.e. /app/data.

EXPOSE 8080

CMD ["uv", "run", "--frozen", "--no-dev", "hypercorn", "garmin_health.app:app", "--bind", "0.0.0.0:8080"]

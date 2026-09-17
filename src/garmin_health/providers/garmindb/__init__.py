"""The GarminDB provider: a polling scraper over a local SQLite corpus.

This is the only package permitted to import garmindb, idbutils, fitfile or
sqlalchemy. Everything crossing this boundary is a health_data_service type or a
stdlib type, which is what lets a different acquisition strategy -- the official
Garmin API, or a webhook aggregator such as Terra, ROOK or Spike -- be added
alongside it without touching the HTTP layer.

The boundary is not finished. The scheduler (``sync.py``), the sign-in flow
(``auth.py``), the GarminDB config file (``garmin_config.py``), the download
scope (``preferences.py``) and the timezone policy (``timezones.py``) are all
GarminDB-specific and still live in the parent package; they move here in
``provider_refactor.md`` Phase 3, and nothing enforces the rule until Phase 5.
"""

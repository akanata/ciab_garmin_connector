"""The GarminDB provider: a polling scraper over a local SQLite corpus.

This is the only package permitted to import ``garmindb``, ``garminconnect``,
``idbutils``, ``fitfile`` or ``sqlalchemy``. Everything crossing this boundary is
a ``health_data_service`` type, a ``garmin_health.ports`` type, or a stdlib type
-- which is what lets a different acquisition strategy (the official Garmin API,
or a webhook aggregator such as Terra, ROOK or Spike) be added alongside it
without touching the HTTP layer.

Everything GarminDB-shaped lives here: the scheduler (``sync.py``), the sign-in
flow (``auth.py``), the config file (``config_file.py``), the download scope
(``preferences.py``), the timezone policy (``timezones.py``), the corpus readers,
and this provider's half of the owner page (``owner.py``). ``provider.py`` is the
entry point: the only object ``app.py`` ever holds, and it holds it as a
``ports.Provider``.

The rule is enforced rather than merely stated. ``TID251`` bans those imports
everywhere but here, and ``tests/test_boundary.py`` walks the import graph for
the half a lint rule cannot express -- that nothing in this package may import
``garmin_health.app``, ``.service`` or ``.routes``. A provider is a leaf. See
``plan.md`` Addendum A for the design of record.
"""

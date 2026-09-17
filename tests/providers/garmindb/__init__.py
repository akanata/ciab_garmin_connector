"""Tests for the GarminDB provider.

Everything here needs GarminDB itself: a real SQLite corpus built by
``fixtures.build_fixture``, or the GarminDB/garminconnect doubles in ``fakes``.
A package rather than a bare directory because later phases give the generic
suite files of the same name (``test_service_routes.py``), which pytest could
not otherwise tell apart.
"""

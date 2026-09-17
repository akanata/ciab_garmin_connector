"""The only package permitted to import garmindb, idbutils, fitfile or sqlalchemy.

Everything crossing this boundary is a health_data_service type or a stdlib type,
which is what lets GarminDB be swapped for the real Garmin API later without
touching the HTTP layer.
"""

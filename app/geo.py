"""Geography helpers. Pure functions, no I/O, so they're easy to test and reuse."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8
FEET_PER_METER = 3.28084


def feet_to_meters(ft: float) -> float:
    return ft / FEET_PER_METER


def meters_to_feet(m: float) -> float:
    return m * FEET_PER_METER


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

"""Geolocation helpers."""

from __future__ import annotations

import math


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two WGS84 coordinates in meters."""
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def within_geofence(
    latitude: float,
    longitude: float,
    branch_latitude: float,
    branch_longitude: float,
    radius_m: int,
) -> tuple[bool, float]:
    distance = haversine_distance_m(latitude, longitude, branch_latitude, branch_longitude)
    return distance <= radius_m, distance

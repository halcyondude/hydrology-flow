"""Recommend a projected CRS for an AOI geometry, using only the geometry itself."""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

UTM_ZONE_WIDTH_DEG = 6.0
UTM_ZONE_COUNT = 60
LON_ORIGIN = -180.0
EPSG_UTM_NORTH = 32600
EPSG_UTM_SOUTH = 32700


def _utm_zone(lon: float) -> int:
    return int((lon - LON_ORIGIN) // UTM_ZONE_WIDTH_DEG) % UTM_ZONE_COUNT + 1


def recommend_projected_crs(geometry: BaseGeometry) -> str:
    """Pick a CRS suited to area/distance work over ``geometry`` (EPSG:4326 input).

    Extents that fit in one UTM zone get that zone's EPSG code. Wider extents (large states,
    multi-state AOIs) get a local Lambert azimuthal equal-area PROJ string centred on the
    extent, which keeps areas true over any size.
    """
    west, south, east, north = geometry.bounds
    centre_lon = (west + east) / 2
    centre_lat = (south + north) / 2
    if _utm_zone(west) == _utm_zone(east):
        base = EPSG_UTM_NORTH if centre_lat >= 0 else EPSG_UTM_SOUTH
        return f"EPSG:{base + _utm_zone(centre_lon)}"
    return (
        f"+proj=laea +lat_0={centre_lat:.3f} +lon_0={centre_lon:.3f} "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )

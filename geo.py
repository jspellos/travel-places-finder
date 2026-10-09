"""
Pure geometry helpers for Travel Places Finder. No Streamlit or network
calls here — just math on (lat, lng) tuples, so it's easy to test.
"""

from math import radians, cos, sin, asin, sqrt
from typing import Optional


def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles."""
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return R * 2 * asin(sqrt(a))


def cumulative_miles(coords) -> list:
    """Arc length in miles at each vertex of a polyline (first entry is 0)."""
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_miles(
            coords[i - 1][0], coords[i - 1][1],
            coords[i][0],     coords[i][1],
        ))
    return cum


def locate_on_polyline(point_lat, point_lng, polyline_coords,
                       cum: Optional[list] = None):
    """
    Snap a point onto a polyline. Returns (off_route_miles, along_route_miles):
    the straight-line distance from the point to the nearest segment, and how
    far along the route (from its start) that nearest spot is.

    Projects into a local flat plane using equirectangular projection with a
    cos(latitude) correction for longitude. Accurate to well under 1% for the
    few-miles-scale distances this app cares about. Returns (None, None) if
    inputs are missing or the polyline is degenerate.

    Caveat: off-route is crow-flies distance to the road, not actual driving
    detour. A Starbucks 0.3 mi off the highway might be a 1.5 mi surface-street
    drive to reach. For a "is this a reasonable detour" judgment, crow-flies is
    fine and much cheaper than a second Routes API call per result.

    `cum` is the output of cumulative_miles(polyline_coords); pass it in when
    locating many points on the same route to avoid recomputing it each time.
    """
    if point_lat is None or point_lng is None:
        return None, None
    if not polyline_coords or len(polyline_coords) < 2:
        return None, None
    if cum is None:
        cum = cumulative_miles(polyline_coords)

    mi_per_deg_lat = 69.0
    mi_per_deg_lng = 69.0 * cos(radians(point_lat))

    # Translate so the query point sits at the origin of the local plane.
    best, best_i, best_t = float("inf"), 0, 0.0
    for i in range(len(polyline_coords) - 1):
        lat1, lng1 = polyline_coords[i]
        lat2, lng2 = polyline_coords[i + 1]
        ax = (lng1 - point_lng) * mi_per_deg_lng
        ay = (lat1 - point_lat) * mi_per_deg_lat
        bx = (lng2 - point_lng) * mi_per_deg_lng
        by = (lat2 - point_lat) * mi_per_deg_lat

        abx, aby = bx - ax, by - ay
        ab_sq = abx * abx + aby * aby
        if ab_sq < 1e-12:
            # Degenerate zero-length segment — fall back to distance from A.
            t = 0.0
        else:
            # Project point (at origin) onto segment AB, clamp parameter to [0,1]
            # so the nearest point is on the segment, not its infinite extension.
            t = (-ax * abx + -ay * aby) / ab_sq
            t = max(0.0, min(1.0, t))
        projx = ax + t * abx
        projy = ay + t * aby

        d = sqrt(projx * projx + projy * projy)
        if d < best:
            best, best_i, best_t = d, i, t

    along = cum[best_i] + best_t * (cum[best_i + 1] - cum[best_i])
    return round(best, 2), round(along, 1)


def chunk_polyline(coords, n_chunks: int, overlap_fraction: float = 0.05) -> list:
    """
    Split a polyline into n_chunks sub-polylines of roughly equal arc length,
    with a small overlap between adjacent chunks.

    Why: Google's searchAlongRoute returns up to 20 results ranked by their own
    relevance. On long routes through dense areas, those 20 slots get consumed
    near the origin before the algorithm reaches the destination end. Splitting
    the polyline into chunks and querying each independently gives every
    segment of the trip its own result budget.

    Overlap prevents a result sitting exactly on a chunk boundary from falling
    through the cracks of how Google treats segment endpoints.

    Returns a list of (lat, lng) sublists, each suitable for re-encoding.
    If the polyline is too short or n_chunks <= 1, returns [coords] unchanged.
    """
    if n_chunks <= 1 or len(coords) < 4:
        return [coords]

    cum = cumulative_miles(coords)
    total = cum[-1]
    if total < 1.0:  # sub-mile route — chunking is meaningless
        return [coords]

    chunk_len = total / n_chunks
    overlap = chunk_len * overlap_fraction

    chunks = []
    for k in range(n_chunks):
        start = max(0.0, k * chunk_len - overlap)
        end   = min(total, (k + 1) * chunk_len + overlap)

        # Find the vertex range that covers [start, end]. We want to include
        # the last vertex at-or-before start and the first vertex at-or-after
        # end, so the resulting sub-polyline fully spans the window.
        start_idx = 0
        while start_idx < len(cum) - 1 and cum[start_idx + 1] < start:
            start_idx += 1
        end_idx = len(coords) - 1
        while end_idx > 0 and cum[end_idx - 1] > end:
            end_idx -= 1

        chunk = coords[start_idx:end_idx + 1]
        if len(chunk) >= 2:
            chunks.append(chunk)

    return chunks if chunks else [coords]


def trim_polyline_start(coords, skip_miles: float):
    """
    Return (trimmed_coords, skipped_miles) — the portion of the polyline
    starting at approximately `skip_miles` of arc length from the origin.

    Why: along-route search is for discovery mid-trip and toward the
    destination. Results clustered near the origin are better served by the
    "Near a place" mode. Skipping the first N miles lets the route mode
    escape the dense origin area and focus on what's actually ahead.

    If the route is too short to trim meaningfully (< skip + 5 mi), returns
    (coords, 0.0) so the caller falls back to the full route.
    """
    if skip_miles <= 0 or len(coords) < 2:
        return coords, 0.0

    cum = cumulative_miles(coords)
    total = cum[-1]
    if total < skip_miles + 5:
        return coords, 0.0  # trip too short to skip meaningfully

    # First vertex at or past the skip threshold.
    start_idx = 0
    while start_idx < len(cum) and cum[start_idx] < skip_miles:
        start_idx += 1
    if start_idx >= len(coords):
        return coords, 0.0  # safety fallback

    return coords[start_idx:], cum[start_idx]

"""
Travel Places Finder
--------------------
Find places (restaurants, shops, hotels, etc.) at destinations you're planning
to visit — not just where you are now.

Three search modes:
  1. Near a location  - radius search around any address/landmark/hotel
  2. Along a route    - search along the driving path between two points

Uses Google Maps Platform:
  - Geocoding API       (address -> lat/lng)
  - Places API (New)    (find places)
  - Routes API          (compute driving polylines)
"""

import streamlit as st
import requests
import folium
from streamlit_folium import st_folium
import pandas as pd
import polyline as polyline_lib
from math import radians, cos, sin, asin, sqrt
from typing import Optional

# -------------------- Config --------------------
st.set_page_config(
    page_title="Travel Places Finder",
    page_icon="🗺️",
    layout="wide",
)

# Mobile readability: bump text sizes on narrow viewports without touching
# the desktop layout. Uses a single @media query so desktop stays default.
st.markdown("""
<style>
@media (max-width: 768px) {
    html, body, [class*="css"] { font-size: 16px !important; }
    div[data-testid="stMarkdownContainer"] p,
    div[data-testid="stMarkdownContainer"] li { font-size: 16px !important; }
    .stTextInput input, .stTextArea textarea,
    .stSelectbox, .stSlider { font-size: 16px !important; }
    .stDataFrame, .stDataFrame * { font-size: 13px !important; }
    h1 { font-size: 1.6rem !important; }
    h2 { font-size: 1.3rem !important; }
    h3 { font-size: 1.15rem !important; }
    .stAlert, .stAlert * { font-size: 15px !important; }
    /* Folium popups render inline; inline styles below are the real fix,
       this is the safety net. */
    .leaflet-popup-content { font-size: 14px !important; line-height: 1.4 !important; }
}
</style>
""", unsafe_allow_html=True)

# Pull API key from Streamlit secrets. On Streamlit Cloud, set this in the
# app's Secrets panel. Locally, put it in .streamlit/secrets.toml
try:
    API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "")
except Exception:
    API_KEY = ""

if not API_KEY:
    st.error("⚠️ Google Maps API key not found.")
    st.markdown(
        """
        **Setup:** add your API key to `.streamlit/secrets.toml`:
        ```toml
        GOOGLE_MAPS_API_KEY = "your-key-here"
        ```

        In Google Cloud Console, make sure these APIs are enabled:
        - Geocoding API
        - Places API (New)
        - Routes API
        """
    )
    st.stop()


# -------------------- API helpers --------------------

# The Places API (New) requires an explicit field mask. Asking for only
# what we use keeps responses small and billing predictable.
PLACES_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.regularOpeningHours.weekdayDescriptions",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.types",
])


@st.cache_data(ttl=3600, show_spinner=False)
def geocode_address(address: str) -> Optional[dict]:
    """Convert an address/place name to coordinates via Geocoding API."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
    except Exception as e:
        st.error(f"Geocoding network error: {e}")
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None

    result = data["results"][0]
    loc = result["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "formatted_address": result["formatted_address"],
    }


@st.cache_data(ttl=300, show_spinner=False)
def places_text_search(query: str, lat: float, lng: float, radius_meters: float) -> list:
    """Text search near a location using Places API (New)."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_meters),
            }
        },
        "maxResultCount": 20,
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        st.error(f"Places search network error: {e}")
        return []

    if "error" in data:
        st.error(f"Places API error: {data['error'].get('message', 'unknown')}")
        return []
    return data.get("places", [])


@st.cache_data(ttl=3600, show_spinner=False)
def compute_route(origin_lat: float, origin_lng: float,
                  dest_lat: float, dest_lng: float) -> Optional[dict]:
    """Compute a driving route between two coordinates via Routes API."""
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "routes.polyline.encodedPolyline,routes.duration,routes.distanceMeters",
    }
    body = {
        "origin":      {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat,   "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        st.error(f"Routes network error: {e}")
        return None

    if "error" in data:
        st.error(f"Routes API error: {data['error'].get('message', 'unknown')}")
        return None

    routes = data.get("routes", [])
    if not routes:
        return None

    return {
        "polyline": routes[0]["polyline"]["encodedPolyline"],
        "duration_sec": int(str(routes[0]["duration"]).rstrip("s")),
        "distance_meters": routes[0]["distanceMeters"],
    }


@st.cache_data(ttl=300, show_spinner=False)
def places_search_along_route(query: str, encoded_polyline: str) -> list:
    """Find places along a route. THIS is the killer feature."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "searchAlongRouteParameters": {
            "polyline": {"encodedPolyline": encoded_polyline}
        },
        "maxResultCount": 20,
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        st.error(f"Along-route search network error: {e}")
        return []

    if "error" in data:
        st.error(f"Places API error: {data['error'].get('message', 'unknown')}")
        return []
    return data.get("places", [])


# -------------------- Data helpers --------------------

def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles."""
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return R * 2 * asin(sqrt(a))


def point_to_polyline_miles(point_lat, point_lng, polyline_coords) -> Optional[float]:
    """
    Minimum straight-line distance in miles from a point to the nearest segment
    of a polyline. `polyline_coords` is a list of (lat, lng) tuples.

    Projects into a local flat plane using equirectangular projection with a
    cos(latitude) correction for longitude. Accurate to well under 1% for the
    few-miles-scale distances this app cares about. Returns None if inputs
    are missing or the polyline is degenerate.

    Caveat: this is crow-flies distance to the road, not actual driving detour.
    A Starbucks 0.3 mi off the highway might be a 1.5 mi surface-street drive
    to reach. For a "is this a reasonable detour" judgment, crow-flies is fine
    and much cheaper than a second Routes API call per result.
    """
    if point_lat is None or point_lng is None:
        return None
    if not polyline_coords or len(polyline_coords) < 2:
        return None

    mi_per_deg_lat = 69.0
    mi_per_deg_lng = 69.0 * cos(radians(point_lat))

    # Translate so the query point sits at the origin of the local plane.
    best = float("inf")
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
            dx, dy = -ax, -ay
        else:
            # Project point (at origin) onto segment AB, clamp parameter to [0,1]
            # so the nearest point is on the segment, not its infinite extension.
            t = (-ax * abx + -ay * aby) / ab_sq
            t = max(0.0, min(1.0, t))
            projx = ax + t * abx
            projy = ay + t * aby
            dx, dy = -projx, -projy

        d = sqrt(dx * dx + dy * dy)
        if d < best:
            best = d

    return round(best, 2)


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

    # Cumulative arc length at each vertex, in miles.
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_miles(
            coords[i - 1][0], coords[i - 1][1],
            coords[i][0],     coords[i][1],
        ))
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
    "Near a location" mode. Skipping the first N miles lets the route mode
    escape the dense origin area and focus on what's actually ahead.

    If the route is too short to trim meaningfully (< skip + 5 mi), returns
    (coords, 0.0) so the caller falls back to the full route.
    """
    if skip_miles <= 0 or len(coords) < 2:
        return coords, 0.0

    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_miles(
            coords[i - 1][0], coords[i - 1][1],
            coords[i][0],     coords[i][1],
        ))
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


PRICE_LEVELS = {
    "PRICE_LEVEL_FREE":           "Free",
    "PRICE_LEVEL_INEXPENSIVE":    "$",
    "PRICE_LEVEL_MODERATE":       "$$",
    "PRICE_LEVEL_EXPENSIVE":      "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def places_to_dataframe(places, center_lat=None, center_lng=None) -> pd.DataFrame:
    rows = []
    for p in places:
        loc = p.get("location", {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        distance = None
        if center_lat is not None and lat is not None:
            distance = round(haversine_miles(center_lat, center_lng, lat, lng), 2)
        hours = p.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
        rows.append({
            "Name":          p.get("displayName", {}).get("text", "Unknown"),
            "Rating":        p.get("rating"),
            "Reviews":       p.get("userRatingCount"),
            "Price":         PRICE_LEVELS.get(p.get("priceLevel", ""), ""),
            "Distance (mi)": distance,
            "Address":       p.get("formattedAddress", ""),
            "Phone":         p.get("nationalPhoneNumber", ""),
            "Website":       p.get("websiteUri", ""),
            "Hours":         " | ".join(hours) if hours else "",
            "lat":           lat,
            "lng":           lng,
        })
    return pd.DataFrame(rows)


def render_map(df, center_lat, center_lng, center_label="Search center",
               polyline_str=None, endpoint_markers=None):
    """Build a Folium map with center, results, and optional route."""
    m = folium.Map(location=[center_lat, center_lng], zoom_start=11)

    # Center marker (red star) — only show when it's a meaningful anchor
    if center_label and not endpoint_markers:
        folium.Marker(
            [center_lat, center_lng],
            popup=center_label,
            icon=folium.Icon(color="red", icon="star", prefix="fa"),
        ).add_to(m)

    # Endpoint markers (green flag for origin, dark-red flag for destination)
    if endpoint_markers:
        for label, lat, lng, color in endpoint_markers:
            folium.Marker(
                [lat, lng],
                popup=label,
                icon=folium.Icon(color=color, icon="flag", prefix="fa"),
            ).add_to(m)

    # Route polyline
    if polyline_str:
        coords = polyline_lib.decode(polyline_str)
        folium.PolyLine(coords, color="#1f77b4", weight=4, opacity=0.6).add_to(m)

    # Result markers
    for _, row in df.iterrows():
        if pd.notna(row.get("lat")) and pd.notna(row.get("lng")):
            # Inline styles here are load-bearing: Folium popups render in a
            # Leaflet div that doesn't reliably inherit the parent page's CSS,
            # so the @media query from main app CSS can't be trusted alone.
            popup_html = (
                "<div style='font-size:14px;line-height:1.5;min-width:220px;"
                "font-family:-apple-system,BlinkMacSystemFont,sans-serif;'>"
            )
            popup_html += f"<b style='font-size:15px;'>{row['Name']}</b><br>"
            if pd.notna(row.get("Rating")):
                popup_html += f"⭐ {row['Rating']} ({int(row['Reviews'])} reviews)<br>"
            if row.get("Price"):
                popup_html += f"Price: {row['Price']}<br>"
            # Distance: prefer "Off route" (route mode), else "Distance" (near mode).
            off_route = row.get("Off route (mi)")
            distance = row.get("Distance (mi)")
            if pd.notna(off_route):
                popup_html += f"🛣️ <b>{off_route} mi</b> off route<br>"
            elif pd.notna(distance):
                popup_html += f"📍 {distance} mi away<br>"
            popup_html += f"{row['Address']}<br>"
            if row.get("Phone"):
                popup_html += f"📞 {row['Phone']}<br>"
            if row.get("Website"):
                popup_html += (
                    f"<a href='{row['Website']}' target='_blank' "
                    f"style='font-size:14px;'>Website</a>"
                )
            popup_html += "</div>"
            folium.Marker(
                [row["lat"], row["lng"]],
                popup=folium.Popup(popup_html, max_width=320),
                icon=folium.Icon(color="blue"),
            ).add_to(m)

    # Fit bounds to everything
    all_lats, all_lngs = [center_lat], [center_lng]
    all_lats += df["lat"].dropna().tolist()
    all_lngs += df["lng"].dropna().tolist()
    if endpoint_markers:
        for _, lat, lng, _ in endpoint_markers:
            all_lats.append(lat)
            all_lngs.append(lng)
    if len(all_lats) > 1:
        m.fit_bounds(
            [[min(all_lats), min(all_lngs)], [max(all_lats), max(all_lngs)]],
            padding=(30, 30),
        )

    return m


def results_table(df):
    """Render the details table with consistent formatting."""
    # Show up to ~18 rows before scrolling. Streamlit rows are roughly 35px
    # plus a 38px header; cap at 18 so the table doesn't dominate the page
    # on short result sets and doesn't sprawl endlessly on long ones.
    visible_rows = min(len(df), 18)
    table_height = visible_rows * 35 + 38
    st.dataframe(
        df[["Name", "Rating", "Reviews", "Price", "Distance (mi)",
            "Address", "Phone", "Website", "Hours"]],
        use_container_width=True,
        hide_index=True,
        height=table_height,
        column_config={
            "Website": st.column_config.LinkColumn("Website"),
            "Rating":  st.column_config.NumberColumn("⭐", format="%.1f"),
        },
    )


# -------------------- UI --------------------

st.title("🗺️ Travel Places Finder")
st.caption("Find places at destinations you're planning to visit — not just where you are now.")

mode = st.sidebar.radio(
    "Search mode",
    ["📍 Near a location", "🛣️ Along a route"],
    help=(
        "Near a location: find places within X miles of a destination, hotel, "
        "or landmark. Along a route: find places on the drive between two points."
    ),
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Why this exists**")
st.sidebar.caption(
    "Google Maps excels at 'what's near me right now' but struggles with "
    "'what's near where I'll be next Tuesday.' This fills that gap."
)


# -------- Mode 1: Near a location --------
if mode == "📍 Near a location":
    st.subheader("Find places near a location")

    col1, col2 = st.columns([2, 1])
    with col1:
        location = st.text_input(
            "Location (city, address, hotel name, or landmark)",
            placeholder="Ossining, NY  —  or  —  Hilton Garden Inn Tarrytown",
            key="near_location",
        )
        query = st.text_input(
            "What are you looking for?",
            placeholder="vegan restaurants, Whole Foods, coffee shops, bookstores…",
            key="near_query",
        )
    with col2:
        radius_miles = st.slider("Within (miles)", 1, 50, 20, key="near_radius")

    # When Search is pressed, do the work and stash results in session_state
    # so they survive the reruns that Streamlit triggers on any UI interaction.
    if st.button("🔍 Search", type="primary", key="near_search"):
        if not location or not query:
            st.warning("Please fill in both location and search query.")
        else:
            with st.spinner(f"Looking up '{location}'…"):
                geo = geocode_address(location)
            if not geo:
                st.error(f"Couldn't find '{location}'. Try being more specific.")
                st.stop()

            with st.spinner(f"Searching for '{query}' within {radius_miles} miles…"):
                places = places_text_search(
                    query, geo["lat"], geo["lng"], radius_miles * 1609.34
                )

            if not places:
                st.warning("No results found. Try a wider radius or different query.")
                st.session_state.pop("near_results", None)  # clear any stale results
            else:
                df = places_to_dataframe(places, geo["lat"], geo["lng"])
                df = df.sort_values("Distance (mi)").reset_index(drop=True)
                st.session_state["near_results"] = {
                    "geo": geo,
                    "df": df,
                    "query": query,
                    "radius": radius_miles,
                }

    # Render from session_state (survives reruns from map interactions, tab
    # switches, etc.). Nothing renders until the first successful search.
    if "near_results" in st.session_state:
        res = st.session_state["near_results"]
        st.success(f"📍 {res['geo']['formatted_address']}")
        st.success(f"Found {len(res['df'])} result(s) for '{res['query']}' "
                   f"within {res['radius']} miles — sorted by distance")

        tab_map, tab_table = st.tabs(["🗺️ Map", "📋 Details"])
        with tab_map:
            m = render_map(res["df"], res["geo"]["lat"], res["geo"]["lng"],
                           center_label=res["geo"]["formatted_address"])
            st_folium(m, height=600, use_container_width=True,
                      returned_objects=[], key="near_map")
        with tab_table:
            results_table(res["df"])


# -------- Mode 2: Along a route --------
elif mode == "🛣️ Along a route":
    st.subheader("Find places along a driving route")

    col1, col2 = st.columns(2)
    with col1:
        origin = st.text_input("From", placeholder="Queens, NY", key="route_origin")
    with col2:
        destination = st.text_input("To", placeholder="Croton-on-Hudson, NY", key="route_dest")

    query = st.text_input(
        "What are you looking for along the way?",
        placeholder="Starbucks, Whole Foods, charging stations, rest stops…",
        key="route_query",
    )

    # Route mode is for discovery mid-trip and toward the destination. The
    # "Near a location" mode covers the origin area. Default 25 mi — tunable
    # because "25 mi out of Manhattan" ≠ "25 mi out of Poughkeepsie."
    skip_miles = st.slider(
        "Skip first N miles",
        min_value=0, max_value=50, value=25, step=5,
        key="route_skip",
        help=("Exclude results clustered near your origin — use "
              "'Near a location' mode for those. This mode is for what's "
              "ahead on your trip."),
    )

    if st.button("🔍 Search", type="primary", key="route_search"):
        if not origin or not destination or not query:
            st.warning("Please fill in all three fields.")
        else:
            with st.spinner("Geocoding origin and destination…"):
                origin_geo = geocode_address(origin)
                dest_geo = geocode_address(destination)

            if not origin_geo or not dest_geo:
                st.error("Couldn't find one of the locations. Try being more specific.")
                st.stop()

            with st.spinner("Computing driving route…"):
                route = compute_route(
                    origin_geo["lat"], origin_geo["lng"],
                    dest_geo["lat"], dest_geo["lng"],
                )

            if not route:
                st.error("Couldn't compute a route between those locations.")
                st.stop()

            # Decode the full route and optionally trim the start. The full
            # polyline is used for off-route distance (so "0.3 mi off route"
            # means off the actual highway) and for drawing the map. The
            # trimmed polyline is what we send to the Places API.
            route_miles = route["distance_meters"] / 1609.34
            route_coords = polyline_lib.decode(route["polyline"])
            search_coords, skipped = trim_polyline_start(route_coords, skip_miles)
            effective_miles = route_miles - skipped

            # Chunking decisions based on the *effective* length we're actually
            # searching. One chunk per ~40 mi, capped at 5.
            n_chunks = max(1, min(5, 1 + int(effective_miles // 40)))

            skip_note = f" · skipping first {skipped:.0f} mi" if skipped > 0 else ""

            if n_chunks == 1:
                with st.spinner(f"Searching for '{query}' along route{skip_note}…"):
                    encoded = (route["polyline"] if search_coords is route_coords
                               else polyline_lib.encode(search_coords))
                    places = places_search_along_route(query, encoded)
            else:
                with st.spinner(
                    f"Searching for '{query}' along route{skip_note} · "
                    f"{n_chunks} segments of ~{effective_miles / n_chunks:.0f} mi each…"
                ):
                    chunks = chunk_polyline(search_coords, n_chunks)
                    places = []
                    seen_ids = set()
                    for sub_coords in chunks:
                        encoded = polyline_lib.encode(sub_coords)
                        sub_places = places_search_along_route(query, encoded)
                        for p in sub_places:
                            pid = p.get("id")
                            # Dedupe by place_id; a Starbucks near a chunk
                            # boundary will appear in both adjacent chunks.
                            if pid and pid not in seen_ids:
                                seen_ids.add(pid)
                                places.append(p)

            if not places:
                st.warning("No results found along this route.")
                st.session_state.pop("route_results", None)
            else:
                mid_lat = (origin_geo["lat"] + dest_geo["lat"]) / 2
                mid_lng = (origin_geo["lng"] + dest_geo["lng"]) / 2

                df = places_to_dataframe(places, mid_lat, mid_lng)

                # Decode the route polyline once and compute each result's
                # perpendicular distance to the nearest segment. This answers
                # "how far off the highway is this?" — the actual question
                # a driver has when considering a detour.
                route_coords = polyline_lib.decode(route["polyline"])
                df["Off route (mi)"] = df.apply(
                    lambda r: point_to_polyline_miles(
                        r["lat"], r["lng"], route_coords
                    ) if pd.notna(r["lat"]) else None,
                    axis=1,
                )

                # Also track distance from origin as a rough "where along the
                # trip is this" indicator. Not a sort key anymore, just context.
                df["From origin (mi)"] = df.apply(
                    lambda r: round(haversine_miles(
                        origin_geo["lat"], origin_geo["lng"],
                        r["lat"], r["lng"],
                    ), 2) if pd.notna(r["lat"]) else None,
                    axis=1,
                )

                # The haversine-from-midpoint "Distance (mi)" that
                # places_to_dataframe computed is meaningless here.
                df = df.drop(columns=["Distance (mi)"])

                # Primary sort: minimize detour. Near-zero-off-route results
                # bubble to the top — exactly what a driver on the highway wants.
                df = df.sort_values(
                    "Off route (mi)", na_position="last"
                ).reset_index(drop=True)

                st.session_state["route_results"] = {
                    "origin_geo": origin_geo,
                    "dest_geo": dest_geo,
                    "route": route,
                    "df": df,
                    "query": query,
                    "mid_lat": mid_lat,
                    "mid_lng": mid_lng,
                    "skipped_miles": skipped,
                    "skip_requested": skip_miles,
                }

    # Render from session_state
    if "route_results" in st.session_state:
        res = st.session_state["route_results"]
        minutes = res["route"]["duration_sec"] // 60
        miles = res["route"]["distance_meters"] / 1609.34
        skipped = res.get("skipped_miles", 0)
        skip_requested = res.get("skip_requested", 0)

        if skipped > 0:
            st.success(
                f"🛣️ Route: {miles:.1f} mi · ~{minutes} min driving · "
                f"searched last {miles - skipped:.0f} mi "
                f"(skipped first {skipped:.0f})"
            )
        elif skip_requested > 0:
            st.info(
                f"🛣️ Route: {miles:.1f} mi · ~{minutes} min driving · "
                f"too short to skip {skip_requested} mi — searched entire route"
            )
        else:
            st.success(f"🛣️ Route: {miles:.1f} mi · ~{minutes} min driving")

        df_all = res["df"]

        # Live detour filter — changes re-run the script but stored results
        # stay in session_state, so no API call is triggered.
        max_off = df_all["Off route (mi)"].dropna().max()
        if pd.notna(max_off) and max_off > 0.5:
            slider_max = float(round(max_off + 0.5, 1))
            default_val = float(min(max_off, 5.0))
            detour_limit = st.slider(
                "Max detour off route (miles)",
                min_value=0.1,
                max_value=slider_max,
                value=default_val,
                step=0.1,
                key="route_detour_limit",
                help=("Filter out results that would require driving further off "
                      "the route. Straight-line distance to the road, not actual "
                      "driving detour."),
            )
            df_view = df_all[df_all["Off route (mi)"] <= detour_limit].reset_index(drop=True)
        else:
            df_view = df_all

        st.success(
            f"Showing {len(df_view)} of {len(df_all)} result(s) for "
            f"'{res['query']}' — sorted by detour distance"
        )

        tab_map, tab_table = st.tabs(["🗺️ Map", "📋 Details"])
        with tab_map:
            m = render_map(
                df_view,
                res["mid_lat"], res["mid_lng"],
                center_label=None,
                polyline_str=res["route"]["polyline"],
                endpoint_markers=[
                    (f"Start: {res['origin_geo']['formatted_address']}",
                     res["origin_geo"]["lat"], res["origin_geo"]["lng"], "green"),
                    (f"End: {res['dest_geo']['formatted_address']}",
                     res["dest_geo"]["lat"], res["dest_geo"]["lng"], "darkred"),
                ],
            )
            st_folium(m, height=600, use_container_width=True,
                      returned_objects=[], key="route_map")
        with tab_table:
            visible_rows = min(len(df_view), 18)
            table_height = visible_rows * 35 + 38
            st.dataframe(
                df_view[["Name", "Rating", "Reviews", "Price",
                         "Off route (mi)", "From origin (mi)",
                         "Address", "Phone", "Website", "Hours"]],
                use_container_width=True,
                hide_index=True,
                height=table_height,
                column_config={
                    "Website": st.column_config.LinkColumn("Website"),
                    "Rating":  st.column_config.NumberColumn("⭐", format="%.1f"),
                    "Off route (mi)":   st.column_config.NumberColumn(
                        "Off route (mi)", format="%.2f"
                    ),
                    "From origin (mi)": st.column_config.NumberColumn(
                        "From origin (mi)", format="%.2f"
                    ),
                },
            )

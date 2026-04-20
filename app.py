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
            popup_html = f"<b>{row['Name']}</b><br>"
            if pd.notna(row.get("Rating")):
                popup_html += f"⭐ {row['Rating']} ({int(row['Reviews'])} reviews)<br>"
            if row.get("Price"):
                popup_html += f"Price: {row['Price']}<br>"
            popup_html += f"{row['Address']}<br>"
            if row.get("Phone"):
                popup_html += f"📞 {row['Phone']}<br>"
            if row.get("Website"):
                popup_html += f"<a href='{row['Website']}' target='_blank'>Website</a>"
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
    st.dataframe(
        df[["Name", "Rating", "Reviews", "Price", "Distance (mi)",
            "Address", "Phone", "Website", "Hours"]],
        use_container_width=True,
        hide_index=True,
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

    if st.button("🔍 Search", type="primary", key="near_search"):
        if not location or not query:
            st.warning("Please fill in both location and search query.")
        else:
            with st.spinner(f"Looking up '{location}'…"):
                geo = geocode_address(location)
            if not geo:
                st.error(f"Couldn't find '{location}'. Try being more specific.")
                st.stop()

            st.success(f"📍 {geo['formatted_address']}")

            with st.spinner(f"Searching for '{query}' within {radius_miles} miles…"):
                places = places_text_search(
                    query, geo["lat"], geo["lng"], radius_miles * 1609.34
                )

            if not places:
                st.warning("No results found. Try a wider radius or different query.")
            else:
                df = places_to_dataframe(places, geo["lat"], geo["lng"])
                df = df.sort_values("Distance (mi)").reset_index(drop=True)

                st.success(f"Found {len(df)} result(s) — sorted by distance")

                tab_map, tab_table = st.tabs(["🗺️ Map", "📋 Details"])
                with tab_map:
                    m = render_map(df, geo["lat"], geo["lng"],
                                   center_label=geo["formatted_address"])
                    st_folium(m, height=600, use_container_width=True)
                with tab_table:
                    results_table(df)


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

            minutes = route["duration_sec"] // 60
            miles = route["distance_meters"] / 1609.34
            st.success(f"🛣️ Route: {miles:.1f} miles · ~{minutes} min driving")

            with st.spinner(f"Searching for '{query}' along the route…"):
                places = places_search_along_route(query, route["polyline"])

            if not places:
                st.warning("No results found along this route.")
            else:
                # Map center = midpoint; sort results by distance from origin
                # (rough approximation of route order — good enough for v1)
                mid_lat = (origin_geo["lat"] + dest_geo["lat"]) / 2
                mid_lng = (origin_geo["lng"] + dest_geo["lng"]) / 2

                df = places_to_dataframe(places, mid_lat, mid_lng)
                df["_from_origin"] = df.apply(
                    lambda r: haversine_miles(
                        origin_geo["lat"], origin_geo["lng"], r["lat"], r["lng"]
                    ) if pd.notna(r["lat"]) else 999,
                    axis=1,
                )
                df = (df.sort_values("_from_origin")
                        .drop(columns=["_from_origin"])
                        .reset_index(drop=True))
                df["Distance (mi)"] = df.apply(
                    lambda r: round(haversine_miles(
                        origin_geo["lat"], origin_geo["lng"], r["lat"], r["lng"]
                    ), 2) if pd.notna(r["lat"]) else None,
                    axis=1,
                )
                df = df.rename(columns={"Distance (mi)": "From origin (mi)"})

                st.success(f"Found {len(df)} result(s) along the route")

                tab_map, tab_table = st.tabs(["🗺️ Map", "📋 Details"])
                with tab_map:
                    m = render_map(
                        df.rename(columns={"From origin (mi)": "Distance (mi)"}),
                        mid_lat, mid_lng,
                        center_label=None,
                        polyline_str=route["polyline"],
                        endpoint_markers=[
                            (f"Start: {origin_geo['formatted_address']}",
                             origin_geo["lat"], origin_geo["lng"], "green"),
                            (f"End: {dest_geo['formatted_address']}",
                             dest_geo["lat"], dest_geo["lng"], "darkred"),
                        ],
                    )
                    st_folium(m, height=600, use_container_width=True)
                with tab_table:
                    st.dataframe(
                        df[["Name", "Rating", "Reviews", "Price",
                            "From origin (mi)", "Address", "Phone", "Website", "Hours"]],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Website": st.column_config.LinkColumn("Website"),
                            "Rating":  st.column_config.NumberColumn("⭐", format="%.1f"),
                        },
                    )

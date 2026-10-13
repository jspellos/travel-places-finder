"""
Travel Places Finder
--------------------
Find places (restaurants, shops, hotels, etc.) at destinations you're planning
to visit — not just where you are now.

Two search modes:
  1. Near a place     - radius search around any address/landmark/hotel
  2. Along a drive    - search along the driving path between two points

Uses Google Maps Platform:
  - Geocoding API       (address <-> lat/lng)
  - Places API (New)    (find places)
  - Routes API          (compute driving polylines)
"""

import hashlib
import hmac
import html
from datetime import datetime, timedelta, timezone
from math import cos, radians
from typing import Optional
from urllib.parse import quote

import streamlit as st
import requests
import folium
from streamlit_folium import st_folium
import pandas as pd
import polyline as polyline_lib

from geo import (
    haversine_miles, cumulative_miles, locate_on_polyline,
    chunk_polyline, trim_polyline_start,
)

# Anthropic is optional — the app runs fine without it, the natural-language
# shortcut just hides itself. Keeps the app working for anyone cloning the
# repo who doesn't have a Claude API key yet.
try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# Same idea for the small JS component behind "Use my location" and the
# "remember this phone" login storage.
try:
    from streamlit_js_eval import get_geolocation, streamlit_js_eval
    _JS_EVAL_AVAILABLE = True
except ImportError:
    _JS_EVAL_AVAILABLE = False

# -------------------- Config --------------------
st.set_page_config(
    page_title="Travel Places Finder",
    page_icon="🗺️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

BRAND = "#0F766E"  # keep in sync with primaryColor in .streamlit/config.toml

# Global CSS. Two things worth knowing:
#   (1) st.html() bypasses Streamlit's markdown pipeline, which sometimes
#       mangles CSS blocks (and was rendering a stray "}" at the top of the
#       page when we used st.markdown).
#   (2) <meta name="viewport"> doesn't work via Python injection — it has to
#       be in <head>, not <body>. Streamlit Cloud already sets a correct
#       viewport by default, so we don't need to re-add it.
#
# Colors in the result cards use inherit / translucent greys so they read
# correctly in both Streamlit's light and dark themes.
st.html(f"""
<style>
/* ---------- Result cards ---------- */
.tpf-card {{
    border: 1px solid rgba(128,128,128,.28);
    border-radius: 14px;
    padding: 14px 14px 12px 14px;
    margin-bottom: 12px;
}}
.tpf-head {{ display: flex; gap: 10px; align-items: flex-start; }}
.tpf-num {{
    flex: none; width: 26px; height: 26px; border-radius: 50%;
    background: {BRAND}; color: #fff; font-weight: 700; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
    margin-top: 1px;
}}
.tpf-name {{
    font-weight: 700; font-size: 1.05rem; line-height: 1.3;
    color: inherit !important; text-decoration: none !important;
}}
.tpf-meta {{ font-size: .92rem; margin-top: 6px; }}
.tpf-muted {{ opacity: .65; }}
.tpf-open {{ color: #16A34A; font-weight: 600; }}
.tpf-closed {{ color: #DC2626; font-weight: 600; }}
.tpf-addr {{ font-size: .88rem; opacity: .7; margin-top: 4px; }}
.tpf-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
.tpf-btn {{
    flex: 1 1 0; min-width: 88px; text-align: center;
    padding: 9px 10px; border-radius: 10px;
    border: 1px solid rgba(128,128,128,.35);
    font-weight: 600; font-size: .92rem;
    color: inherit !important; text-decoration: none !important;
}}
.tpf-btn.primary {{ background: {BRAND}; border-color: {BRAND}; color: #fff !important; }}
.tpf-card details {{ margin-top: 10px; font-size: .85rem; }}
.tpf-card summary {{ cursor: pointer; opacity: .75; }}
.tpf-card details div {{ margin-top: 4px; opacity: .85; }}

/* ---------- Phones ---------- */
@media (max-width: 768px) {{
    /* Contain overflow — without this, font sizes don't matter because
       the browser zooms out to fit any over-wide element. */
    html, body {{ overflow-x: hidden !important; }}
    div[data-testid="stAppViewContainer"] {{ overflow-x: hidden !important; }}
    /* Trim Streamlit's ~6rem top spacer, but leave room for the fixed
       header bar (~64px; it overlays the page). Less than this and the
       title hides under the bar on Streamlit Cloud. */
    .main .block-container, div[data-testid="stMainBlockContainer"] {{
        max-width: 100% !important;
        padding: 4.5rem 0.75rem 2rem 0.75rem !important;
    }}

    html {{ font-size: 17px !important; }}

    /* Inputs need 16px+ on iOS to prevent auto-zoom-on-focus.
       No-op on Android but doesn't hurt. */
    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input,
    textarea {{ font-size: 17px !important; }}

    /* Headings: Streamlit's JS injects stylesheets AFTER our CSS loads, and
       those rules use data-testid selectors. To win the specificity race,
       we have to match their exact selector chain — a bare h1 rule loses.
       Confirmed by observing "flash of correct size, then shrink back" on
       Android Chrome — classic late-stylesheet symptom. */
    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stHeading"] h1,
    h1[data-testid="stHeading"] {{
        font-size: 1.6rem !important; line-height: 1.2 !important;
        margin-top: 0.25rem !important; margin-bottom: 0.25rem !important;
        font-weight: 700 !important;
    }}

    div[data-testid="stCaptionContainer"] p {{ font-size: 14px !important; }}
    label {{ font-size: 15px !important; }}
    div[data-testid="stAlert"] p {{ font-size: 16px !important; }}
}}
</style>
""")


def _secret(name: str) -> str:
    """Read a Streamlit secret, or "" if secrets aren't configured at all."""
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


# On Streamlit Cloud, set these in the app's Secrets panel. Locally, put them
# in .streamlit/secrets.toml (see secrets.toml.example).
API_KEY = _secret("GOOGLE_MAPS_API_KEY")
ANTHROPIC_KEY = _secret("ANTHROPIC_API_KEY")  # optional — enables the shortcut
APP_PASSWORD = _secret("APP_PASSWORD")        # optional — enables the lock screen

SHORTCUT_ENABLED = bool(ANTHROPIC_KEY) and _ANTHROPIC_AVAILABLE


# -------------------- Password gate --------------------

# A deployed Streamlit app is public to anyone with the URL, and every search
# spends the owner's Google (and Claude) quota. A shared password keeps the
# link safe to text to a travel companion.
#
# "Remember this phone" saves a hash of the password in the browser, in two
# places: a cookie (read server-side via st.context.cookies — instant, no
# flash of the lock screen) and localStorage (read through the JS component).
# The cookie alone wasn't enough on Streamlit Cloud, where it didn't come back
# on later visits; localStorage is the dependable copy. Changing APP_PASSWORD
# invalidates both.
AUTH_KEY = "tpf_auth"


def _auth_token(password: str) -> str:
    return hashlib.sha256(f"travel-places-finder:{password}".encode()).hexdigest()


def require_password():
    """Show a lock screen and st.stop() until the right password is entered."""
    if not APP_PASSWORD or st.session_state.get("authed"):
        return

    token = _auth_token(APP_PASSWORD)
    cookie = st.context.cookies.get(AUTH_KEY)
    if isinstance(cookie, str) and hmac.compare_digest(cookie, token):
        st.session_state["authed"] = True
        return

    st.title("🗺️ Travel Places Finder")

    # The localStorage answer arrives a moment after the page loads (the
    # component reruns the script when it replies: "" if nothing is saved).
    # Until then show "Opening…" rather than flashing the lock screen at
    # people who are already logged in. The button is the escape hatch in
    # case the component never answers (e.g. scripts blocked).
    if _JS_EVAL_AVAILABLE and not st.session_state.get("show_login"):
        saved = streamlit_js_eval(
            js_expressions=f"localStorage.getItem('{AUTH_KEY}') || ''",
            key="auth_read",
        )
        if saved is None:
            st.caption("⏳ Opening…")
            st.button("Enter password instead", type="tertiary",
                      on_click=lambda: st.session_state.update(show_login=True))
            st.stop()
        if isinstance(saved, str) and saved and hmac.compare_digest(saved, token):
            st.session_state["authed"] = True
            st.rerun()

    with st.form("login"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary", width="stretch")
    if submitted:
        if hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            st.session_state["authed"] = True
            st.session_state["save_login"] = True
            st.rerun()
        st.error("That password didn't work.")
    st.stop()


require_password()

# First run after login: remember this browser so it skips the lock screen
# next time (one year).
if st.session_state.pop("save_login", False) and _JS_EVAL_AVAILABLE:
    _token = _auth_token(APP_PASSWORD)
    streamlit_js_eval(
        js_expressions=(
            f"localStorage.setItem('{AUTH_KEY}', '{_token}');"
            f"document.cookie = '{AUTH_KEY}={_token}; max-age=31536000; path=/; SameSite=Lax'"
            " + (location.protocol === 'https:' ? '; Secure' : ''); 'saved'"
        ),
        key="auth_write",
    )

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
    "places.currentOpeningHours.openNow",
    "places.utcOffsetMinutes",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
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


@st.cache_data(ttl=3600, show_spinner=False)
def reverse_geocode(lat: float, lng: float) -> Optional[str]:
    """Turn the phone's GPS fix into a human-readable address."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lng}", "key": API_KEY}
    try:
        data = requests.get(url, params=params, timeout=10).json()
    except Exception:
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    return data["results"][0]["formatted_address"]


@st.cache_data(ttl=300, show_spinner=False)
def places_text_search(query: str, lat: float, lng: float, radius_miles: float) -> list:
    """
    Text search near a location using Places API (New).

    Text Search only accepts a *rectangle* for locationRestriction (circles
    are bias-only, which lets Google return places well outside the radius).
    So we restrict to the circle's bounding box here, and the caller trims
    the box's corners with an exact distance check.
    """
    dlat = radius_miles / 69.0
    dlng = radius_miles / (69.0 * max(cos(radians(lat)), 0.01))
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "locationRestriction": {
            "rectangle": {
                "low":  {"latitude": lat - dlat, "longitude": lng - dlng},
                "high": {"latitude": lat + dlat, "longitude": lng + dlng},
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

PRICE_LEVELS = {
    "PRICE_LEVEL_FREE":           "Free",
    "PRICE_LEVEL_INEXPENSIVE":    "$",
    "PRICE_LEVEL_MODERATE":       "$$",
    "PRICE_LEVEL_EXPENSIVE":      "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def todays_hours(place: dict) -> str:
    """
    Today's opening hours *in the place's own time zone*, e.g.
    "7:00 AM – 9:00 PM". Google's weekdayDescriptions list starts on Monday,
    which matches datetime.weekday().
    """
    desc = place.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
    offset = place.get("utcOffsetMinutes")
    if len(desc) != 7 or offset is None:
        return ""
    local_now = datetime.now(timezone.utc) + timedelta(minutes=offset)
    line = desc[local_now.weekday()]
    return line.split(": ", 1)[1] if ": " in line else line


def places_to_dataframe(places, center_lat=None, center_lng=None) -> pd.DataFrame:
    rows = []
    for p in places:
        loc = p.get("location", {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        distance = None
        if center_lat is not None and lat is not None:
            distance = round(haversine_miles(center_lat, center_lng, lat, lng), 2)
        rows.append({
            "id":            p.get("id", ""),
            "Name":          p.get("displayName", {}).get("text", "Unknown"),
            "Rating":        p.get("rating"),
            "Reviews":       p.get("userRatingCount"),
            "Price":         PRICE_LEVELS.get(p.get("priceLevel", ""), ""),
            "Distance (mi)": distance,
            "Address":       p.get("formattedAddress", ""),
            "Phone":         p.get("nationalPhoneNumber", ""),
            "Dial":          p.get("internationalPhoneNumber", ""),
            "Website":       p.get("websiteUri", ""),
            "Hours":         p.get("regularOpeningHours", {}).get("weekdayDescriptions", []),
            "Today":         todays_hours(p),
            "Open now":      p.get("currentOpeningHours", {}).get("openNow"),
            "lat":           lat,
            "lng":           lng,
        })
    return pd.DataFrame(rows)


def maps_url(row) -> str:
    """Google Maps listing for the place (opens the Maps app on phones)."""
    return (f"https://www.google.com/maps/search/?api=1"
            f"&query={quote(row['Name'] + ', ' + row['Address'])}"
            f"&query_place_id={quote(row['id'])}")


def directions_url(row) -> str:
    """Turn-by-turn directions from wherever the phone is now."""
    return (f"https://www.google.com/maps/dir/?api=1"
            f"&destination={quote(row['Name'] + ', ' + row['Address'])}"
            f"&destination_place_id={quote(row['id'])}")


def _is_open(value) -> Optional[bool]:
    """Normalise the 'Open now' cell — pandas turns missing values into NaN."""
    return value if isinstance(value, bool) else None


def filter_and_sort(df: pd.DataFrame, prefix: str, sort_options: dict) -> pd.DataFrame:
    """
    Render the filter row (open now, min rating, sort) and return the
    filtered, sorted view. Filtering happens on stored results, so changing
    a filter never triggers another API call.

    `sort_options` maps a label to (columns, ascending) for sort_values.
    """
    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
        open_only = st.toggle("Open now", key=f"{prefix}_open")
        min_rating = st.segmented_control(
            "Rating", ["Any", "4.0+", "4.5+"], default="Any",
            key=f"{prefix}_rating", label_visibility="collapsed",
        )
        sort_label = st.selectbox(
            "Sort", list(sort_options), key=f"{prefix}_sort",
            label_visibility="collapsed", width=170,
        )

    view = df
    if open_only:
        view = view[view["Open now"].map(_is_open) == True]  # noqa: E712
    if min_rating in ("4.0+", "4.5+"):
        view = view[view["Rating"].fillna(0) >= float(min_rating.rstrip("+"))]
    cols, ascending = sort_options[sort_label]
    view = view.sort_values(cols, ascending=ascending, na_position="last")
    return view.reset_index(drop=True)


def render_cards(df: pd.DataFrame, distance_fn):
    """
    Result list as tappable cards. One st.html call for the whole list keeps
    the page light on phones (vs. dozens of separate Streamlit widgets).

    `distance_fn(row)` returns the mode-specific distance text, e.g.
    "2.3 mi away" or "0.4 mi off route · mile 63".
    """
    esc = html.escape
    parts = []
    for i, row in df.iterrows():
        meta = []
        if pd.notna(row["Rating"]):
            reviews = f" <span class='tpf-muted'>({int(row['Reviews']):,})</span>" \
                if pd.notna(row["Reviews"]) else ""
            meta.append(f"⭐ {row['Rating']:.1f}{reviews}")
        if row["Price"]:
            meta.append(esc(row["Price"]))
        dist = distance_fn(row)
        if dist:
            meta.append(esc(dist))

        status = []
        is_open = _is_open(row["Open now"])
        if is_open is True:
            status.append("<span class='tpf-open'>Open now</span>")
        elif is_open is False:
            status.append("<span class='tpf-closed'>Closed now</span>")
        if row["Today"]:
            status.append(f"Today {esc(row['Today'])}")

        buttons = [f"<a class='tpf-btn primary' href='{esc(directions_url(row))}' "
                   f"target='_blank' rel='noopener'>Directions</a>"]
        if row["Dial"] or row["Phone"]:
            dial = "".join(c for c in (row["Dial"] or row["Phone"]) if c.isdigit() or c == "+")
            # target=_blank matters: Streamlit Cloud runs the app in a
            # sandboxed frame that blocks a plain tel: link, but lets
            # popups out — and the phone hands a tel: popup to the dialer.
            buttons.append(f"<a class='tpf-btn' href='tel:{esc(dial)}' "
                           f"target='_blank' rel='noopener'>Call</a>")
        if str(row["Website"]).startswith(("http://", "https://")):
            buttons.append(f"<a class='tpf-btn' href='{esc(row['Website'])}' "
                           f"target='_blank' rel='noopener'>Website</a>")

        hours = ""
        if row["Hours"]:
            hours = ("<details><summary>All hours</summary>"
                     + "".join(f"<div>{esc(h)}</div>" for h in row["Hours"])
                     + "</details>")

        parts.append(
            "<div class='tpf-card'>"
            "<div class='tpf-head'>"
            f"<span class='tpf-num'>{i + 1}</span>"
            f"<a class='tpf-name' href='{esc(maps_url(row))}' target='_blank' "
            f"rel='noopener'>{esc(row['Name'])}</a>"
            "</div>"
            + (f"<div class='tpf-meta'>{' · '.join(meta)}</div>" if meta else "")
            + (f"<div class='tpf-meta'>{' · '.join(status)}</div>" if status else "")
            + f"<div class='tpf-addr'>{esc(row['Address'])}</div>"
            + f"<div class='tpf-actions'>{''.join(buttons)}</div>"
            + hours
            + "</div>"
        )
    st.html("".join(parts))


def render_map(df, center_lat, center_lng, center_label="Search center",
               polyline_str=None, endpoint_markers=None):
    """Build a Folium map with center, numbered results, and optional route."""
    m = folium.Map(location=[center_lat, center_lng], zoom_start=11)
    esc = html.escape

    # Center marker (red star) — only show when it's a meaningful anchor
    if center_label and not endpoint_markers:
        folium.Marker(
            [center_lat, center_lng],
            popup=esc(center_label),
            icon=folium.Icon(color="red", icon="star", prefix="fa"),
        ).add_to(m)

    # Endpoint markers (green flag for origin, dark-red flag for destination)
    if endpoint_markers:
        for label, lat, lng, color in endpoint_markers:
            folium.Marker(
                [lat, lng],
                popup=esc(label),
                icon=folium.Icon(color=color, icon="flag", prefix="fa"),
            ).add_to(m)

    # Route polyline
    if polyline_str:
        coords = polyline_lib.decode(polyline_str)
        folium.PolyLine(coords, color=BRAND, weight=5, opacity=0.6).add_to(m)

    # Result markers, numbered to match the cards in the List tab.
    for i, row in df.iterrows():
        if pd.isna(row.get("lat")) or pd.isna(row.get("lng")):
            continue
        # Inline styles here are load-bearing: Folium renders in its own
        # iframe, so the page's CSS can't reach popups or pins.
        popup_html = (
            "<div style='font-size:14px;line-height:1.5;min-width:200px;"
            "font-family:-apple-system,BlinkMacSystemFont,sans-serif;'>"
            f"<b style='font-size:15px;'>{i + 1}. {esc(row['Name'])}</b><br>"
        )
        if pd.notna(row.get("Rating")):
            reviews = f" ({int(row['Reviews'])})" if pd.notna(row.get("Reviews")) else ""
            popup_html += f"⭐ {row['Rating']}{reviews}<br>"
        popup_html += f"{esc(row['Address'])}<br>"
        popup_html += (
            f"<a href='{esc(directions_url(row))}' target='_blank' "
            f"style='font-weight:600;color:{BRAND};'>Directions</a>"
            "</div>"
        )
        pin = (
            f"<div style='background:{BRAND};color:#fff;border:2px solid #fff;"
            "border-radius:50%;width:28px;height:28px;display:flex;"
            "align-items:center;justify-content:center;font:700 13px sans-serif;"
            f"box-shadow:0 1px 4px rgba(0,0,0,.4);'>{i + 1}</div>"
        )
        folium.Marker(
            [row["lat"], row["lng"]],
            popup=folium.Popup(popup_html, max_width=280),
            tooltip=esc(row["Name"]),
            icon=folium.DivIcon(html=pin, icon_size=(28, 28), icon_anchor=(14, 14)),
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


def announce_results(count: int):
    """
    Call right after a successful search. The results render below the form,
    which on a phone (or a short laptop window) is off-screen — so without a
    signal it looks like Search did nothing. A toast is always visible.
    """
    st.session_state["scroll_to_results"] = True
    st.session_state["search_n"] = st.session_state.get("search_n", 0) + 1
    st.toast(f"Found {count} place{'s' if count != 1 else ''} — showing results below", icon="✅")


def scroll_to_results():
    """
    After a fresh search, scroll the page down to the results.

    The search number goes into the HTML so every search produces new markup;
    identical markup would be reused by Streamlit without re-running the
    script, and later searches wouldn't scroll. The script retries for a
    couple of seconds because the results may still be drawing.
    """
    if st.session_state.pop("scroll_to_results", False):
        anchor = f"tpf-results-{st.session_state.get('search_n', 0)}"
        st.html(
            f"<div id='{anchor}'></div><script>(function () {{"
            "var tries = 0;"
            "(function go() {"
            f"  var el = document.getElementById('{anchor}');"
            "  if (el) { el.scrollIntoView({behavior: 'smooth', block: 'start'}); return; }"
            "  if (++tries < 15) setTimeout(go, 200);"
            "})();"
            "}());</script>",
            unsafe_allow_javascript=True,
        )


def show_results(df_view, total, map_fn, distance_fn, map_key):
    """List / Map view of a filtered result set."""
    if df_view.empty:
        st.info("No places match these filters.")
        return
    if len(df_view) < total:
        st.caption(f"Showing {len(df_view)} of {total} after filters")

    # A switch rather than st.tabs: Leaflet can't measure a map drawn inside
    # a hidden tab, so fit_bounds zooms all the way in. Drawing the map only
    # when it's visible fixes that, and keeps the List view light on phones.
    view = st.segmented_control(
        "View", ["📋 List", "🗺️ Map"], default="📋 List",
        key=f"{map_key}_view", label_visibility="collapsed",
    )
    if view == "🗺️ Map":
        st_folium(map_fn(df_view), height=440, use_container_width=True,
                  returned_objects=[], key=map_key)
    else:
        render_cards(df_view, distance_fn)


# -------------------- "Use my location" --------------------

# The browser's GPS fix arrives asynchronously: the button sets a flag, the
# JS component asks the browser, and Streamlit reruns once it answers. Each
# press gets a fresh component key so a second press asks again.
#
# The consume step must run *before* the target text_input is created in a
# script run — Streamlit forbids writing a widget's state after it exists.

def _request_location(target_key: str):
    st.session_state[f"geo_n_{target_key}"] = st.session_state.get(f"geo_n_{target_key}", 0) + 1
    st.session_state[f"geo_pending_{target_key}"] = True


def consume_location(target_key: str):
    if not st.session_state.get(f"geo_pending_{target_key}"):
        return
    n = st.session_state.get(f"geo_n_{target_key}", 0)
    pos = get_geolocation(component_key=f"geo_{target_key}_{n}")
    if pos is None:
        st.caption("📍 Getting your location… (allow location access if asked)")
        return
    st.session_state[f"geo_pending_{target_key}"] = False
    coords = pos.get("coords") if isinstance(pos, dict) else None
    if not coords:
        st.warning("Couldn't get your location. Check that location access "
                   "is allowed for this site in your browser settings.")
        return
    lat, lng = coords["latitude"], coords["longitude"]
    st.session_state[target_key] = reverse_geocode(lat, lng) or f"{lat:.5f},{lng:.5f}"
    # A spoken/typed "near me" request was waiting on this fix to search.
    if st.session_state.pop(f"search_after_geo_{target_key}", False):
        st.session_state["auto_search"] = True


def location_button(target_key: str):
    if _JS_EVAL_AVAILABLE:
        st.button("📍 Use my location", key=f"geo_btn_{target_key}", type="tertiary",
                  on_click=_request_location, args=(target_key,))


# -------------------- Voice input --------------------

# Speech-to-text runs in the browser (Chrome's built-in Web Speech API — the
# same service behind the phone keyboard's mic), so there's no extra API key
# or cost. It follows the same async pattern as "Use my location": the 🎤
# button sets a flag, the JS component listens and replies, Streamlit reruns.
# The transcript lands in the "Ask in plain English" box, and the existing
# shortcut flow parses it and searches.
SPEECH_JS = """
new Promise(function (resolve) {
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { resolve({error: 'unsupported'}); return; }
  var r = new SR(), done = false;
  r.lang = navigator.language || 'en-US';
  r.interimResults = false;
  r.maxAlternatives = 1;
  r.onresult = function (e) { done = true; resolve({text: e.results[0][0].transcript}); };
  r.onerror = function (e) { done = true; resolve({error: e.error}); };
  r.onend = function () { if (!done) resolve({error: 'no-speech'}); };
  try { r.start(); } catch (err) { resolve({error: String(err)}); }
})
"""

SPEECH_ERRORS = {
    "not-allowed": "The microphone is blocked. Allow it for this site in your browser settings.",
    "service-not-allowed": "The microphone is blocked. Allow it for this site in your browser settings.",
    "no-speech": "Didn't hear anything — tap 🎤 and try again.",
    "audio-capture": "No microphone found.",
    "network": "Voice needs an internet connection — try again in a moment.",
    "unsupported": "This browser can't do voice input. Use the 🎤 key on your keyboard instead.",
}


def _request_speech():
    st.session_state["speech_n"] = st.session_state.get("speech_n", 0) + 1
    st.session_state["speech_pending"] = True


def consume_speech():
    """
    Must run before the "Ask in plain English" text_input is created, since
    it writes that widget's value.
    """
    if not st.session_state.get("speech_pending"):
        return
    result = streamlit_js_eval(
        js_expressions=SPEECH_JS, key=f"speech_{st.session_state.get('speech_n', 0)}",
    )
    if result is None:
        st.info("🎙️ Listening… say what you're looking for.")
        return
    st.session_state["speech_pending"] = False
    text = result.get("text", "").strip() if isinstance(result, dict) else ""
    if text:
        st.session_state["shortcut_text"] = text
        # Saying the same thing twice should search again, not be skipped
        # as "already parsed".
        st.session_state["shortcut_last_parsed"] = ""
    else:
        err = result.get("error", "") if isinstance(result, dict) else ""
        st.warning(SPEECH_ERRORS.get(err, "Voice input didn't work — try again, or type instead."))


# -------------------- Natural-language parser --------------------

# Tool schema: Claude must return exactly these fields via tool_use. Using
# a tool call (rather than "please respond in JSON") is the hard contract —
# the API won't return malformed JSON, saving us from defensive parsing.
PARSE_TOOL = {
    "name": "parse_travel_request",
    "description": (
        "Parse a user's natural-language travel/search request into "
        "structured search parameters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["near_location", "along_route"],
                "description": (
                    "near_location: user wants places within a radius of a "
                    "single point (hotel, city, landmark). "
                    "along_route: user wants places along a drive between "
                    "two explicit points (A to B)."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "What the user is looking for, normalized to a clean "
                    "search term. e.g. 'vegan dinner' -> 'vegan restaurant', "
                    "'charging' -> 'EV charging station'."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "For near_location mode only. The anchor point: city, "
                    "address, hotel, landmark. Use the most specific form "
                    "the user gave."
                ),
            },
            "radius_miles": {
                "type": "integer",
                "description": (
                    "For near_location mode. Default 10 if unspecified. "
                    "Infer from words like 'nearby' (5), 'around' (10), "
                    "'within an hour' (50)."
                ),
            },
            "origin": {
                "type": "string",
                "description": "For along_route mode. The starting point.",
            },
            "destination": {
                "type": "string",
                "description": "For along_route mode. The ending point.",
            },
            "use_current_location": {
                "type": "boolean",
                "description": (
                    "True when the user means where they are right now "
                    "instead of naming a place: 'near me', 'around here', "
                    "'nearby' with no place given (near_location), or "
                    "'from here' / 'from where I am' (along_route origin). "
                    "Leave location / origin empty in that case."
                ),
            },
            "skip_first_miles": {
                "type": "integer",
                "description": (
                    "For along_route mode. How many miles of the start of "
                    "the route to skip. Default 25. Infer from phrases like "
                    "'once I'm out of the city' (25-40) or "
                    "'the whole way' (0)."
                ),
            },
        },
        "required": ["mode", "query"],
    },
}

PARSE_SYSTEM_PROMPT = (
    "You are a parser for a travel places finder. The user describes what "
    "they want in natural language; you translate it to structured search "
    "parameters by calling the parse_travel_request tool. Always call the "
    "tool — never respond with plain text. If the request is ambiguous, "
    "make a reasonable best guess rather than asking for clarification; "
    "the user will see your interpretation and can adjust before searching."
)


def parse_with_claude(user_text: str) -> Optional[dict]:
    """
    Ask Claude to parse a natural-language request into search parameters.
    Returns the tool input dict, or None on any failure (network, parse, etc.).

    Uses Claude Haiku at low effort — this is a small, structured parsing
    task. Costs a fraction of a cent per call.
    """
    if not SHORTCUT_ENABLED or not user_text.strip():
        return None
    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=1024,
            system=PARSE_SYSTEM_PROMPT,
            tools=[PARSE_TOOL],
            tool_choice={"type": "tool", "name": "parse_travel_request"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        st.error(f"Parse error: {e}")
        return None

    if response.stop_reason == "refusal":
        return None
    # Find the tool_use block in the response — with tool_choice forcing a
    # specific tool, there should be exactly one.
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    return None


# -------------------- UI --------------------

MODE_NEAR = "📍 Near a place"
MODE_ROUTE = "🛣️ Along a drive"

st.title("🗺️ Travel Places Finder")
st.caption("Find places where you're *going* — not just where you are.")

# Natural-language shortcut: one box that fills in the form below via
# Claude, then runs the search. Populates session_state so existing widgets
# pick up the values on the next rerun.
if SHORTCUT_ENABLED:
    voice = _JS_EVAL_AVAILABLE
    if voice:
        consume_speech()
    with st.container(horizontal=True, vertical_alignment="bottom"):
        shortcut_text = st.text_input(
            "💬 Ask in plain English" + (" — or tap 🎤 and say it" if voice else ""),
            placeholder="coffee near me",
            key="shortcut_text",
        )
        if voice:
            st.button("🎤", key="speak_btn", type="primary", help="Speak your search",
                      on_click=_request_speech)

    # Trigger parsing when the text changes (Streamlit reruns on Enter).
    # Guard with a session_state flag so we don't re-parse identical text
    # on unrelated reruns (e.g. slider changes elsewhere on the page).
    last_parsed = st.session_state.get("shortcut_last_parsed", "")
    if shortcut_text and shortcut_text != last_parsed:
        with st.spinner("Interpreting…"):
            parsed = parse_with_claude(shortcut_text)
        st.session_state["shortcut_last_parsed"] = shortcut_text

        if parsed:
            st.session_state["shortcut_parsed"] = parsed
            # Populate the appropriate mode's form fields. Widgets below
            # read from these session_state keys on render.
            if parsed.get("mode") == "near_location":
                st.session_state["mode"] = MODE_NEAR
                if parsed.get("location"):
                    st.session_state["near_location"] = parsed["location"]
                if parsed.get("query"):
                    st.session_state["near_query"] = parsed["query"]
                if parsed.get("radius_miles"):
                    st.session_state["near_radius"] = max(1, min(50, int(parsed["radius_miles"])))
                if parsed.get("use_current_location") and _JS_EVAL_AVAILABLE:
                    st.session_state["near_location"] = ""
                    _request_location("near_location")
            elif parsed.get("mode") == "along_route":
                st.session_state["mode"] = MODE_ROUTE
                if parsed.get("origin"):
                    st.session_state["route_origin"] = parsed["origin"]
                if parsed.get("destination"):
                    st.session_state["route_dest"] = parsed["destination"]
                if parsed.get("query"):
                    st.session_state["route_query"] = parsed["query"]
                if parsed.get("skip_first_miles") is not None:
                    skip = int(parsed["skip_first_miles"])
                    st.session_state["route_skip"] = max(0, min(50, round(skip / 5) * 5))
                if parsed.get("use_current_location") and _JS_EVAL_AVAILABLE:
                    st.session_state["route_origin"] = ""
                    _request_location("route_origin")
            # Search straight away — on a phone, every saved tap counts. The
            # form still shows what was understood, so it's easy to adjust.
            # "Near me" requests wait for the GPS fix, then search.
            geo_key = {"near_location": "near_location",
                       "along_route": "route_origin"}.get(parsed.get("mode"))
            if geo_key and st.session_state.get(f"geo_pending_{geo_key}"):
                st.session_state[f"search_after_geo_{geo_key}"] = True
            else:
                st.session_state["auto_search"] = True
            st.rerun()
        else:
            st.warning("Couldn't understand that — try the form below.")


# Mode switch. segmented_control can be clicked "off" (returns None), so a
# callback snaps it back to the last real choice.
def _keep_mode():
    if st.session_state.get("mode") is None:
        st.session_state["mode"] = st.session_state.get("mode_prev", MODE_NEAR)
    st.session_state["mode_prev"] = st.session_state["mode"]


if st.session_state.get("mode") is None:
    st.session_state["mode"] = st.session_state.get("mode_prev", MODE_NEAR)
mode = st.segmented_control(
    "Search mode", [MODE_NEAR, MODE_ROUTE], key="mode",
    on_change=_keep_mode, label_visibility="collapsed", width="stretch",
)
st.session_state["mode_prev"] = mode


# -------- Mode 1: Near a place --------
if mode == MODE_NEAR:
    consume_location("near_location")
    location = st.text_input(
        "Where",
        placeholder="Ossining, NY  —  or  —  Hilton Garden Inn Tarrytown",
        key="near_location",
    )
    location_button("near_location")
    query = st.text_input(
        "Looking for",
        placeholder="vegan restaurants, coffee, pharmacy…",
        key="near_query",
    )
    # Defaults live in session_state (not the widget's `value=`) because the
    # plain-English shortcut also writes these keys — passing both makes
    # Streamlit show a warning.
    st.session_state.setdefault("near_radius", 10)
    radius_miles = st.slider("Within (miles)", 1, 50, key="near_radius")

    clicked = st.button("🔍 Search", type="primary", key="near_search", width="stretch")
    # When Search is pressed, do the work and stash results in session_state
    # so they survive the reruns that Streamlit triggers on any UI interaction.
    if clicked or st.session_state.pop("auto_search", False):
        if not location or not query:
            st.warning("Please fill in both where and what.")
        else:
            with st.spinner(f"Looking up '{location}'…"):
                geo = geocode_address(location)
            if not geo:
                st.error(f"Couldn't find '{location}'. Try being more specific.")
                st.stop()

            with st.spinner(f"Searching for '{query}' within {radius_miles} miles…"):
                places = places_text_search(query, geo["lat"], geo["lng"], radius_miles)

            df = places_to_dataframe(places, geo["lat"], geo["lng"])
            if not df.empty:
                # Trim the corners of the search rectangle back to a circle.
                df = df[df["Distance (mi)"] <= radius_miles].reset_index(drop=True)

            if df.empty:
                st.warning("No results found. Try a wider radius or a different search.")
                st.session_state.pop("near_results", None)  # clear any stale results
            else:
                st.session_state["near_results"] = {
                    "geo": geo,
                    "df": df,
                    "query": query,
                    "radius": radius_miles,
                }
                announce_results(len(df))

    # Render from session_state (survives reruns from map interactions, tab
    # switches, etc.). Nothing renders until the first successful search.
    if "near_results" in st.session_state:
        res = st.session_state["near_results"]
        st.divider()
        scroll_to_results()
        st.markdown(
            f"**{len(res['df'])} places** for “{res['query']}” within "
            f"{res['radius']} mi of {res['geo']['formatted_address']}"
        )
        df_view = filter_and_sort(res["df"], "near", {
            "Closest":   (["Distance (mi)"], [True]),
            "Top rated": (["Rating", "Reviews"], [False, False]),
        })
        show_results(
            df_view, len(res["df"]),
            map_fn=lambda d: render_map(d, res["geo"]["lat"], res["geo"]["lng"],
                                        center_label=res["geo"]["formatted_address"]),
            distance_fn=lambda r: f"{r['Distance (mi)']:.1f} mi away"
                                  if pd.notna(r["Distance (mi)"]) else "",
            map_key="near_map",
        )


# -------- Mode 2: Along a drive --------
elif mode == MODE_ROUTE:
    consume_location("route_origin")
    origin = st.text_input("From", placeholder="Queens, NY", key="route_origin")
    location_button("route_origin")
    destination = st.text_input("To", placeholder="Croton-on-Hudson, NY", key="route_dest")
    query = st.text_input(
        "Looking for along the way",
        placeholder="Starbucks, gas, EV charging, rest stop…",
        key="route_query",
    )

    # Route mode is for discovery mid-trip and toward the destination. The
    # "Near a place" mode covers the origin area. Default 25 mi — tunable
    # because "25 mi out of Manhattan" ≠ "25 mi out of Poughkeepsie."
    st.session_state.setdefault("route_skip", 25)
    skip_miles = st.slider(
        "Skip the first … miles",
        min_value=0, max_value=50, step=5,
        key="route_skip",
        help=("Leave out places near your starting point — use "
              "'Near a place' for those. This mode is for what's "
              "ahead on your trip."),
    )

    clicked = st.button("🔍 Search", type="primary", key="route_search", width="stretch")
    if clicked or st.session_state.pop("auto_search", False):
        if not origin or not destination or not query:
            st.warning("Please fill in From, To, and what you're looking for.")
        else:
            with st.spinner("Finding your start and end points…"):
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

                # The haversine-from-center "Distance (mi)" is meaningless
                # here; snap each result onto the route instead. That gives
                # both "how far off the highway is this?" (the question a
                # driver has when considering a detour) and "how far into the
                # trip is it?" — measured along the road, so bends in the
                # route don't scramble the order.
                df = places_to_dataframe(places)
                cum = cumulative_miles(route_coords)
                located = [
                    locate_on_polyline(lat, lng, route_coords, cum)
                    if pd.notna(lat) else (None, None)
                    for lat, lng in zip(df["lat"], df["lng"])
                ]
                df["Off route (mi)"] = [off for off, _ in located]
                df["Mile"] = [along for _, along in located]

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
                # New result set → the detour slider's range changes; drop
                # its old value so it resets to the new default.
                st.session_state.pop("route_detour_limit", None)
                announce_results(len(df))

    # Render from session_state
    if "route_results" in st.session_state:
        res = st.session_state["route_results"]
        minutes = res["route"]["duration_sec"] // 60
        miles = res["route"]["distance_meters"] / 1609.34
        skipped = res.get("skipped_miles", 0)
        skip_requested = res.get("skip_requested", 0)
        drive = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"

        st.divider()
        scroll_to_results()
        if skipped > 0:
            skip_text = f" · skipped first {skipped:.0f} mi"
        elif skip_requested > 0:
            skip_text = " · trip too short to skip, searched the whole way"
        else:
            skip_text = ""
        st.markdown(f"🛣️ **{miles:.0f} mi** · about {drive} driving{skip_text}")

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
                format="%.1f",
                key="route_detour_limit",
                help=("Hide places farther off the route than this. "
                      "Straight-line distance to the road, not actual "
                      "driving detour."),
            )
            df_in_range = df_all[df_all["Off route (mi)"] <= detour_limit]
        else:
            df_in_range = df_all

        st.markdown(f"**{len(df_in_range)} of {len(df_all)} places** for “{res['query']}”")
        df_view = filter_and_sort(df_in_range, "route", {
            "Least detour": (["Off route (mi)"], [True]),
            "Trip order":   (["Mile"], [True]),
            "Top rated":    (["Rating", "Reviews"], [False, False]),
        })

        def route_distance(r):
            if pd.isna(r["Off route (mi)"]):
                return ""
            return f"{r['Off route (mi)']:.1f} mi off route · mile {r['Mile']:.0f}"

        show_results(
            df_view, len(df_in_range),
            map_fn=lambda d: render_map(
                d,
                res["mid_lat"], res["mid_lng"],
                center_label=None,
                polyline_str=res["route"]["polyline"],
                endpoint_markers=[
                    (f"Start: {res['origin_geo']['formatted_address']}",
                     res["origin_geo"]["lat"], res["origin_geo"]["lng"], "green"),
                    (f"End: {res['dest_geo']['formatted_address']}",
                     res["dest_geo"]["lat"], res["dest_geo"]["lng"], "darkred"),
                ],
            ),
            distance_fn=route_distance,
            map_key="route_map",
        )

# Travel Places Finder

A Streamlit app for finding places (restaurants, shops, hotels, etc.) at destinations you're planning to visit — not just where you are right now. Solves the real problem that Google Maps is great at "what's around me" but poor at "what's around where I'll be next Tuesday."

## What it does

Two search modes:

1. **Near a place** — Radius search around any address, hotel name, or landmark. Example: "vegan restaurants within 20 miles of Ossining, NY." A **Use my location** button fills in where you are now.
2. **Along a drive** — Finds places along the actual driving path between two points, using Google's `searchAlongRouteParameters`. Example: "Starbucks on the drive from Queens to Croton-on-Hudson." Each result shows how far off the road it is and roughly which mile of the trip it's at.

Results show as phone-friendly cards — rating, price, distance, open/closed now, and today's hours — with **Directions**, **Call**, and **Website** buttons that hand off to the phone's Maps and dialer apps. Switch to **Map** for numbered pins that match the cards. Filter by *open now* or minimum rating and change the sort without another search.

With an Anthropic key set, an **Ask in plain English** box (e.g. "vegan dinner near Hilton Garden Inn Tarrytown") fills in the form and runs the search for you.

An optional **password screen** keeps the deployed app private; each browser remembers the password for a year.

## Prerequisites

- Python 3.9+
- A Google Cloud account with billing enabled (the free tier is generous — personal-use volumes stay free)

## Setup

### 1. Get a Google Maps Platform API key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Enable billing on the project (required even for free tier).
4. Enable these three APIs under **APIs & Services → Library**:
   - **Geocoding API**
   - **Places API (New)** ← important: the "New" one, not legacy
   - **Routes API**
5. Go to **APIs & Services → Credentials → Create Credentials → API key**.
6. Copy the key. Optionally restrict it by HTTP referrer or IP for safety.

### 2. Install and configure locally

```bash
# Clone or download this folder, then:
cd travel-places-finder

# (Optional but recommended) create a virtual env
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create your secrets file
# Windows:
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# macOS/Linux:
cp .streamlit/secrets.toml.example .streamlit/secrets.toml

# Edit .streamlit/secrets.toml and paste in your API key
# (plus the optional Anthropic key and app password — see the comments)
```

### 3. Run it

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501`.

## Deploying to Streamlit Cloud

1. Push this folder to a GitHub repo (`.streamlit/secrets.toml` is already gitignored, so your key stays local).
2. Go to [share.streamlit.io](https://share.streamlit.io), connect your GitHub, and deploy the repo.
3. In the deployed app's **Settings → Secrets**, paste:
   ```toml
   GOOGLE_MAPS_API_KEY = "your-key-here"
   ANTHROPIC_API_KEY = "your-key-here"   # optional
   APP_PASSWORD = "pick-a-password"      # strongly recommended once deployed
   ```
   Without `APP_PASSWORD`, anyone with the link can run searches on your API keys.
4. Done. The app is now live at `https://your-app-name.streamlit.app`.

## Phone home-screen launcher

Streamlit Community Cloud wraps every app in its own page, so "Add to Home screen" on a phone shows Streamlit's name and icon. The `docs/` folder is a tiny GitHub Pages site — **https://jspellos.github.io/travel-places-finder/** — with its own name (JS-Travel), icon, and web manifest. It immediately redirects to the Streamlit app. Add *that* page to the home screen instead.

It redirects rather than showing the app in an `<iframe>`: Streamlit's "wake up" button for a sleeping app hangs inside a frame on another site, but works when the app is opened directly.

If the Streamlit app URL ever changes, update the two URLs in `docs/index.html`.

## File structure

```
travel-places-finder/
├── app.py                              # Main Streamlit app (UI + API calls)
├── geo.py                              # Pure geometry helpers (distance, route snapping)
├── requirements.txt                    # Python dependencies
├── README.md                           # This file
├── .gitignore                          # Protects secrets.toml
├── docs/                               # GitHub Pages home-screen launcher (name, icon, manifest)
└── .streamlit/
    ├── config.toml                     # Theme color, toolbar
    └── secrets.toml.example            # Template — copy to secrets.toml
```

## Architecture notes

- **Three Google APIs** are used: Geocoding (free — address → lat/lng), Places API (New) (paid after free tier — the place lookup), and Routes API (paid after free tier — driving polylines).
- **Results are cached** via `@st.cache_data` — 1 hour for geocoding/routes, 5 minutes for place searches. This both speeds up the app and saves API calls during iteration.
- **The Places API (New) requires a field mask** (`X-Goog-FieldMask` header). The mask in `app.py` asks for only the fields used in the UI. Adding fields costs more per call; removing fields saves money.
- **"Along a drive" positions** snap each result onto the route polyline (`geo.locate_on_polyline`), giving both the straight-line distance off the road and the mile marker along it — so "Trip order" stays correct even on routes with big bends.
- **"Near a place" stays inside the radius.** Text Search only accepts a rectangle as a hard restriction, so the app restricts to the circle's bounding box and then drops the corners with an exact distance check.
- **Password cookie.** After a correct password, the app stores a hash of it in a cookie (`tpf_auth`) and reads it back with `st.context.cookies`. Changing `APP_PASSWORD` logs every browser out.

## Cost watch

At standard Google Maps Platform pricing:
- Geocoding: $5 per 1,000 calls (10,000 free/month)
- Places Text Search: $32 per 1,000 calls (~$0.032 each — generous free tier)
- Routes: $5 per 1,000 calls

For personal travel-planning use, you'll almost certainly stay inside the free tier. Set a billing alert in Google Cloud if that makes you sleep better.

## Next steps (if this tool earns its keep)

- Extract Google API calls into a FastAPI backend; keep API key off the client entirely.
- Build a React PWA frontend against that backend for a real mobile experience.
- Add trip-level context: save itineraries, pre-search all destinations on a multi-city trip.

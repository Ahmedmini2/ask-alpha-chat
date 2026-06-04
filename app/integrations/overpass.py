"""Minimal OpenStreetMap Overpass API client for nearby points of interest.

Overpass is free and requires no key (the cacheable base layer in our hybrid POI
plan; Google Places can enrich later). Be gentle: it is rate-limited, so callers
should cache results (see app/ingest/poi.py / project_pois)."""
import asyncio
import logging
import httpx

log = logging.getLogger("askalpha.overpass")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Map OSM tags -> our POI categories. Each entry is (osm_key, regex_of_values, category).
_OSM_MAP = [
    ("amenity", "school|kindergarten|college|university", "school"),
    ("amenity", "hospital", "hospital"),
    ("amenity", "clinic|doctors", "clinic"),
    ("amenity", "pharmacy", "pharmacy"),
    ("shop", "mall|department_store", "mall"),
    ("shop", "supermarket", "supermarket"),
    ("railway", "station", "metro"),
    ("leisure", "park", "park"),
    ("natural", "beach", "beach"),
]

# Categories the tool exposes.
CATEGORIES = ["school", "hospital", "clinic", "pharmacy", "mall", "supermarket", "metro", "park", "beach"]


def _build_query(lat: float, lng: float, radius_m: int) -> str:
    # Group all values that share an OSM key into ONE nwr clause (node+way+relation).
    # 18 separate node/way clauses time out; ~5 grouped nwr clauses run in time.
    by_key: dict[str, list[str]] = {}
    for key, values, _cat in _OSM_MAP:
        by_key.setdefault(key, []).extend(values.split("|"))
    clauses = [
        f'nwr["{key}"~"^({"|".join(vals)})$"](around:{radius_m},{lat},{lng});'
        for key, vals in by_key.items()
    ]
    body = "\n".join(clauses)
    return f"[out:json][timeout:60];\n({body}\n);\nout center tags;"


def _categorize(tags: dict) -> str | None:
    for key, values, cat in _OSM_MAP:
        v = tags.get(key)
        if v and any(v == opt for opt in values.split("|")):
            return cat
    return None


async def fetch_pois(lat: float, lng: float, radius_m: int = 3000) -> list[dict]:
    """Return a list of {name, category, lat, lng, source_ref, raw} near a point."""
    query = _build_query(lat, lng, radius_m)
    # Overpass rejects requests without a descriptive User-Agent (returns 406).
    headers = {"User-Agent": "AskAlpha/1.0 (UAE real-estate assistant; +https://allegiance.ae)"}
    async with httpx.AsyncClient(timeout=40.0, headers=headers) as client:
        for attempt in range(3):
            try:
                resp = await client.post(OVERPASS_URL, data={"data": query})
                if resp.status_code == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except httpx.HTTPError as e:
                if attempt == 2:
                    log.warning("Overpass failed: %s", e)
                    return []
                await asyncio.sleep(1.5 * (attempt + 1))
        else:
            return []

    out: list[dict] = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {}) or {}
        cat = _categorize(tags)
        if not cat:
            continue
        # nodes have lat/lng; ways have a 'center'.
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plng = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None or plng is None:
            continue
        out.append({
            "name": tags.get("name"),
            "category": cat,
            "lat": float(plat),
            "lng": float(plng),
            "source_ref": f'{el.get("type")}/{el.get("id")}',
            "raw": tags,
        })
    return out

"""Build a combined EU NUTS2 marketing map GeoJSON (single-tileset, multi-layer).

Everything lives as attributes on one set of NUTS2 polygons so a MapTiler free
account (one tileset) can render several thematic layers from the same upload.

Data sources:
  - NUTS2 boundaries (NUTS 2024) from Eurostat GISCO, 1:3M
  - GDP per inhabitant in PPS, index EU27_2020=100 (nama_10r_2gdp, unit
    PPS_HAB_EU27_2020 — NOT PPS_EU27_2020_HAB, which is the absolute value)
  - Net disposable household income per inhabitant in PPS (nama_10r_2hhinc,
    unit PPS_EU27_2020_HAB, direction BAL, na_item B6N; the dataset has no
    index unit and no PPCS_HAB — the EU27=100 index is computed here against
    the EU27_2020 aggregate, fallback: unweighted mean of EU27 regions)
  - Private-ish swimming pools (leisure=swimming_pool, access != yes) from
    OpenStreetMap via Overpass, for POOL_COUNTRIES only; responses are cached
    under overpass_cache/ so reruns don't re-download
  - Population per NUTS2 (demo_r_d2jan, latest year) for pools_per_100k

Feature properties written:
  nuts_id, name, country,
  pps_index, pps_year, fill                      (GDP per capita, EU27=100)
  income, income_index, income_year, fill_income
  hotspot, fill_hotspot                          (both indices >= 110)
  pool_count, pools_per_100k, fill_pools         (null where country not queried)

Writes eu_market_map.geojson (03M geometry). If it exceeds 9 MB, also writes
eu_market_map_small.geojson from 10M geometry to stay under MapTiler's 10 MB
Vector Editor limit.

Dependencies beyond the stdlib: requests, shapely.
"""

import bisect
import json
import math
import os
import statistics
import sys
import time

import argparse

import requests
from shapely import STRtree, prepare
from shapely.geometry import Point, shape

OUT_MAIN = "eu_market_map.geojson"
OUT_SMALL = "eu_market_map_small.geojson"
SIZE_SOFT_LIMIT = 9 * 1024 * 1024   # simplify above this
SIZE_HARD_LIMIT = 10 * 1024 * 1024  # MapTiler Vector Editor limit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "overpass_cache")
# Eurostat and GISCO responses land here. Overpass keeps its own directory
# because those files are the expensive ones (hours of polite querying) and
# should survive a `rm -rf` of the cheap cache.
HTTP_CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
QUERY_FILE = os.path.join(SCRIPT_DIR, "overpass", "private_pools.overpassql")
# set from CLI flags in main(); see build_arg_parser()
REFRESH = set()

GISCO_TMPL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
    "NUTS_RG_{res}_2024_4326_LEVL_2.geojson"
)
API_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# overpass-api.de answers 406 to the default python-requests User-Agent;
# a descriptive UA with contact info is also what their usage policy asks for
OVERPASS_HEADERS = {
    "User-Agent": "eu-market-map-builder/1.0 (contact: ywdsham@gmail.com)"
}

# EU + EFTA, as NUTS country codes (Greece is EL in NUTS, GR in OSM's
# ISO3166-1 tags — see NUTS_TO_OSM_ISO). The UK is deliberately absent: it is
# not in the NUTS 2024 classification our boundaries use, and Eurostat has no
# current UK population data, so GB pools could never be joined to a region.
POOL_COUNTRIES = [
    "SI", "AT", "HR", "IT", "DE", "FR", "ES",           # cached from earlier runs
    "BE", "BG", "CY", "CZ", "DK", "EE", "EL", "FI", "HU", "IE", "LT", "LU",
    "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SK",     # rest of EU
    "NO", "CH", "IS", "LI",                             # EFTA
]
NUTS_TO_OSM_ISO = {"EL": "GR"}

# climate/wealth peer groups for the OSM-coverage plausibility check
CLIMATE_GROUPS = {
    "mediterranean": {"CY", "EL", "ES", "FR", "HR", "IT", "MT", "PT"},
    "continental": {"AT", "BE", "BG", "CH", "CZ", "DE", "HU", "LI", "LU",
                    "NL", "PL", "RO", "SI", "SK"},
    "northern": {"DK", "EE", "FI", "IE", "IS", "LT", "LV", "NO", "SE"},
}

OVERPASS_DELAY = 12          # polite pause between country requests, seconds
OVERPASS_RETRY_STATUS = {429, 502, 503, 504}
OVERPASS_ATTEMPTS = 5

EU27 = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK",
}

# class breaks / palette shared by fill and fill_income (ColorBrewer YlGnBu)
FILL_BREAKS = [50, 75, 100, 125, 150]
FILL_COLORS = ["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#2c7fb8", "#253494"]
HOTSPOT_THRESHOLD = 110
HOTSPOT_COLOR = "#d7263d"
POOLS_LOW = (255, 255, 255)   # white
POOLS_HIGH = (8, 48, 107)     # deep blue #08306b


def fill_color(index_value):
    """Map an EU27=100 index value to the choropleth class color."""
    if index_value is None:
        return None
    return FILL_COLORS[bisect.bisect_right(FILL_BREAKS, index_value)]


def pools_color(per100k, scale_max):
    """White -> deep blue gradient; sqrt-eased, clipped at scale_max (p95)."""
    if per100k is None:
        return None
    t = min(per100k / scale_max, 1.0) if scale_max > 0 else 0.0
    t = math.sqrt(t)
    rgb = tuple(round(lo + (hi - lo) * t) for lo, hi in zip(POOLS_LOW, POOLS_HIGH))
    return "#%02x%02x%02x" % rgb


# --------------------------------------------------------------------------
# Eurostat + GISCO downloads
# --------------------------------------------------------------------------

def cached_json(kind, name, fetch):
    """Return parsed JSON for `name`, fetching via `fetch()` on a cache miss.

    `kind` is one of the --refresh keywords, so `--refresh eurostat` re-downloads
    every Eurostat dataset without touching the GISCO boundaries. Caching these
    matters less than caching Overpass, but re-running the build while tuning
    colour breaks should not re-hit a public statistical API a dozen times.
    """
    os.makedirs(HTTP_CACHE_DIR, exist_ok=True)
    path = os.path.join(HTTP_CACHE_DIR, f"{name}.json")
    if os.path.exists(path) and kind not in REFRESH:
        print(f"  using cached {name}.json "
              f"({os.path.getsize(path) / 1024:.0f} kB; "
              f"--refresh {kind} to re-download)")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    data = fetch()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(f"  cached {name}.json ({os.path.getsize(path) / 1024:.0f} kB)")
    return data


def fetch_boundaries(res):
    print(f"Downloading NUTS2 boundaries ({res}) from GISCO ...")

    def download():
        r = requests.get(GISCO_TMPL.format(res=res), timeout=120)
        r.raise_for_status()
        return r.json()

    gj = cached_json("gisco", f"nuts2_{res}", download)
    print(f"  {len(gj['features'])} NUTS2 features")
    return gj


def fetch_series(dataset, params, label):
    """Fetch a JSON-stat 2.0 dataset, return {geo_code: {year: float}}.

    All dimensions other than geo and time must be pinned to one category
    by the query params; parsing is done generically via dimension strides.
    """
    print(f"Downloading {label} ({dataset}) from the Eurostat API ...")

    def download():
        r = requests.get(API_BASE + dataset,
                         params={"format": "JSON", "lang": "EN", **params},
                         timeout=120)
        r.raise_for_status()
        return r.json()

    # the params are part of the cache key: changing --since-year or a unit
    # filter must not silently reuse a response fetched for different filters
    key = "_".join(f"{k}-{v}" for k, v in sorted(params.items()))
    data = cached_json("eurostat", f"eurostat_{dataset}_{key}", download)

    dim_ids = data["id"]
    sizes = data["size"]

    strides = {}
    acc = 1
    for dim_id, size in zip(reversed(dim_ids), reversed(sizes)):
        strides[dim_id] = acc
        acc *= size

    def category_index(dim_id):
        cat = data["dimension"][dim_id]["category"]
        idx = cat.get("index")
        if idx is None:
            return {code: 0 for code in cat["label"]}
        if isinstance(idx, list):
            return {code: i for i, code in enumerate(idx)}
        return dict(idx)

    for dim_id, size in zip(dim_ids, sizes):
        if dim_id not in ("geo", "time") and size != 1:
            raise RuntimeError(
                f"{dataset}: dimension {dim_id!r} has {size} categories; "
                "expected 1. Adjust the query filters."
            )

    geo_idx = category_index("geo")
    time_idx = category_index("time")
    values = data["value"]

    series = {}
    for geo, gpos in geo_idx.items():
        for year, tpos in time_idx.items():
            v = values.get(str(gpos * strides["geo"] + tpos * strides["time"]))
            if v is not None:
                series.setdefault(geo, {})[year] = float(v)
    return series


def latest(by_year, max_year=None):
    """Newest value at or before `max_year`, or None if the region has none.

    Eurostat coverage is ragged: a region can report GDP for 2024 and income
    only to 2021, and some report neither. Callers drop the Nones and the
    resulting gaps are counted in report() rather than being papered over.
    """
    years = [y for y in by_year if max_year is None or int(y) <= max_year]
    if not years:
        return None
    year = max(years)
    return by_year[year], year


def fetch_pps(since=2020, max_year=None):
    """{nuts2_code: (pps_index, year)}, latest available year per region."""
    series = fetch_series(
        "nama_10r_2gdp",
        {"unit": "PPS_HAB_EU27_2020", "sinceTimePeriod": str(since)},
        "GDP-per-capita PPS index",
    )
    result = {g: latest(y, max_year) for g, y in series.items() if len(g) == 4}
    result = {g: v for g, v in result.items() if v is not None}
    print(f"  PPS index values for {len(result)} NUTS2 regions "
          f"(years {min(y for _, y in result.values())}"
          f"-{max(y for _, y in result.values())})")
    return result


def fetch_income(since=2020, max_year=None):
    """{nuts2_code: (income_pps, income_index, year)}, latest year per region."""
    series = fetch_series(
        "nama_10r_2hhinc",
        {"unit": "PPS_EU27_2020_HAB", "direct": "BAL", "na_item": "B6N",
         "sinceTimePeriod": str(since)},
        "net disposable household income per inhabitant",
    )

    eu_avg = series.get("EU27_2020", {})
    if eu_avg:
        print(f"  EU27 average from the EU27_2020 aggregate: "
              f"{ {y: v for y, v in sorted(eu_avg.items())} }")
    else:
        print("  EU27_2020 aggregate not in response -> computing unweighted "
              "mean of EU27 regions per year")
        per_year = {}
        for geo, by_year in series.items():
            if len(geo) == 4 and geo[:2] in EU27:
                for year, v in by_year.items():
                    per_year.setdefault(year, []).append(v)
        eu_avg = {y: statistics.fmean(vs) for y, vs in per_year.items()}

    result = {}
    for geo, by_year in series.items():
        if len(geo) != 4:
            continue
        newest = latest(by_year, max_year)
        if newest is None:
            continue
        value, year = newest
        base_years = [y for y in eu_avg if y <= year] or list(eu_avg)
        base = eu_avg[max(base_years)] if base_years else None
        index = round(value / base * 100, 1) if base else None
        result[geo] = (value, index, year)
    print(f"  income values for {len(result)} NUTS2 regions "
          f"(years {min(y for *_, y in result.values())}"
          f"-{max(y for *_, y in result.values())})")
    return result


def fetch_population(since=2022, max_year=None):
    """{nuts2_code: population}, latest available year per region."""
    series = fetch_series(
        "demo_r_d2jan",
        {"unit": "NR", "sex": "T", "age": "TOTAL",
         "sinceTimePeriod": str(since)},
        "population on 1 January",
    )
    newest = ((g, latest(y, max_year))
              for g, y in series.items() if len(g) == 4)
    result = {g: v[0] for g, v in newest if v is not None}
    print(f"  population for {len(result)} NUTS2 regions")
    return result


# --------------------------------------------------------------------------
# Overpass: swimming pools
# --------------------------------------------------------------------------

def overpass_query(cc):
    """The query from overpass/private_pools.overpassql, with the country filled in.

    It lives in its own file rather than inline here so that a reader can see
    exactly what was counted, and paste it straight into overpass-turbo.eu to
    check it themselves. The tag choices are argued in the comments there.
    """
    try:
        with open(QUERY_FILE, encoding="utf-8") as fh:
            template = fh.read()
    except FileNotFoundError:
        raise SystemExit(
            f"Overpass query template missing: {QUERY_FILE}\n"
            "It ships with the repo; restore it from git before rebuilding."
        )
    if "{{country}}" not in template:
        raise SystemExit(
            f"{QUERY_FILE} has no {{{{country}}}} placeholder — refusing to "
            "send a query that would silently target the wrong country."
        )
    return template.replace("{{country}}", cc)


def fetch_pools_country(cc):
    """Return [(lon, lat), ...] for one country, using the on-disk cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    iso = NUTS_TO_OSM_ISO.get(cc, cc)
    cache_file = os.path.join(CACHE_DIR, f"swimming_pools_{iso}.csv")

    if os.path.exists(cache_file):
        print(f"  {cc}: using cached response "
              f"({os.path.getsize(cache_file) / 1024:.0f} kB)")
        with open(cache_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = None
        for attempt in range(1, OVERPASS_ATTEMPTS + 1):
            print(f"  {cc}: querying Overpass (attempt {attempt}"
                  f"/{OVERPASS_ATTEMPTS}) ...", flush=True)
            try:
                r = requests.post(OVERPASS_URL,
                                  data={"data": overpass_query(iso)},
                                  headers=OVERPASS_HEADERS,
                                  timeout=900)
            except requests.RequestException as e:
                print(f"  {cc}: request failed ({e}); retrying ...")
                time.sleep(45 * attempt)
                continue
            if r.status_code in OVERPASS_RETRY_STATUS:
                print(f"  {cc}: HTTP {r.status_code} (rate limit/overload); "
                      f"waiting {45 * attempt}s ...")
                time.sleep(45 * attempt)
                continue
            r.raise_for_status()
            text = r.text
            break
        if text is None:
            raise RuntimeError(
                f"Overpass query for {cc} failed after {OVERPASS_ATTEMPTS} attempts")
        with open(cache_file, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"  {cc}: cached {len(text) / 1024:.0f} kB")

    points = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] and parts[1]:
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            points.append((lon, lat))
    return points


def fetch_pools(countries=None):
    """({country: [(lon, lat), ...]}, [failed_countries]) for `countries`.

    A country that keeps failing is skipped and reported, not fatal — its
    regions end up with null pool attributes, same as never-queried countries.
    """
    countries = POOL_COUNTRIES if countries is None else countries
    print("Downloading swimming pools (leisure=swimming_pool, access!=yes) "
          "from Overpass ...")
    print(f"  {len(countries)} countries; GB intentionally excluded "
          "(UK is not in NUTS 2024, no region to join pools to)")
    pools = {}
    failed = []
    for i, cc in enumerate(countries):
        iso = NUTS_TO_OSM_ISO.get(cc, cc)
        cache_file = os.path.join(CACHE_DIR, f"swimming_pools_{iso}.csv")
        if i > 0 and not os.path.exists(cache_file):
            time.sleep(OVERPASS_DELAY)  # be polite between live requests
        try:
            pools[cc] = fetch_pools_country(cc)
        except (RuntimeError, requests.RequestException) as e:
            print(f"  {cc}: FAILED, skipping ({e})")
            failed.append(cc)
            continue
        print(f"  {cc}: {len(pools[cc])} pools  "
              f"[{i + 1}/{len(countries)}]", flush=True)
    if failed:
        print(f"  failed countries (regions stay null): {', '.join(failed)}")
    return pools, failed


def count_pools(boundaries, pools, population):
    """Point-in-polygon count per NUTS2 region in the queried countries.

    Returns {nuts_id: (pool_count, pools_per_100k)}.
    """
    print("Counting pools per NUTS2 region (shapely point-in-polygon) ...")
    regions = []  # (nuts_id, country, geometry)
    for f in boundaries["features"]:
        cc = f["properties"]["CNTR_CODE"]
        if cc in pools:
            geom = shape(f["geometry"])
            prepare(geom)
            regions.append((f["properties"]["NUTS_ID"], cc, geom))

    counts = {nuts_id: 0 for nuts_id, _, _ in regions}
    all_points = [Point(lon, lat)
                  for pts in pools.values() for lon, lat in pts]
    tree = STRtree(all_points)
    for nuts_id, _, geom in regions:
        counts[nuts_id] = len(tree.query(geom, predicate="intersects"))

    matched = sum(counts.values())
    print(f"  {matched} of {len(all_points)} pool points fell inside a "
          f"queried-country NUTS2 region")

    result = {}
    for nuts_id, cc, _ in regions:
        pop = population.get(nuts_id)
        per100k = round(counts[nuts_id] / pop * 100_000, 1) if pop else None
        result[nuts_id] = (counts[nuts_id], per100k)
    return result


# --------------------------------------------------------------------------
# Join + output
# --------------------------------------------------------------------------

def join(boundaries, pps, income, pool_stats, pools_scale_max, pool_medians):
    features = []
    for f in boundaries["features"]:
        p = f["properties"]
        code = p["NUTS_ID"]
        cc = p["CNTR_CODE"]
        gdp = pps.get(code)
        inc = income.get(code)
        pool = pool_stats.get(code)
        pps_index = gdp[0] if gdp else None
        income_index = inc[1] if inc else None
        # pool criterion is relative to the region's own country (median of its
        # NUTS2 regions) so OSM mapping completeness differences between
        # countries don't decide who counts as a hotspot
        pool_ok = (pool is not None and pool[1] is not None
                   and cc in pool_medians
                   and pool[1] >= pool_medians[cc])
        hotspot = (pps_index is not None and income_index is not None
                   and pps_index >= HOTSPOT_THRESHOLD
                   and income_index >= HOTSPOT_THRESHOLD
                   and pool_ok)
        features.append({
            "type": "Feature",
            "properties": {
                "nuts_id": code,
                "name": p.get("NAME_LATN") or p.get("NUTS_NAME"),
                "country": p["CNTR_CODE"],
                "pps_index": pps_index,
                "pps_year": gdp[1] if gdp else None,
                "fill": fill_color(pps_index),
                "income": inc[0] if inc else None,
                "income_index": income_index,
                "income_year": inc[2] if inc else None,
                "fill_income": fill_color(income_index),
                "hotspot": hotspot,
                "fill_hotspot": HOTSPOT_COLOR if hotspot else None,
                "pool_count": pool[0] if pool else None,
                "pools_per_100k": pool[1] if pool else None,
                "fill_pools": pools_color(pool[1] if pool else None,
                                          pools_scale_max),
            },
            "geometry": f["geometry"],
        })
    return {"type": "FeatureCollection", "features": features}


def round_coords(obj, ndigits):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [round_coords(x, ndigits) for x in obj]
    return obj


def write_geojson(fc, path, ndigits=5):
    for f in fc["features"]:
        f["geometry"]["coordinates"] = round_coords(f["geometry"]["coordinates"], ndigits)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path)
    print(f"Wrote {path}  ({size / 1024 / 1024:.2f} MB)")
    return size


def validate_geometry(fc):
    bad = []
    for f in fc["features"]:
        g = f["geometry"]
        code = f["properties"]["nuts_id"]
        if g is None or g["type"] not in ("Polygon", "MultiPolygon"):
            bad.append((code, "not a (Multi)Polygon"))
            continue
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            for ring in poly:
                if len(ring) < 4:
                    bad.append((code, "ring with < 4 points"))
                elif ring[0] != ring[-1]:
                    bad.append((code, "unclosed ring"))
                elif any(
                    not (-180 <= x <= 180 and -90 <= y <= 90)
                    or not (math.isfinite(x) and math.isfinite(y))
                    for x, y in ring
                ):
                    bad.append((code, "coordinates out of range"))
    return bad


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_missing(features, key):
    missing = [f for f in features if f["properties"][key] is None]
    by_country = {}
    for f in missing:
        by_country.setdefault(f["properties"]["country"], []).append(
            f["properties"]["nuts_id"]
        )
    for c in sorted(by_country):
        print(f"  {c}: {', '.join(sorted(by_country[c]))}")


def fmt_region(f, value_key, year_key=None):
    p = f["properties"]
    year = f" , {p[year_key]}" if year_key else ""
    return (f"  {p[value_key]:8.1f}  {p['nuts_id']}  {p['name']} "
            f"({p['country']}{year})")


def report(fc, pools, population, failed_countries):
    feats = fc["features"]
    with_pps = [f for f in feats if f["properties"]["pps_index"] is not None]
    with_inc = [f for f in feats if f["properties"]["income_index"] is not None]

    print(f"\n=== Coverage ===")
    print(f"Regions with pps_index:      {len(with_pps)}  "
          f"(missing: {len(feats) - len(with_pps)})")
    print_missing(feats, "pps_index")
    print(f"Regions with income_index:   {len(with_inc)}  "
          f"(missing: {len(feats) - len(with_inc)})")
    print_missing(feats, "income_index")

    bad = validate_geometry(fc)
    print(f"\n=== Geometry validation ===")
    if bad:
        for code, why in bad:
            print(f"  INVALID {code}: {why}")
    else:
        print("All geometries are valid (Multi)Polygons.")

    both = [f for f in feats
            if f["properties"]["pps_index"] is not None
            and f["properties"]["income_index"] is not None]
    for f in both:
        p = f["properties"]
        p["_gap"] = p["pps_index"] - p["income_index"]
    by_gap = sorted(both, key=lambda f: f["properties"]["_gap"], reverse=True)

    def fmt_gap(f):
        p = f["properties"]
        return (f"  {p['_gap']:+6.0f}  {p['nuts_id']}  {p['name']} ({p['country']})"
                f"  pps {p['pps_index']:.0f} vs income {p['income_index']:.0f}")

    print(f"\n=== Divergence: GDP far above income "
          f"(multinational-HQ / production stories) ===")
    for f in by_gap[:10]:
        print(fmt_gap(f))
    print(f"\n=== Divergence: income far above GDP (commuter-belt stories) ===")
    for f in by_gap[-10:][::-1]:
        print(fmt_gap(f))
    for f in both:
        del f["properties"]["_gap"]

    hotspots = [f for f in feats if f["properties"]["hotspot"]]
    print(f"\n=== Hotspots (pps_index >= {HOTSPOT_THRESHOLD}, "
          f"income_index >= {HOTSPOT_THRESHOLD}, pools_per_100k >= country "
          f"median; queried countries only): {len(hotspots)} regions ===")
    for f in sorted(hotspots, key=lambda f: f["properties"]["nuts_id"]):
        p = f["properties"]
        print(f"  {p['nuts_id']}  {p['name']} ({p['country']})  "
              f"pps {p['pps_index']:.0f} / income {p['income_index']:.0f} / "
              f"pools {p['pools_per_100k']:.0f}/100k")

    with_pools = [f for f in feats
                  if f["properties"]["pools_per_100k"] is not None]
    print(f"\n=== Top 10 regions by pools_per_100k ===")
    for f in sorted(with_pools,
                    key=lambda f: f["properties"]["pools_per_100k"],
                    reverse=True)[:10]:
        p = f["properties"]
        print(f"  {p['pools_per_100k']:8.1f}  {p['nuts_id']}  {p['name']} "
              f"({p['country']}, {p['pool_count']} pools)")

    print(f"\n=== OSM pool coverage by country ===")
    country_pop = {}
    for f in feats:
        p = f["properties"]
        if p["country"] in pools and population.get(p["nuts_id"]):
            country_pop[p["country"]] = (country_pop.get(p["country"], 0)
                                         + population[p["nuts_id"]])
    # pools per MILLION inhabitants, flagged against climate/wealth peers
    rates = {}
    for cc in pools:
        pop = country_pop.get(cc)
        rates[cc] = len(pools[cc]) / pop * 1_000_000 if pop else None
    group_of = {cc: g for g, ccs in CLIMATE_GROUPS.items() for cc in ccs}
    group_median = {}
    for g, ccs in CLIMATE_GROUPS.items():
        vals = [rates[cc] for cc in ccs if rates.get(cc) is not None]
        group_median[g] = statistics.median(vals) if vals else None
    flagged = []
    for cc in sorted(pools):
        n = len(pools[cc])
        rate = rates[cc]
        g = group_of.get(cc, "?")
        if rate is None:
            print(f"  {cc}: {n:>8} pools, no population data")
            continue
        med = group_median.get(g)
        flag = ""
        if med and rate < 0.5 * med:
            flag = (f"  <-- implausibly low vs {g} peers "
                    f"(median {med:.0f}/M): OSM coverage gap, not few pools")
            flagged.append(cc)
        print(f"  {cc}: {n:>8} pools, {rate:8.0f} per million [{g}]{flag}")
    if failed_countries:
        print(f"  not included (Overpass failed): {', '.join(failed_countries)}")
    print("  (Rates also reflect cadastre imports (FR/ES are bulk-imported), "
          "so treat cross-country gaps as mapping completeness first, "
          "lifestyle second.)")
    if flagged:
        print(f"  flagged as under-mapped: {', '.join(flagged)}")


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="build_pps_map.py",
        description="Build the combined EU NUTS2 marketing-map GeoJSON.",
        epilog=(
            "Everything is cached on disk: Eurostat and GISCO under cache/, "
            "Overpass under overpass_cache/. A second run with no flags does "
            "no network I/O at all, which is the point: colour breaks get "
            "tuned far more often than the data changes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-o", "--out", default=OUT_MAIN, metavar="PATH",
                   help=f"output GeoJSON path (default: {OUT_MAIN})")
    p.add_argument("--resolution", default="03M", choices=["01M", "03M", "10M", "20M"],
                   help="GISCO boundary resolution (default: 03M). Coarser "
                        "resolutions shrink the file; 03M is 1.8 MB, well "
                        "under MapTiler's 10 MB limit.")
    p.add_argument("--year", type=int, metavar="YYYY",
                   help="use no data newer than this year, for reproducing an "
                        "older build. Default: latest available per region, "
                        "which differs between regions and between datasets.")
    p.add_argument("--since-year", type=int, default=2020, metavar="YYYY",
                   help="earliest year to request from Eurostat (default: 2020). "
                        "Only affects how far back the fallback reaches for "
                        "regions with no recent reporting.")
    p.add_argument("--datasets", default="gdp,income,pools", metavar="LIST",
                   help="comma-separated layers to build: gdp, income, pools. "
                        "Default: all three. Dropping 'pools' skips the "
                        "Overpass stage entirely, which is the slow one - use "
                        "it when you only want to re-colour the economic "
                        "layers. Hotspots need all three and are skipped "
                        "unless all three are present.")
    p.add_argument("--pool-countries", metavar="LIST",
                   help="comma-separated NUTS country codes to query for pools, "
                        f"instead of all {len(POOL_COUNTRIES)}. Useful for a "
                        "fast test run: --pool-countries AT,SI")
    p.add_argument("--refresh", default="", metavar="LIST",
                   help="comma-separated caches to bypass: eurostat, gisco, all. "
                        "Overpass is deliberately not refreshable this way - "
                        "delete the per-country file in overpass_cache/ by hand, "
                        "so a full 31-country re-download is always a "
                        "deliberate act.")
    return p


def main(argv=None):
    global REFRESH
    args = build_arg_parser().parse_args(argv)

    refresh = {k.strip() for k in args.refresh.split(",") if k.strip()}
    unknown = refresh - {"eurostat", "gisco", "all"}
    if unknown:
        raise SystemExit(f"--refresh: unknown cache(s) {', '.join(sorted(unknown))}")
    REFRESH = {"eurostat", "gisco"} if "all" in refresh else refresh

    datasets = {d.strip() for d in args.datasets.split(",") if d.strip()}
    unknown = datasets - {"gdp", "income", "pools"}
    if unknown:
        raise SystemExit(f"--datasets: unknown layer(s) {', '.join(sorted(unknown))}")
    if not datasets:
        raise SystemExit("--datasets: nothing to build")

    pool_countries = POOL_COUNTRIES
    if args.pool_countries:
        pool_countries = [c.strip().upper()
                          for c in args.pool_countries.split(",") if c.strip()]
        unknown = set(pool_countries) - set(POOL_COUNTRIES)
        if unknown:
            raise SystemExit(
                f"--pool-countries: {', '.join(sorted(unknown))} not in "
                f"POOL_COUNTRIES. Add them there first: a country needs a "
                f"CLIMATE_GROUPS entry too, or the coverage check will not "
                f"cover it.")

    if args.year:
        print(f"Year cap: using no data newer than {args.year}\n")

    pps = fetch_pps(args.since_year, args.year) if "gdp" in datasets else {}
    income = fetch_income(args.since_year, args.year) if "income" in datasets else {}
    if "pools" in datasets:
        population = fetch_population(max_year=args.year)
        pools, failed_countries = fetch_pools(pool_countries)
    else:
        print("Skipping the pool layer (--datasets without 'pools')")
        population, pools, failed_countries = {}, {}, []
    boundaries = fetch_boundaries(args.resolution)

    pool_stats = count_pools(boundaries, pools, population)
    per100k_vals = sorted(v for _, v in pool_stats.values() if v is not None)
    scale_max = (per100k_vals[int(len(per100k_vals) * 0.95)]
                 if per100k_vals else 1.0)
    print(f"  fill_pools gradient scaled to p95 = {scale_max:.1f} pools/100k")

    by_country = {}
    for f in boundaries["features"]:
        code = f["properties"]["NUTS_ID"]
        if code in pool_stats and pool_stats[code][1] is not None:
            by_country.setdefault(f["properties"]["CNTR_CODE"], []).append(
                pool_stats[code][1])
    pool_medians = {cc: statistics.median(v) for cc, v in by_country.items()}
    if pool_medians:
        print("  country medians for the hotspot pool criterion: "
              + ", ".join(f"{cc} {m:.0f}"
                          for cc, m in sorted(pool_medians.items())))

    fc = join(boundaries, pps, income, pool_stats, scale_max, pool_medians)
    report(fc, pools, population, failed_countries)
    print()
    size = write_geojson(fc, args.out)

    if size > SIZE_SOFT_LIMIT:
        base, ext = os.path.splitext(args.out)
        small_path = f"{base}_small{ext}"
        print(f"\n{args.out} exceeds 9 MB -> building simplified version from "
              f"10M-resolution geometry ...")
        small = join(fetch_boundaries("10M"), pps, income, pool_stats,
                     scale_max, pool_medians)
        ssize = write_geojson(small, small_path, ndigits=4)
        if ssize > SIZE_HARD_LIMIT:
            print("WARNING: even the 10M version exceeds 10 MB.", file=sys.stderr)


if __name__ == "__main__":
    main()

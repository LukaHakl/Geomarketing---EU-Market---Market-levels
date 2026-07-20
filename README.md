# Geomarketing---EU-Market---Market-levels
EU marketing map: purchasing power, disposable income, hotspots and pool density per NUTS2 region, built from public data for MapTiler
# EU Market Map (NUTS2) — one GeoJSON, four thematic layers

Combined marketing map for 299 NUTS2 regions. Everything lives as attributes on
one set of polygons, so a **MapTiler free account (one tileset)** can render
four layers from a single upload:

1. **GDP per capita in PPS**, index EU27 = 100 (Eurostat `nama_10r_2gdp`)
2. **Net disposable household income per inhabitant in PPS**, EU27 = 100 index
   computed against the EU27_2020 aggregate (Eurostat `nama_10r_2hhinc`,
   direction `BAL`, na_item `B6N`)
3. **Hotspots** — both indices ≥ 110 **and** pool density at or above the
   country median (29 regions)
4. **Private pool density** — OpenStreetMap `leisure=swimming_pool` with
   `access != yes`, counted per region, per 100k inhabitants (population from
   Eurostat `demo_r_d2jan`). Queried: **all EU27 + EFTA (31 countries)**.
   The UK is excluded — it is no longer in the NUTS classification our
   boundaries and Eurostat data use, so its pools have no region to join to.

## Files

| File | What it is |
|---|---|
| `build_pps_map.py` | Build script. Re-runnable end to end; Overpass responses are cached under `overpass_cache/` so reruns don't re-download (delete a country's file there to force a refresh). Dependencies: `requests`, `shapely`. |
| `eu_market_map.geojson` | **The file to upload.** 1.84 MB, 299 features — far under MapTiler's 10 MB Vector Editor limit, no simplified version needed. |
| `eu_purchasing_power.geojson` | Previous two-indicator build, superseded by `eu_market_map.geojson`. Safe to delete. |

## Feature properties

| Property | Meaning |
|---|---|
| `nuts_id`, `name`, `country` | NUTS2 code, region name, 2-letter country code |
| `pps_index`, `pps_year` | GDP per capita in PPS, EU27 = 100, and its reference year (mostly 2024; `null` for CH, IS, LI, BA, XK, NO0B) |
| `fill` | Pre-baked hex color for `pps_index` |
| `income`, `income_index`, `income_year` | Income in PPS, its EU27 = 100 index, reference year (mostly 2023 — the dataset lags GDP; `null` also for TR, RS, AL, ME, MK) |
| `fill_income` | Pre-baked hex color for `income_index`, same palette as `fill` |
| `hotspot`, `fill_hotspot` | `true` + `"#d7263d"` where `pps_index >= 110` **and** `income_index >= 110` **and** `pools_per_100k` ≥ the median of the region's own country (the country-relative pool criterion neutralizes OSM mapping-completeness gaps); `false` + `null` otherwise. Only possible in the 7 pool-queried countries. |
| `pool_count` | OSM pools inside the region (`null` where the country wasn't queried or its Overpass download failed) |
| `pools_per_100k` | `pool_count` per 100k inhabitants |
| `fill_pools` | Pre-baked white→deep-blue gradient color for `pools_per_100k` (sqrt-eased, clipped at the p95 of the distribution, ≈ 1431 pools/100k so France's cadastre-import outliers don't wash everyone else out); `null` where not queried/failed |

Notes and caveats:

- `income_index` for regions whose latest income year is 2024 is computed
  against the latest EU27 aggregate (2023) — flatters them by ~1 year of
  nominal growth.
- Eurostat has no `PPCS_HAB` unit in `nama_10r_2hhinc`; the per-inhabitant
  PPS unit there is `PPS_EU27_2020_HAB`, and the index is computed by the
  script. In `nama_10r_2gdp` the index unit is `PPS_HAB_EU27_2020` — the
  near-identical `PPS_EU27_2020_HAB` is the absolute value. Both traps are
  handled.
## Pool coverage caveats — read before trusting the blue layer

- **Grey (`null`) means not queried or download failed**, never "no pools":
  the UK (not in NUTS 2024) and the non-EU/EFTA Balkans and Turkey.
- **Pool data is OSM mapping completeness as much as reality.** FR (826k
  pools) and ES (411k) include bulk cadastre imports; Austria (8354/million)
  is also densely mapped. The build flags countries whose per-capita rate is
  under half their climate/wealth peer group's median.
- **Flagged as under-mapped in the current build — low values there mean
  incomplete mapping, not few pools:**
  - **HR** 1440/million vs Mediterranean median 3905
  - **IT** 1948/million vs Mediterranean median 3905
  - **IE** 50/million vs northern median 147
  - **LV** 68/million vs northern median 147
  - **PL** 89/million vs continental median 825
  - **RO** 135/million vs continental median 825
- Germany (779/million) and the Netherlands (600) pass the peer test but are
  still far below Austria/Switzerland; treat any cross-border color jump with
  suspicion. Compare regions *within* a country, not across countries — which
  is also why the hotspot pool criterion is country-relative.

## Data snapshot (2026-07 build)

- 285/299 regions with GDP data, 250/299 with income, 1,671,420 pool points
  matched to regions across the 31 queried countries (all downloads succeeded).
- Hotspots: 29 regions — the Austrian/south-German belt (Styria, Upper
  Austria, Salzburg, Stuttgart, Karlsruhe, Tübingen, Ober-/Niederbayern,
  Oberpfalz, Unterfranken, Darmstadt, Braunschweig), Flanders + Brabant
  wallon, Madrid, northern Italy + Lazio, Luxembourg, Malta, Noord-Holland,
  Noord-Brabant, Helsinki, Budapest, Bucharest, Oslo, Stockholm. Dense
  capitals Vienna and Paris still fail the country-relative pool criterion.
- Biggest GDP-over-income gaps: Dublin +171, Southern Ireland +118,
  Luxembourg +95, Brussels +86, Prague +79.
- Biggest income-over-GDP gaps: Burgenland −47, Prov. Luxembourg (BE) −40,
  Lüneburg −40, Niederösterreich −37, Trier −32.
- Top pool density: Languedoc-Roussillon 5519/100k, Provence-Alpes-Côte d'Azur
  2998, Midi-Pyrénées 2414, Poitou-Charentes 2323, Illes Balears 2294; the
  Ionian Islands (1745) and the Algarve (1719) now crack the top 10.

## The single-tileset multi-layer trick

MapTiler free allows one tileset — but one tileset can feed any number of
*style layers*. All four thematic fills are pre-baked as hex colors in the
attributes, so each layer is just `["get", "<fill property>"]`:

1. Upload **`eu_market_map.geojson`** once: cloud.maptiler.com → **Tiles** →
   **New tileset** → **Upload file**. Note the source layer name (usually
   `eu_market_map`).
2. Open a map in **Customize** (or start from an empty basemap) and **add the
   tileset as a polygon layer four times** (duplicate the layer three times).
3. Set each copy's fill color to a data-driven expression:
   - layer `gdp`: `["get", "fill"]`
   - layer `income`: `["get", "fill_income"]`
   - layer `pools`: `["get", "fill_pools"]`
   - layer `hotspots`: `["get", "fill_hotspot"]` — **keep this one on top**;
     non-hotspot regions have `null` and render transparent
4. Toggle layer visibility to switch stories; the hotspot overlay works on top
   of any of them.

`null` fills render transparent, so put a neutral grey layer underneath if you
want an explicit "no data" look (see recipes below).

## Styling recipes (style JSON)

Shared palette for `fill` / `fill_income` — breaks **50 / 75 / 100 / 125 / 150**
(EU average = 100), ColorBrewer YlGnBu:

| Class | `#ffffcc` < 50 | `#c7e9b4` 50–75 | `#7fcdbb` 75–100 | `#41b6c4` 100–125 | `#2c7fb8` 125–150 | `#253494` ≥ 150 |
|---|---|---|---|---|---|---|

Simplest form — one layer per baked fill (recommended with the trick above):

```json
{
  "id": "gdp",
  "type": "fill",
  "source": "your-tileset",
  "source-layer": "eu_market_map",
  "paint": {
    "fill-color": ["coalesce", ["get", "fill"], "#d9d9d9"],
    "fill-opacity": 0.8,
    "fill-outline-color": "#ffffff"
  }
}
```

Duplicate and swap the property: `fill_income`, `fill_pools` (both with the
same `coalesce` → grey for no data). For the hotspot overlay, drop the
`coalesce` so non-hotspots stay transparent:

```json
{
  "id": "hotspots",
  "type": "fill",
  "source": "your-tileset",
  "source-layer": "eu_market_map",
  "paint": { "fill-color": ["get", "fill_hotspot"], "fill-opacity": 0.55 }
}
```

If you prefer breaks you can tweak in the style instead of the baked colors,
use a `step` expression on the raw values, e.g. for GDP:

```json
"fill-color": [
  "case",
  ["==", ["get", "pps_index"], null], "#d9d9d9",
  ["step", ["get", "pps_index"],
    "#ffffcc", 50, "#c7e9b4", 75, "#7fcdbb",
    100, "#41b6c4", 125, "#2c7fb8", 150, "#253494"]
]
```

…and for pools an `interpolate` on `pools_per_100k` (clip ~1430, the baked
gradient's p95):

```json
"fill-color": [
  "case",
  ["==", ["get", "pools_per_100k"], null], "#d9d9d9",
  ["interpolate", ["linear"], ["sqrt", ["get", "pools_per_100k"]],
    0, "#ffffff", 37.8, "#08306b"]
]
```

For tooltips: `name`, `country`, both indices and years, `income`,
`pool_count`, and `pools_per_100k` are all in the tile properties.

## Rebuilding with fresh data

```
python build_pps_map.py
```

Eurostat data always uses the latest available year per region (since 2020).
Overpass responses are cached in `overpass_cache/` — delete the per-country
`.csv` files to re-download (the script waits politely between live requests
and retries on 429/504; overpass-api.de also rejects the default
python-requests User-Agent with HTTP 406, so the script sends a descriptive
one).

"""Render the four thematic views of eu_market_map.geojson as static SVGs.

Why this exists: the map's real home is MapTiler Cloud, but most people who
open the repo will never click through to a hosted map. The README needs the
pictures inline, and they need to be reproducible rather than screenshots that
silently go stale the next time the data is rebuilt.

Why SVG and not PNG: it keeps the dependency list at zero. Rendering a
choropleth to PNG means matplotlib or cairo; rendering it to SVG means writing
path strings, which is a hundred lines of stdlib. GitHub renders SVG in
markdown image tags, so the README works either way. If you need PNGs (for a
slide, or for a platform that strips SVG), see the note at the bottom of this
docstring.

What it draws: each view reads the colour that build_pps_map.py already baked
into the feature properties, so these previews cannot drift from what MapTiler
shows -- they are literally the same hex values, projected and written out.

Projection: Web Mercator, the same projection MapTiler renders in, so the
shapes match what you see in the browser.

Extent: the previews are clipped to the European mainland (lon -25..45,
lat 34..72). The outermost regions -- the French DOM, the Azores, Madeira, the
Canaries -- are in the data and on the real map, but including them zooms the
frame out until Europe is a smudge. They are dropped from the picture only.

Usage:
    python render_previews.py                      # writes previews/*.svg
    python render_previews.py --in some.geojson --out-dir docs/img

To get PNGs, convert afterwards with whatever is already on the machine, e.g.
    inkscape --export-type=png --export-width=1800 previews/*.svg
    rsvg-convert -w 1800 previews/gdp.svg -o previews/gdp.png
or open the SVG in a browser and screenshot it. Deliberately not automated
here -- it would mean a dependency for a once-per-rebuild chore.
"""

import argparse
import json
import math
import os

# clip box for the drawn frame (see module docstring)
LON_MIN, LON_MAX = -25.0, 45.0
LAT_MIN, LAT_MAX = 34.0, 72.0

WIDTH = 900          # px; height follows from the projected aspect ratio
MARGIN = 8
NO_DATA = "#e8e8e8"  # regions the view has no value for
STROKE = "#ffffff"
BG = "#f7f9fb"

# the palette build_pps_map.py bakes into `fill` and `fill_income`
INDEX_BREAKS = [50, 75, 100, 125, 150]
INDEX_COLORS = ["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#2c7fb8", "#253494"]

VIEWS = [
    {
        "name": "gdp",
        "prop": "fill",
        "title": "GDP per capita in PPS",
        "subtitle": "EU27 = 100. Eurostat nama_10r_2gdp, latest year per region.",
        "legend": "index",
    },
    {
        "name": "income",
        "prop": "fill_income",
        "title": "Net disposable household income per capita in PPS",
        "subtitle": "EU27 = 100. Eurostat nama_10r_2hhinc. The same palette as "
                    "the GDP view, so the two are directly comparable.",
        "legend": "index",
    },
    {
        "name": "pools",
        "prop": "fill_pools",
        "title": "Private pool density",
        "subtitle": "Pools per 100,000 inhabitants. OpenStreetMap via Overpass. "
                    "Compare within a country, not across borders -- see the "
                    "mapping-completeness caveat in the README.",
        "legend": "pools",
    },
    {
        "name": "hotspots",
        "prop": "fill_hotspot",
        "title": "Hotspots",
        "subtitle": "GDP index >= 110 AND income index >= 110 AND pool density "
                    "at or above the region's own country median.",
        "legend": "hotspot",
    },
]


def mercator(lon, lat):
    """Web Mercator, in unscaled units. Latitude clamped away from the poles."""
    lat = max(min(lat, 85.0), -85.0)
    return lon, math.degrees(math.log(math.tan(math.radians(45 + lat / 2.0))))


def rings(geom):
    """Yield every linear ring of a Polygon or MultiPolygon as a coord list."""
    if geom is None:
        return
    if geom["type"] == "Polygon":
        yield from geom["coordinates"]
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield from poly


def in_frame(geom):
    """True if any vertex falls inside the clip box.

    Cheaper and more forgiving than real clipping: a region that straddles the
    edge is drawn whole and the SVG viewBox crops it. Regions entirely outside
    (the DOM, the Azores) contribute no vertices and are dropped.
    """
    for ring in rings(geom):
        for lon, lat in ring:
            if LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX:
                return True
    return False


def make_projector():
    """Return (project, width, height) mapping lon/lat to SVG pixel space."""
    x0, y0 = mercator(LON_MIN, LAT_MIN)
    x1, y1 = mercator(LON_MAX, LAT_MAX)
    span_x, span_y = x1 - x0, y1 - y0
    inner = WIDTH - 2 * MARGIN
    scale = inner / span_x
    height = span_y * scale + 2 * MARGIN

    def project(lon, lat):
        x, y = mercator(lon, lat)
        # SVG y grows downward, Mercator y grows northward -- hence the flip
        return (MARGIN + (x - x0) * scale,
                MARGIN + (y1 - y) * scale)

    return project, WIDTH, height


def path_d(geom, project, grid):
    """SVG path data for one feature, all rings concatenated.

    Simplification happens in pixel space rather than in degrees: snap every
    vertex to a whole pixel, then drop consecutive duplicates. At this frame
    size one pixel is roughly 8 km, so the 1:3M source geometry carries far
    more detail than the image can show -- this cuts the file by ~90% with no
    visible change. Doing it in pixel space also means the tolerance is
    automatically correct at every latitude, which a degree-based tolerance
    would not be across a frame spanning Crete to Tromso.
    """
    parts = []
    kept_any = False
    for ring in rings(geom):
        pts = []
        last = None
        for lon, lat in ring:
            x, y = project(lon, lat)
            pt = (round(x / grid) * grid, round(y / grid) * grid)
            if pt != last:
                pts.append(pt)
                last = pt
        if len(pts) < 3:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # drop specks, but never the last ring of a feature: a region that
        # collapses entirely would silently disappear from the map, which is
        # exactly the failure mode the README warns about for missing data
        if max(xs) - min(xs) < grid and max(ys) - min(ys) < grid and kept_any:
            continue
        kept_any = True
        parts.append("M" + " L".join(f"{fmt(x)} {fmt(y)}" for x, y in pts) + "Z")
    return "".join(parts)


def fmt(v):
    """Trim '12.0' to '12' -- across ~200k coordinates this is worth ~8%."""
    return f"{v:.1f}".rstrip("0").rstrip(".")


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def legend_index(x, y):
    """Six-class swatch strip for the GDP and income views."""
    out = []
    sw, sh = 46, 12
    labels = ["<50", "50-75", "75-100", "100-125", "125-150", ">=150"]
    for i, (color, label) in enumerate(zip(INDEX_COLORS, labels)):
        cx = x + i * sw
        out.append(f'<rect x="{cx}" y="{y}" width="{sw}" height="{sh}" '
                   f'fill="{color}" stroke="#ffffff" stroke-width="0.5"/>')
        out.append(f'<text x="{cx + sw / 2}" y="{y + sh + 11}" '
                   f'class="lg" text-anchor="middle">{label}</text>')
    out.append(f'<rect x="{x + 6 * sw + 14}" y="{y}" width="{sw}" '
               f'height="{sh}" fill="{NO_DATA}" stroke="#ffffff" '
               f'stroke-width="0.5"/>')
    out.append(f'<text x="{x + 6 * sw + 14 + sw / 2}" y="{y + sh + 11}" '
               f'class="lg" text-anchor="middle">no data</text>')
    return "\n".join(out)


def legend_pools(x, y):
    """Continuous white-to-blue bar, matching the sqrt-eased baked ramp."""
    sw, sh = 276, 12
    out = [
        '<defs><linearGradient id="poolramp" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#ffffff"/>'
        '<stop offset="1" stop-color="#08306b"/></linearGradient></defs>',
        f'<rect x="{x}" y="{y}" width="{sw}" height="{sh}" '
        f'fill="url(#poolramp)" stroke="#cccccc" stroke-width="0.5"/>',
        f'<text x="{x}" y="{y + sh + 11}" class="lg">0</text>',
        f'<text x="{x + sw}" y="{y + sh + 11}" class="lg" '
        f'text-anchor="end">p95 and above</text>',
        f'<rect x="{x + sw + 20}" y="{y}" width="46" height="{sh}" '
        f'fill="{NO_DATA}" stroke="#ffffff" stroke-width="0.5"/>',
        f'<text x="{x + sw + 43}" y="{y + sh + 11}" class="lg" '
        f'text-anchor="middle">not queried</text>',
    ]
    return "\n".join(out)


def legend_hotspot(x, y):
    sw, sh = 46, 12
    out = [
        f'<rect x="{x}" y="{y}" width="{sw}" height="{sh}" fill="#d7263d" '
        f'stroke="#ffffff" stroke-width="0.5"/>',
        f'<text x="{x + sw + 8}" y="{y + sh - 2}" class="lg">hotspot</text>',
        f'<rect x="{x + 130}" y="{y}" width="{sw}" height="{sh}" '
        f'fill="{NO_DATA}" stroke="#ffffff" stroke-width="0.5"/>',
        f'<text x="{x + 130 + sw + 8}" y="{y + sh - 2}" class="lg">'
        f'everywhere else</text>',
    ]
    return "\n".join(out)


LEGENDS = {"index": legend_index, "pools": legend_pools,
           "hotspot": legend_hotspot}


def render(features, view, project, width, height, grid):
    header = 58   # room for title + subtitle above the map
    footer = 40   # room for the legend below it
    total = height + header + footer

    body = []
    drawn = valued = 0
    undrawable = []
    for f in features:
        d = path_d(f["geometry"], project, grid)
        if not d:
            # a region too small to survive the simplification grid. Named
            # rather than silently skipped -- a region missing from a map is
            # indistinguishable from a region with no data unless you say so.
            undrawable.append(f["properties"].get("nuts_id", "?"))
            continue
        color = f["properties"].get(view["prop"]) or NO_DATA
        if f["properties"].get(view["prop"]):
            valued += 1
        drawn += 1
        body.append(f'<path d="{d}" fill="{color}"/>')

    subtitle = view["subtitle"]
    # crude two-line wrap; subtitles are short and hand-written, so a word
    # count split is enough and avoids pulling in a text-measuring dependency
    words = subtitle.split()
    half = len(words) // 2
    for i in range(half, len(words)):
        if len(" ".join(words[:i])) >= 78:
            half = i
            break
    else:
        half = len(words)
    line1, line2 = " ".join(words[:half]), " ".join(words[half:])

    legend = LEGENDS[view["legend"]](MARGIN, height + header + 8)

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" \
height="{total:.0f}" viewBox="0 0 {WIDTH} {total:.0f}" \
font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">
<style>
  .ti {{ font-size: 15px; font-weight: 600; fill: #1a1a1a; }}
  .su {{ font-size: 10.5px; fill: #5a5a5a; }}
  .lg {{ font-size: 9.5px; fill: #5a5a5a; }}
  path {{ stroke: {STROKE}; stroke-width: 0.35; stroke-linejoin: round; }}
</style>
<rect width="{WIDTH}" height="{total:.0f}" fill="{BG}"/>
<text x="{MARGIN}" y="19" class="ti">{esc(view["title"])}</text>
<text x="{MARGIN}" y="34" class="su">{esc(line1)}</text>
<text x="{MARGIN}" y="46" class="su">{esc(line2)}</text>
<g transform="translate(0 {header})">
{chr(10).join(body)}
</g>
{legend}
</svg>
'''
    return svg, drawn, valued, undrawable


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Render the four map views as static SVGs for the README.")
    p.add_argument("--in", dest="src", default="eu_market_map.geojson",
                   help="input GeoJSON (default: eu_market_map.geojson)")
    p.add_argument("--out-dir", default="previews",
                   help="output directory (default: previews/)")
    p.add_argument("--simplify", type=float, default=2.0, metavar="PX",
                   help="snap vertices to a grid this many pixels wide "
                        "(default: 2.0). Lower is sharper and bigger; 1.0 "
                        "roughly doubles the file size for detail no one "
                        "will see at this scale.")
    args = p.parse_args(argv)

    with open(args.src, encoding="utf-8") as fh:
        fc = json.load(fh)
    features = [f for f in fc["features"] if in_frame(f["geometry"])]
    dropped = len(fc["features"]) - len(features)
    print(f"{args.src}: {len(fc['features'])} features, "
          f"{len(features)} inside the preview frame "
          f"({dropped} outermost regions dropped from the picture only)")

    os.makedirs(args.out_dir, exist_ok=True)
    project, width, height = make_projector()

    for view in VIEWS:
        svg, drawn, valued, undrawable = render(features, view, project,
                                                width, height, args.simplify)
        path = os.path.join(args.out_dir, f"{view['name']}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"  {path}: {drawn} regions drawn, {valued} with a value, "
              f"{drawn - valued} greyed  ({os.path.getsize(path) / 1024:.0f} kB)")
        if undrawable:
            print(f"    WARNING: too small to draw at --simplify "
                  f"{args.simplify}: {', '.join(undrawable)}")


if __name__ == "__main__":
    main()

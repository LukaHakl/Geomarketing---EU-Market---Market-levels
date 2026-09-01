// Private-ish swimming pools, one country at a time.
//
// This is the exact query the build script sends to https://overpass-api.de.
// {{country}} is substituted with an ISO 3166-1 alpha-2 country code before
// sending (see NUTS_TO_OSM_ISO in build_pps_map.py — Greece is EL in NUTS but
// GR in OSM, and that is the only code that differs).
//
// Run it interactively: paste into https://overpass-turbo.eu/ and replace
// {{country}} with e.g. AT. Zoom to the country first; overpass-turbo will
// refuse to draw ~800k points for France, so try a small country.
//
// ---------------------------------------------------------------------------
// What is actually being counted, and why
// ---------------------------------------------------------------------------
//
// leisure=swimming_pool     Every mapped pool basin in OSM. This tag covers
//                           the water body itself, not the facility around it
//                           (that is leisure=water_park / amenity=public_bath).
//
// access != yes             The filter that turns "all pools" into "probably
//                           somebody's garden". OSM convention is that a pool
//                           open to the public carries access=yes; municipal
//                           lidos, hotel pools open to non-guests and public
//                           baths are tagged that way. Everything else — the
//                           overwhelming majority, usually untagged for access
//                           entirely — is residential.
//
//                           Note this is a negative filter, so it KEEPS
//                           untagged pools. That is deliberate: an untagged
//                           pool traced from aerial imagery in a suburb is a
//                           private pool, and requiring access=private would
//                           throw away almost the whole dataset.
//
// nwr                       Nodes, ways and relations. Most pools are ways
//                           (a traced rectangle); a few are nodes, and pools
//                           with an island or an irregular cutout are
//                           relations. Querying only ways loses a few percent.
//
// out center                Reduces every way/relation to a single centre
//                           point. We only ever ask which NUTS2 region a pool
//                           falls in, so the full polygon is wasted bandwidth.
//
// out:csv(::lat, ::lon; false)
//                           Two columns, no header row. ~25 bytes per pool
//                           instead of ~250 for JSON — across 1.67M pools that
//                           is the difference between a 40 MB cache and a
//                           400 MB one.
//
// area[...][admin_level=2]  The country boundary relation, converted to an
//                           Overpass area. Querying per country rather than
//                           per NUTS2 region means 31 requests instead of 299,
//                           and country boundaries are far more reliably
//                           present as areas than subnational ones.
//
// [timeout:600]             France and Spain genuinely take minutes. The
//                           default 180s times out on both.
// ---------------------------------------------------------------------------

[out:csv(::lat, ::lon; false)][timeout:600];
area["ISO3166-1"="{{country}}"][admin_level=2]->.a;
nwr["leisure"="swimming_pool"]["access"!="yes"](area.a);
out center;

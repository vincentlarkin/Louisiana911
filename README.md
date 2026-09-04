# Louisiana911 Public Incident Monitor

[Louisiana911.com](https://louisiana911.com/) presents public incident information from participating Louisiana sources in one map and list.

Louisiana911 is not an emergency service. Call **911** for emergencies and follow instructions from local authorities.

## About

The site provides:

- Latest published incidents
- Date-based history
- Map and list views
- Source, severity, and agency filters
- Monthly summaries
- Mobile-friendly incident details

Locations are shown only when a usable public location can be validated. Map markers identify reported locations, not guaranteed pinpoints or exact boundaries.

## Coverage

Current sources include:

- [Caddo Parish 911 Communications District](https://ias.ecc.caddo911.com/All_ActiveEvents.aspx)
- [City of Baton Rouge traffic incidents](https://city.brla.gov/traffic/incidents.asp)
- [Lafayette Parish traffic feed](https://lafayette911.org/wp-json/traffic-feed/v1/data)
- [City of New Orleans / NOPD Calls for Service](https://data.nola.gov/resource/es9j-6y5d.json)
- [Lake Charles Police-to-Citizen closed calls](https://lakecharles.policetocitizen.com/cadcalls) — LCPD traffic, crime-related and service calls; 24-hour publication delay.

New Orleans information is published as a delayed daily record rather than a live feed. Source agencies may update, reclassify, delay, or remove records. Some locations are approximate, generalized, or unavailable.

## Map Guide

- **Red** — higher-severity incidents
- **Yellow** — medium-severity incidents
- **Blue** — lower-severity incidents
- **Green** — public-service incidents
- **Gray** — custody or prisoner incidents
- **Striped red and white** — medical or EMS incidents
- **Marker size** — larger markers indicate less precise placement; dashed outlines highlight approximate locations

Single transparent triangles are the default. Settings includes a saved **Circle markers** option. Marker shape, dark/light appearance, map/list view, and the History map setting use localStorage on the same browser and site. Live incidents with 5 or more units pulse with an outline matching their shape; reduced-motion preferences keep the outline static.

Colors are a visual organization aid, not an official emergency classification.

## Important Limits

Louisiana911 reflects what each public source publishes. It is not a complete record of emergency activity, dispatch operations, response status, or final outcomes. Times, descriptions, units, and locations can change at the source.

## Project Layout

- `sources/` — source adapters and normalization
- `public/` — site interface and coverage pages
- `tests/` — behavior and source checks
- `wiki/` — brief project notes

## Wiki

- [Behavior](wiki/Behavior.md)
- [Data sources](wiki/Data-Sources.md)
- [Analytics](wiki/Analytics.md)
- [Database behavior and validation](wiki/Database.md)
- [Public-data access protections](wiki/Security.md)

## License

See [LICENSE](LICENSE).

## Checks

Run `python -m unittest discover -s tests` for backend checks and `node --test tests/frontend.test.cjs` for frontend behavior regressions (Node.js 18 or newer).

## Basemap configuration

The map uses CARTO vector Dark Matter (dark) and Positron (light), with labeled raster tiles as a fallback if WebGL or vector loading fails. To enable CARTO, set `LOUISIANA911_CARTO_BASEMAP_KEY` on the web process, then restart it. `CARTO_BASEMAP_API_KEY` is also accepted. For local development, put `{"cartoKey":"YOUR_KEY"}` in `instance/basemaps.json` (excluded from Git and Docker builds); environment variables take precedence. Request a basemap key at https://carto.com/basemaps/apikey/. This is separate from the geocoder. The key is passed to the browser for tile requests; keep it out of source control and use the domain registered with CARTO. Without a key, the map uses OpenStreetMap with a monochrome dark appearance. Map-provider credits stay visible.

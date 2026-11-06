# Self-hosted Louisiana map tiles

The map's default basemap (VersaTiles Colorful) comes from `tiles.versatiles.org`,
one server in Europe. From Louisiana each tile takes about 0.5 s, and the style,
tiles and fonts load one after another. This folder serves a Louisiana-only copy
from the NAS at `maps.louisiana911.com`, cached by Cloudflare close to visitors.

The site switches to it only when `LOUISIANA911_MAP_TILE_HOST` is set. Until
then, nothing changes. If our copy fails, browsers fall back to
`tiles.versatiles.org` on their own, then to raster tiles.

## One-time setup (NAS)

1. Make a folder for the tiles, for example `/mnt/<pool>/apps/louisiana911-maps`,
   and copy `config.yaml` into it.
2. Build the first extract. It reads only the Louisiana part of the planet files
   (66.6 GB and 4.4 GB upstream) over HTTP range requests.

   ```sh
   MAP_TILES_DIR=/mnt/<pool>/apps/louisiana911-maps ./update-map-tiles.sh
   ```

   The size and duration of the extract have not been measured yet; expect
   hundreds of MB, not GB.
3. Start the server from this folder:

   ```sh
   MAP_TILES_DIR=/mnt/<pool>/apps/louisiana911-maps docker compose up -d
   ```

4. Check it on the NAS. Every line should print `200`:

   ```sh
   for p in assets/styles/colorful-dark/style.json assets/styles/colorful/style.json \
            assets/sprites/base@2x.json assets/glyphs/noto_sans_regular/0-255.pbf \
            tiles/osm/11/490/828 tiles/landcover-vectors/10/245/414; do
     curl -s -o /dev/null -w "%{http_code} $p\n" "http://localhost:8095/$p"
   done
   ```

   A `404` on an `assets/` path means the bundled frontend lays files out
   differently from `tiles.versatiles.org`. Don't set the site variable yet.
   Visitors would still get a map (they fall back to the public server), but
   they'd gain nothing.

## Cloudflare

1. **Tunnel:** in Zero Trust → Networks → Tunnels → your tunnel → Public hostnames,
   add `maps.louisiana911.com` → `http://<NAS LAN IP>:8095`.
2. **Cache rule:** Caching → Cache Rules → Create rule:
   - When: Hostname equals `maps.louisiana911.com`
   - Then: Eligible for cache. Edge TTL: ignore cache-control header, 7 days.
     Browser TTL: respect origin (1 day).

   Tile URLs have no file extension, so Cloudflare won't cache them without this
   rule.
3. Check from any computer. Run it twice: the second time should show
   `cf-cache-status: HIT`, along with an `access-control-allow-origin` for the site.

   ```sh
   curl -s -o /dev/null -D - -H "Origin: https://louisiana911.com" \
     https://maps.louisiana911.com/tiles/osm/11/490/828 | grep -i -E "^HTTP|cf-cache-status|access-control-allow-origin"
   ```

## Turn it on

Set `LOUISIANA911_MAP_TILE_HOST=https://maps.louisiana911.com` on the
Louisiana911 web container and restart it. The site then rewrites every
`tiles.versatiles.org` request (style, tiles, fonts, sprites) to this host and
preconnects to it. Remove the variable to switch back.

## Keeping it current

OpenStreetMap changes daily; VersaTiles rebuilds the planet file periodically.
Add a monthly cron job on the NAS (TrueNAS: System → Advanced → Cron Jobs):

```sh
MAP_TILES_DIR=/mnt/<pool>/apps/louisiana911-maps /path/to/deploy/maps/update-map-tiles.sh
```

It verifies the new extract before swapping it in and leaves the current
tiles in place if anything fails. Visitors see updates once Cloudflare's 7-day
copy expires, or right away if you purge `maps.louisiana911.com` from the cache.

## Upgrading VersaTiles

`compose.yaml` and `update-map-tiles.sh` pin `v5.3.0-alpine`. To upgrade,
change both tags, run the refresh script, and repeat the checks above.

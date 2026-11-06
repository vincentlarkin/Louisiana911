# Self-hosted Louisiana map tiles

The map's default basemap (VersaTiles Colorful) normally comes from
`tiles.versatiles.org`, a single server in Europe. Measured in Chrome on a fast
connection, a cold first draw took 1.57 s, against 0.32 s once the tiles were
cached. So about 1.25 s of every new view was spent waiting on Europe. This
folder serves a Louisiana-only copy from the NAS at
`https://louisiana911.com/basemap/`, through the site's existing nginx and
Cloudflare tunnel. It needs no new hostname, no tunnel change and no CORS.

The site uses it only when `LOUISIANA911_MAP_TILE_HOST` is set. If our copy
fails, browsers fall back to `tiles.versatiles.org` on their own, then to raster
tiles.

## Setup (NAS)

1. Make a folder, for example `/mnt/Pool3x2/web/louisiana911-maps`, and copy in
   `config.yaml`, `compose.yaml` and `update-map-tiles.sh`.
2. Build the first extract. It reads only Louisiana (bbox
   `-95.1,27.9,-87.8,34.0`, about 154,000 tiles) out of the planet files over
   HTTP range requests, and takes roughly an hour:

   ```sh
   MAP_TILES_DIR=/mnt/Pool3x2/web/louisiana911-maps ./update-map-tiles.sh
   ```

3. Start the server on nginx's Docker network:

   ```sh
   MAP_TILES_DIR=/mnt/Pool3x2/web/louisiana911-maps docker compose up -d
   ```

4. Add `nginx-basemap.conf` inside both `louisiana911.com` server blocks of
   `conf.d/louisiana911.conf`, before `location /`. Then run `nginx -t` and
   reload nginx.
5. Check through the public site. Every line should print `200`:

   ```sh
   for p in assets/styles/colorful-dark/style.json assets/styles/colorful/style.json \
            assets/sprites/base@2x.json assets/glyphs/noto_sans_regular/0-255.pbf \
            tiles/osm/11/490/828 tiles/landcover-vectors/10/245/414; do
     curl -s -o /dev/null -w "%{http_code} $p\n" "https://louisiana911.com/basemap/$p"
   done
   ```

## Turn it on

Set `LOUISIANA911_MAP_TILE_HOST=https://louisiana911.com/basemap` on the
Louisiana911 web container and restart it. Remove the variable to switch back.

## Optional: Cloudflare edge cache

Tile URLs have no file extension, so Cloudflare passes them through to the NAS
rather than caching them. That is already far faster than Europe. To also
serve them from Cloudflare's edge, add a Cache Rule:

- When the URI path starts with `/basemap/`
- Then mark it eligible for cache, with an Edge TTL of 7 days

## Keeping it current

Add a monthly cron job on the NAS (TrueNAS: System → Advanced → Cron Jobs):

```sh
MAP_TILES_DIR=/mnt/Pool3x2/web/louisiana911-maps /mnt/Pool3x2/web/louisiana911-maps/update-map-tiles.sh
```

It checks the new extract before swapping it in, keeps the current tiles if
anything fails, and restarts the server.

## Upgrading VersaTiles

`compose.yaml` and `update-map-tiles.sh` pin `v5.3.0-alpine`. To upgrade, change
both tags, run the refresh script, and repeat step 5.

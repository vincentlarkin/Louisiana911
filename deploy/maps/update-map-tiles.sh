#!/bin/sh
# Refresh the Louisiana basemap tiles served at maps.louisiana911.com.
#
# Cuts Louisiana (plus a margin) out of the VersaTiles planet files without
# downloading them whole, checks the result, swaps it in, and restarts the
# server. The old tiles keep serving until the new ones are verified, so a
# failed run changes nothing. Safe to run monthly from cron.
#
# Usage: MAP_TILES_DIR=/mnt/<pool>/apps/louisiana911-maps ./update-map-tiles.sh
set -eu

DATA_DIR="${MAP_TILES_DIR:?set MAP_TILES_DIR to the folder mounted at /data in compose.yaml}"
CONTAINER="${MAP_TILES_CONTAINER:-louisiana911-maps}"
IMAGE="versatiles/versatiles:v5.3.0-alpine"
# Louisiana spans about -94.05..-88.82 lon, 28.93..33.02 lat. Keep about a
# degree of neighbors so the statewide view has no empty edges.
BBOX="-95.1,27.9,-87.8,34.0"
INCOMING="$DATA_DIR/.incoming"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

mkdir -p "$INCOMING"
for name in osm landcover-vectors; do
  rm -f "$INCOMING/$name.versatiles"
  log "extracting $name for bbox $BBOX"
  docker run --rm -v "$INCOMING":/data "$IMAGE" \
    convert --bbox-border 3 --bbox "$BBOX" \
    "https://download.versatiles.org/$name.versatiles" "$name.versatiles"
done

# A truncated or empty extract must never replace working tiles.
osm_bytes=$(wc -c < "$INCOMING/osm.versatiles")
landcover_bytes=$(wc -c < "$INCOMING/landcover-vectors.versatiles")
if [ "$osm_bytes" -lt 20000000 ] || [ "$landcover_bytes" -lt 100000 ]; then
  log "extract looks incomplete (osm=$osm_bytes bytes, landcover=$landcover_bytes bytes); keeping current tiles"
  exit 1
fi
for name in osm landcover-vectors; do
  docker run --rm -v "$INCOMING":/data "$IMAGE" probe "$name.versatiles" > /dev/null
done

# Same filesystem, so each rename is atomic.
mv -f "$INCOMING/osm.versatiles" "$DATA_DIR/osm.versatiles"
mv -f "$INCOMING/landcover-vectors.versatiles" "$DATA_DIR/landcover-vectors.versatiles"
log "installed osm=$osm_bytes bytes, landcover=$landcover_bytes bytes"

if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker restart "$CONTAINER" > /dev/null
  log "restarted $CONTAINER"
else
  log "container $CONTAINER is not running; start it with: docker compose up -d"
fi

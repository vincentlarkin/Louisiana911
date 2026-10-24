#!/usr/bin/bash
# Production deploy for Louisiana911 on the server. Runs as root, invoked by
# deploy/bin/deploy-louisiana with a commit SHA that has already
# been checked against the tip of main. The installed copy lives at
# /srv/deploy/private/louisiana.sh; see deploy/README.md.
set -euo pipefail

APP_ROOT="/srv/app"
LIVE_REPO_DIR="${APP_ROOT}/repo"
SRC_REPO_DIR="${APP_ROOT}/.deploy_src_repo"
TMP_BASE="${APP_ROOT}/.deploy_tmp"
COMPOSE_FILE="${APP_ROOT}/docker-compose.yml"
INSTALLED_SCRIPT="/srv/deploy/private/louisiana.sh"
REPO_URL="https://github.com/vincentlarkin/Louisiana911.git"
IMAGE="la911"
DEPLOY_SHA="$1"
TS="$(date +%Y%m%d_%H%M%S)"
NEW_DIR="${TMP_BASE}/repo_live_${TS}"
OLD_DIR="${TMP_BASE}/repo_old_${TS}"
SWAPPED=0

compose() {
  docker compose -f "$COMPOSE_FILE" --project-directory "$APP_ROOT" "$@"
}

wait_for_docker() {
  for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

# A Docker daemon crash stops every container on the host, not just ours.
# Remember what was running so the exit handler can bring it all back.
RUNNING_BEFORE="$(docker ps --format '{{.Names}}')"

restart_stopped_containers() {
  if ! wait_for_docker; then
    echo "Docker is not responding; containers must be started by hand"
    return
  fi
  for name in $RUNNING_BEFORE; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "false" ]; then
      echo "Restarting $name, which was running before the deploy"
      docker start "$name" >/dev/null || echo "Could not start $name"
    fi
  done
}

on_exit() {
  status=$?
  trap - EXIT
  if [ "$status" != "0" ] && [ "$SWAPPED" = "1" ]; then
    echo "Deploy failed after swap; restoring previous release"
    if wait_for_docker && docker image inspect "${IMAGE}:previous" >/dev/null 2>&1; then
      if [ -d "$OLD_DIR" ]; then
        rm -rf "$LIVE_REPO_DIR"
        mv "$OLD_DIR" "$LIVE_REPO_DIR"
      fi
      docker tag "${IMAGE}:previous" "${IMAGE}:latest"
      compose up -d || echo "Could not restart the previous release"
    fi
  fi
  restart_stopped_containers
  exit "$status"
}
trap on_exit EXIT

echo "Starting deploy of ${DEPLOY_SHA} at $(date -Is)"
mkdir -p "$TMP_BASE"

if [ -d "$SRC_REPO_DIR/.git" ]; then
  git -C "$SRC_REPO_DIR" remote set-url origin "$REPO_URL"
else
  rm -rf "$SRC_REPO_DIR"
  git clone --depth 1 "$REPO_URL" "$SRC_REPO_DIR"
fi
git -C "$SRC_REPO_DIR" fetch --depth 1 origin "$DEPLOY_SHA"
git -C "$SRC_REPO_DIR" reset --hard "$DEPLOY_SHA"
git -C "$SRC_REPO_DIR" clean -fdx
test "$(git -C "$SRC_REPO_DIR" rev-parse HEAD)" = "$DEPLOY_SHA"

# GitHub is the master copy of the stack definition and this script, but a
# push must not be able to change what runs as root on the NAS. Refuse to
# deploy until an administrator installs the reviewed copies.
for pair in "deploy/docker-compose.yml:$COMPOSE_FILE" "deploy/louisiana.sh:$INSTALLED_SCRIPT"; do
  tracked="$SRC_REPO_DIR/${pair%%:*}"
  installed="${pair#*:}"
  if ! cmp -s "$tracked" "$installed"; then
    echo "Refused: $installed differs from ${pair%%:*} in the repository."
    echo "Review the change and install it as root (see deploy/README.md)."
    exit 5
  fi
done

# Build before touching the running release, so a failed build (or a Docker
# crash during it) leaves the live site exactly as it was.
echo "Building ${IMAGE}:${DEPLOY_SHA}"
docker build --pull --no-cache -t "${IMAGE}:${DEPLOY_SHA}" "$SRC_REPO_DIR"

echo "Preparing release directory"
rm -rf "$NEW_DIR" "$OLD_DIR"
mkdir -p "$NEW_DIR"
rsync -a --delete --exclude '.git' --exclude '.github' --exclude '.env' --exclude '.env.*' \
  "$SRC_REPO_DIR"/ "$NEW_DIR"/
test ! -d "$NEW_DIR/.git"
test -f "$NEW_DIR/Dockerfile"

if docker image inspect "${IMAGE}:latest" >/dev/null 2>&1; then
  docker tag "${IMAGE}:latest" "${IMAGE}:previous"
fi
if [ -d "$LIVE_REPO_DIR" ]; then
  mv "$LIVE_REPO_DIR" "$OLD_DIR"
fi
mv "$NEW_DIR" "$LIVE_REPO_DIR"
SWAPPED=1

echo "Starting containers"
docker tag "${IMAGE}:${DEPLOY_SHA}" "${IMAGE}:latest"
compose up -d --remove-orphans

echo "Waiting for la911_web to report healthy"
for _ in $(seq 1 30); do
  health="$(docker inspect -f '{{.State.Health.Status}}' la911_web 2>/dev/null || echo missing)"
  [ "$health" = "healthy" ] && break
  sleep 3
done
if [ "$health" != "healthy" ]; then
  echo "la911_web is ${health} after the deploy"
  exit 6
fi

rm -rf "$OLD_DIR"
# Keep only latest, previous and this build's tag.
docker images "$IMAGE" --format '{{.Tag}}' \
  | { grep -v -x -e latest -e previous -e "$DEPLOY_SHA" || true; } \
  | while read -r tag; do docker rmi "${IMAGE}:${tag}" >/dev/null || true; done
echo "Deploy complete at $(date -Is)"

# Production deployment

Louisiana911 runs on TrueNAS as the `la911` Compose project:

| Container | Role |
| --- | --- |
| `la911_web` | gunicorn web UI and API, behind the `ix-my-website` nginx |
| `la911_collector` | feed collector, writes the shared SQLite database |

Both use the `la911:latest` image built from this repository.

## Layout on the NAS

```
/mnt/Pool3x2/web/Caddo911/
  docker-compose.yml     installed copy of deploy/docker-compose.yml
  .louisiana911.env      secrets for la911_web (not in git)
  repo/                  current release, no .git
  data/                  SQLite databases and backups
/mnt/Pool3x2/deployment-control/private/louisiana.sh
                         installed copy of deploy/louisiana.sh
```

The directory keeps its historical `Caddo911` name, as do the database files.

## How a deploy runs

A push to `main` runs `.github/workflows/deploy-truenas.yml`, which connects
over Tailscale with a key that can only run `deploy <sha>`. The NAS checks the
SHA is the tip of `main`, then `louisiana.sh`:

1. Fetches the commit into `.deploy_src_repo`.
2. Refuses to continue if `deploy/docker-compose.yml` or `deploy/louisiana.sh`
   differ from the installed copies.
3. Builds `la911:<sha>` without touching the running release.
4. Swaps `repo/`, retags `la911:latest` (the old one becomes `la911:previous`)
   and runs `docker compose up -d`.
5. Waits for `la911_web` to be healthy, otherwise restores the previous release.
6. On any exit, restarts every container that was running before the deploy.
   A Docker daemon crash stops all containers on the host, and they are not
   restarted automatically.

## Changing the stack or the deploy script

The repository is the master copy, but a push cannot change what runs as root.
After reviewing and merging a change to either file, install it on the NAS as
root and then deploy:

```sh
src=/mnt/Pool3x2/web/Caddo911/.deploy_src_repo
git -C "$src" fetch --depth 1 origin main && git -C "$src" reset --hard origin/main
install -m 644 "$src/deploy/docker-compose.yml" /mnt/Pool3x2/web/Caddo911/docker-compose.yml
install -m 700 "$src/deploy/louisiana.sh" /mnt/Pool3x2/deployment-control/private/louisiana.sh
```

Then re-run the workflow from the Actions tab (or push again).

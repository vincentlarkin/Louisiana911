#!/usr/bin/bash
# Nightly off-NAS backup staging for Louisiana911. Runs as root from a TrueNAS
# cron job at 3:30 AM Central, after the collector's 3:00 AM archive run. A
# TrueNAS Cloud Sync task copies the staging folder to OneDrive at 4:00 AM.
# The installed copy lives at
# /mnt/Pool3x2/deployment-control/private/offsite-backup.sh; see deploy/README.md.
set -euo pipefail

DATA_DIR="/mnt/Pool3x2/web/Caddo911/data"
STAGING_DIR="/mnt/Pool3x2/web/Caddo911/offsite"
KEEP_DAILY_LOCAL=14

exec python3 - "$DATA_DIR" "$STAGING_DIR" "$KEEP_DAILY_LOCAL" <<'PY'
import datetime
import glob
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

data_dir, staging_dir, keep_daily = sys.argv[1], sys.argv[2], int(sys.argv[3])
daily_dir = os.path.join(staging_dir, "daily")
archive_dir = os.path.join(staging_dir, "archives")
os.makedirs(daily_dir, exist_ok=True)
os.makedirs(archive_dir, exist_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(source, work_dir):
    """Consistent copy via the SQLite backup API, verified before it is kept."""
    target = os.path.join(work_dir, os.path.basename(source))
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        count = check.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise SystemExit(f"{source}: integrity check failed: {result}")
    return target, count


def compress_into(source, destination):
    """Write destination.gz atomically so a crash never leaves a partial file."""
    partial = destination + ".partial"
    with open(source, "rb") as raw, gzip.open(partial, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed, 1 << 20)
    os.replace(partial, destination)


today = datetime.date.today().isoformat()
summary = []
with tempfile.TemporaryDirectory(dir=staging_dir, prefix=".work-") as work_dir:
    live, live_count = snapshot(os.path.join(data_dir, "caddo911.db"), work_dir)
    compress_into(live, os.path.join(daily_dir, f"caddo911_{today}.db.gz"))
    summary.append(f"caddo911.db {live_count} incidents -> daily/caddo911_{today}.db.gz")
    os.remove(live)

    # Archives only change while their month is still being filled; replace the
    # staged copy when the content differs so OneDrive receives the new version.
    for source in sorted(glob.glob(os.path.join(data_dir, "caddo911_archive_*.db"))):
        copy, count = snapshot(source, work_dir)
        name = os.path.basename(source)
        marker = os.path.join(archive_dir, name + ".sha256")
        digest = sha256(copy)
        previous = open(marker).read().strip() if os.path.exists(marker) else None
        if digest != previous:
            compress_into(copy, os.path.join(archive_dir, name + ".gz"))
            with open(marker + ".partial", "w") as stream:
                stream.write(digest + "\n")
            os.replace(marker + ".partial", marker)
            summary.append(f"{name} {count} incidents -> updated")
        os.remove(copy)

for stale in sorted(glob.glob(os.path.join(daily_dir, "caddo911_*.db.gz")))[:-keep_daily]:
    os.remove(stale)

with open(os.path.join(staging_dir, "SHA256SUMS.partial"), "w") as stream:
    for path in sorted(glob.glob(os.path.join(staging_dir, "*", "*.gz"))):
        stream.write(f"{sha256(path)}  {os.path.relpath(path, staging_dir)}\n")
os.replace(os.path.join(staging_dir, "SHA256SUMS.partial"), os.path.join(staging_dir, "SHA256SUMS"))

stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
with open(os.path.join(staging_dir, "LAST_BACKUP.txt"), "w") as stream:
    stream.write(f"Last backup: {stamp}\n" + "\n".join(summary) + "\n")
print(f"Backup staged at {stamp}")
print("\n".join(summary))
PY

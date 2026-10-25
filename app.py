#!/usr/bin/env python3
"""
Louisiana 911 - statewide public emergency incident monitor.

Aggregates official live and delayed public-safety feeds without claiming to
be an emergency service or a replacement for calling 911.
"""

import sqlite3
import ipaddress
import hashlib
import base64
import hmac
import secrets
import time
import argparse
import os
import sys
import math
import re
import json
import functools
import html as html_module
from urllib.parse import urlsplit
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from threading import Lock, Thread
from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from geopy.geocoders import Nominatim
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from sources import caddo as caddo_source
from sources import lafayette as lafayette_source
from sources import batonrouge as batonrouge_source
from sources import neworleans as neworleans_source
from sources import lakecharles as lakecharles_source

app = Flask(__name__, static_folder='public', static_url_path='')


def _env_setting(name: str, legacy_name: str, default: str = '') -> str:
    """Read the Louisiana911 setting while preserving Caddo911 deployments."""
    primary = os.environ.get(name)
    if primary is not None:
        return primary
    legacy = os.environ.get(legacy_name)
    if legacy is not None:
        return legacy
    return default


# Database setup
def _resolve_db_path() -> str:
    """
    Pick the live SQLite database path.

    Production installs commonly keep the git checkout in ./repo and the
    persistent SQLite files in a sibling ./data directory. Keep
    LOUISIANA911_DB_PATH (or the legacy CADDO911_DB_PATH) is the strongest
    override. The stored filename remains compatible with existing installs.
    """
    configured_path = _env_setting('LOUISIANA911_DB_PATH', 'CADDO911_DB_PATH').strip()
    if configured_path:
        return os.path.expanduser(configured_path)

    app_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    configured_data_dir = _env_setting('LOUISIANA911_DATA_DIR', 'CADDO911_DATA_DIR').strip()
    candidate_dirs = [
        configured_data_dir,
        os.path.join(app_dir, 'data'),
        os.path.join(os.path.dirname(app_dir), 'data'),
        os.path.join(cwd, 'data'),
        os.path.join(os.path.dirname(cwd), 'data'),
        '/data',
    ]

    seen: set[str] = set()
    for data_dir in candidate_dirs:
        if not data_dir:
            continue
        db_path = os.path.abspath(os.path.expanduser(os.path.join(data_dir, 'caddo911.db')))
        if db_path in seen:
            continue
        seen.add(db_path)
        if os.path.exists(db_path):
            return db_path

    return 'caddo911.db'


DB_PATH = _resolve_db_path()

# Archive settings: incidents older than this many days get moved to monthly archive DBs
ARCHIVE_AFTER_DAYS = int(_env_setting('LOUISIANA911_ARCHIVE_DAYS', 'CADDO911_ARCHIVE_DAYS', '30'))
BACKUP_RETENTION_WEEKS = int(_env_setting('LOUISIANA911_BACKUP_RETENTION_WEEKS', 'CADDO911_BACKUP_RETENTION_WEEKS', '5'))

def _get_archive_dir() -> str:
    """Get the directory where archive DBs are stored (same dir as main DB)."""
    return os.path.dirname(os.path.abspath(DB_PATH)) or '.'

def _get_archive_db_path(year: int, month: int) -> str:
    """Get path for a monthly archive database."""
    archive_dir = _get_archive_dir()
    return os.path.join(archive_dir, f"caddo911_archive_{year:04d}_{month:02d}.db")

def _list_archive_dbs() -> list[str]:
    """List all archive database files."""
    archive_dir = _get_archive_dir()
    if not os.path.isdir(archive_dir):
        return []
    return sorted([
        os.path.join(archive_dir, f) 
        for f in os.listdir(archive_dir) 
        if f.startswith('caddo911_archive_') and f.endswith('.db')
    ])


def _get_backup_dir() -> str:
    """Directory where periodic backup snapshots are written."""
    configured = _env_setting('LOUISIANA911_BACKUP_DIR', 'CADDO911_BACKUP_DIR').strip()
    if configured:
        return configured
    return os.path.join(_get_archive_dir(), "backups")


def _backup_label_for_path(path: str) -> str:
    """Stable label for backup file naming."""
    abs_target = os.path.abspath(path)
    abs_main = os.path.abspath(DB_PATH)
    if abs_target == abs_main:
        return "main"
    base = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_") or "db"

def _sqlite_hot_backup(src_path: str, dst_path: str) -> None:
    """
    SQLite-consistent snapshot copy using the built-in backup API.
    Safer than raw file copies when WAL is enabled.
    """
    src_conn = sqlite3.connect(src_path, timeout=30, check_same_thread=False)
    dst_conn = sqlite3.connect(dst_path, timeout=30, check_same_thread=False)
    try:
        src_conn.execute("PRAGMA busy_timeout = 5000;")
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        try:
            dst_conn.close()
        except Exception:
            pass
        try:
            src_conn.close()
        except Exception:
            pass

def _prune_old_backups(backup_dir: str) -> list[str]:
    """
    Keep only N weekly backups per database label if retention is set.
    Returns a list of deleted files.
    """
    keep = int(BACKUP_RETENTION_WEEKS)
    if keep <= 0 or not os.path.isdir(backup_dir):
        return []
    grouped: dict[str, list[str]] = {}
    for name in os.listdir(backup_dir):
        if not name.endswith(".db"):
            continue
        full = os.path.join(backup_dir, name)
        if not os.path.isfile(full):
            continue
        # Format: <label>_YYYYMMDD_HHMMSS.db
        m = re.match(r"^(?P<label>.+)_\d{8}_\d{6}\.db$", name)
        if not m:
            continue
        label = m.group("label")
        grouped.setdefault(label, []).append(full)
    removed: list[str] = []
    for _, files in grouped.items():
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
                removed.append(stale)
            except Exception:
                pass
    return removed

def create_backup_snapshot(*, include_archives: bool = True) -> dict:
    """
    Create timestamped SQLite backups.
    - Always includes the main DB.
    - Optionally includes the monthly archives.
    """
    backup_dir = _get_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    targets = [DB_PATH]
    if include_archives:
        targets.extend(_list_archive_dbs())
    created: list[str] = []
    skipped: list[str] = []
    for src in targets:
        if not os.path.exists(src):
            skipped.append(src)
            continue
        label = _backup_label_for_path(src)
        out_path = os.path.join(backup_dir, f"{label}_{stamp}.db")
        _sqlite_hot_backup(src, out_path)
        created.append(out_path)
    removed = _prune_old_backups(backup_dir)
    return {
        "created": created,
        "skipped": skipped,
        "removed": removed,
        "backup_dir": backup_dir,
    }

def _archive_db_connect(path: str, *, row_factory: bool = False) -> sqlite3.Connection:
    """Connect to an archive database."""
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def _init_archive_db(path: str) -> None:
    """Initialize an archive database with the same schema as main DB."""
    conn = _archive_db_connect(path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE,
            agency TEXT,
            time TEXT,
            units INTEGER,
            description TEXT,
            street TEXT,
            cross_streets TEXT,
            municipality TEXT,
            source TEXT DEFAULT 'caddo',
            latitude REAL,
            longitude REAL,
            first_seen DATETIME,
            last_seen DATETIME,
            is_active INTEGER DEFAULT 0,
            geocode_source TEXT,
            geocode_quality TEXT,
            geocode_query TEXT,
            geocoded_at DATETIME,
            geocode_version INTEGER
        )
    ''')
    try:
        cursor.execute("ALTER TABLE incidents ADD COLUMN source TEXT DEFAULT 'caddo'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE incidents ADD COLUMN geocode_version INTEGER")
    except sqlite3.OperationalError:
        pass
    _init_peak_units(cursor)
    cursor.execute("UPDATE incidents SET source = 'caddo' WHERE source IS NULL OR TRIM(source) = ''")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON incidents(hash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_first_seen ON incidents(first_seen)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source ON incidents(source)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source_first_seen ON incidents(source, first_seen DESC)')
    conn.commit()
    conn.close()

# Scraper/status metadata
SCRAPE_INTERVAL_SECONDS_DEFAULT = 60
feed_refreshed_at: str | None = None  # backwards-compatible: Caddo refresh text
feed_refreshed_by_source: dict[str, str | None] = {
    "caddo": None,
    "lafayette": None,
    "batonrouge": None,
    "neworleans": None,
    "lakecharles": None,
}
last_scrape_started_at: str | None = None  # ISO UTC
last_scrape_finished_at: str | None = None  # ISO UTC

# Identify our automated feed reader and its home page in upstream logs.
SCRAPER_USER_AGENT = "Louisiana 911 Friendly Feed Reader (Made in Louisiana; automated public-safety updates; +https://louisiana911.com/)"

QUIET = False

# Louisiana is in US Central time. Use an IANA name so DST is handled (CST/CDT).
# On Windows, this requires the `tzdata` pip package (added to requirements.txt).
# If zoneinfo data isn't available at runtime, fall back to a fixed CST offset so we don't crash.
CENTRAL_TZ_IS_FALLBACK = False
try:
    CENTRAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    CENTRAL_TZ = timezone(timedelta(hours=-6))  # CST (no DST)
    CENTRAL_TZ_IS_FALLBACK = True

def log(message: str) -> None:
    """Lightweight logger (avoids emoji for Windows terminals)."""
    if not QUIET:
        print(message, flush=True)

def db_connect(*, row_factory: bool = False) -> sqlite3.Connection:
    """
    Create a SQLite connection with sensible defaults for concurrent reader/writer usage
    (collector + web UI in the same process).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def _init_peak_units(cursor):
    """Seed known unit peaks without inventing earlier assignment history."""
    columns = {row[1] for row in cursor.execute('PRAGMA table_info(incidents)')}
    if 'peak_units' not in columns:
        cursor.execute('ALTER TABLE incidents ADD COLUMN peak_units INTEGER')
        cursor.execute('UPDATE incidents SET peak_units = units')


def init_db():
    """Initialize SQLite database with incidents table"""
    conn = db_connect()
    cursor = conn.cursor()
    # Better concurrency for collector + UI
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE,
            agency TEXT,
            time TEXT,
            units INTEGER,
            description TEXT,
            street TEXT,
            cross_streets TEXT,
            municipality TEXT,
            source TEXT DEFAULT 'caddo',
            latitude REAL,
            longitude REAL,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON incidents(hash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_active ON incidents(is_active)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_active_first_seen ON incidents(is_active, first_seen DESC, id DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_first_seen ON incidents(first_seen)')

    # Add geocoding metadata columns (safe to run on an existing DB; does NOT delete data)
    # Note: SQLite doesn't support ADD COLUMN IF NOT EXISTS in older versions, so we try/except.
    for col_name, col_type in (
        ("source", "TEXT DEFAULT 'caddo'"),
        ("geocode_source", "TEXT"),     # 'arcgis' | 'osm' | 'unresolved'
        ("geocode_quality", "TEXT"),    # 'street-segment' | 'intersection-2' | 'street+cross' | lower-confidence values
        ("geocode_query", "TEXT"),      # the query string we sent to the provider
        ("geocoded_at", "DATETIME"),    # UTC ISO timestamp
        ("geocode_version", "INTEGER"), # identifies results made by the current validation/ranking logic
    ):
        try:
            cursor.execute(f"ALTER TABLE incidents ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass

    _init_peak_units(cursor)
    # Backfill legacy rows that predate multi-source support.
    cursor.execute("UPDATE incidents SET source = 'caddo' WHERE source IS NULL OR TRIM(source) = ''")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source ON incidents(source)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_source_active_first_seen ON incidents(source, is_active, first_seen DESC)')

    # Metadata table (shared across collector + web processes)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def meta_set(key: str, value: str | None) -> None:
    if value is None:
        return
    conn = db_connect()
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()

def meta_get_many(keys: list[str]) -> dict[str, str]:
    conn = db_connect(row_factory=True)
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(keys))
    cursor.execute(f"SELECT key, value FROM meta WHERE key IN ({placeholders})", keys)
    rows = cursor.fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows} if rows else {}

def archive_old_incidents(*, dry_run: bool = False) -> dict:
    """
    Move incidents older than ARCHIVE_AFTER_DAYS to monthly archive databases.
    Returns stats about what was archived.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)
    cutoff_iso = cutoff.isoformat()
    
    conn = db_connect(row_factory=True)
    cursor = conn.cursor()
    
    # Find inactive incidents older than cutoff
    cursor.execute('''
        SELECT * FROM incidents 
        WHERE is_active = 0 AND first_seen < ?
        ORDER BY first_seen ASC
    ''', (cutoff_iso,))
    rows = cursor.fetchall()
    
    if not rows:
        conn.close()
        log(f"[ARCHIVE] No incidents older than {ARCHIVE_AFTER_DAYS} days to archive")
        return {'archived': 0, 'files': []}
    
    log(f"[ARCHIVE] Found {len(rows)} incidents to archive{' (dry run)' if dry_run else ''}")
    
    # Group by year-month based on first_seen (in Central time)
    by_month: dict[tuple[int, int], list[dict]] = {}
    for row in rows:
        row_dict = dict(row)
        first_seen = row_dict.get('first_seen')
        if not first_seen:
            continue
        dt = _parse_iso_datetime(first_seen)
        if not dt:
            continue
        # Convert to Central time for archiving by local month
        central_dt = dt.astimezone(CENTRAL_TZ)
        key = (central_dt.year, central_dt.month)
        if key not in by_month:
            by_month[key] = []
        by_month[key].append(row_dict)
    
    archived_count = 0
    archived_files = []
    hashes_to_delete = []
    
    for (year, month), incidents in sorted(by_month.items()):
        archive_path = _get_archive_db_path(year, month)
        log(f"[ARCHIVE] {year}-{month:02d}: {len(incidents)} incidents -> {os.path.basename(archive_path)}")
        
        if not dry_run:
            _init_archive_db(archive_path)
            archive_conn = _archive_db_connect(archive_path)
            archive_cursor = archive_conn.cursor()
            
            for inc in incidents:
                # Refresh a prior copy after interrupted archiving or later corrections.
                try:
                    archive_cursor.execute('''
                        INSERT INTO incidents
                        (hash, agency, time, units, description, street, cross_streets, municipality, source,
                         latitude, longitude, first_seen, last_seen, is_active,
                         geocode_source, geocode_quality, geocode_query, geocoded_at, geocode_version, peak_units)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(hash) DO UPDATE SET
                            agency=excluded.agency, time=excluded.time, units=excluded.units,
                            peak_units=MAX(COALESCE(incidents.peak_units, 0), COALESCE(incidents.units, 0), COALESCE(excluded.peak_units, 0), COALESCE(excluded.units, 0)),
                            description=excluded.description, street=excluded.street,
                            cross_streets=excluded.cross_streets, municipality=excluded.municipality,
                            source=excluded.source, latitude=excluded.latitude, longitude=excluded.longitude,
                            first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                            is_active=excluded.is_active, geocode_source=excluded.geocode_source,
                            geocode_quality=excluded.geocode_quality, geocode_query=excluded.geocode_query,
                            geocoded_at=excluded.geocoded_at, geocode_version=excluded.geocode_version
                    ''', (
                        inc.get('hash'),
                        inc.get('agency'),
                        inc.get('time'),
                        inc.get('units'),
                        inc.get('description'),
                        inc.get('street'),
                        inc.get('cross_streets'),
                        inc.get('municipality'),
                        inc.get('source') or 'caddo',
                        inc.get('latitude'),
                        inc.get('longitude'),
                        inc.get('first_seen'),
                        inc.get('last_seen'),
                        inc.get('is_active', 0),
                        inc.get('geocode_source'),
                        inc.get('geocode_quality'),
                        inc.get('geocode_query'),
                        inc.get('geocoded_at'),
                        inc.get('geocode_version'),
                        inc.get('peak_units', inc.get('units')),
                    ))
                    hashes_to_delete.append((inc.get('hash'), inc.get('last_seen'), inc.get('geocoded_at')))
                    archived_count += 1
                except Exception as e:
                    log(f"[ARCHIVE] Error archiving incident {inc.get('hash')}: {e}")
            
            archive_conn.commit()
            archive_conn.close()
        else:
            archived_count += len(incidents)
        
        if archive_path not in archived_files:
            archived_files.append(archive_path)
    
    # Delete archived incidents from main DB
    if not dry_run and hashes_to_delete:
        log(f"[ARCHIVE] Removing {len(hashes_to_delete)} archived incidents from main DB...")
        cursor.executemany(
            'DELETE FROM incidents WHERE hash = ? AND is_active = 0 AND last_seen IS ? AND geocoded_at IS ?',
            hashes_to_delete,
        )
        conn.commit()
        # SQLite reuses freed pages. Avoid a full database rewrite/writer lock
        # after every routine archive; file compaction belongs in maintenance.
    
    conn.close()
    
    log(f"[ARCHIVE] Complete! Archived {archived_count} incidents to {len(archived_files)} file(s)")
    return {'archived': archived_count, 'files': archived_files}

def _get_archive_dbs_for_date(date_str: str) -> list[str]:
    """Get archive DB paths that might contain data for a given date (YYYY-MM-DD)."""
    try:
        year, month, _ = date_str.split('-')
        archive_path = _get_archive_db_path(int(year), int(month))
        if os.path.exists(archive_path):
            return [archive_path]
    except Exception:
        pass
    return []

def _get_archive_dbs_for_month(month_str: str) -> list[str]:
    """Get archive DB paths for a given month (YYYY-MM)."""
    try:
        year, month = month_str.split('-')
        archive_path = _get_archive_db_path(int(year), int(month))
        if os.path.exists(archive_path):
            return [archive_path]
    except Exception:
        pass
    return []

def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # We store timezone-aware ISO timestamps (UTC) via datetime.now(timezone.utc).isoformat()
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            # Treat naive timestamps as UTC for backwards compatibility.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _central_date_bounds_utc(date_str: str) -> tuple[str, str] | None:
    """Return UTC ISO start/end for a Central date (YYYY-MM-DD)."""
    try:
        year_s, month_s, day_s = (date_str or "").split("-")
        year, month, day = int(year_s), int(month_s), int(day_s)
        start_local = datetime(year, month, day, tzinfo=CENTRAL_TZ)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        return start_utc, end_utc
    except Exception:
        return None

def _central_month_bounds_utc(month_str: str) -> tuple[str, str] | None:
    """Return UTC ISO start/end for a Central month (YYYY-MM)."""
    try:
        year_s, month_s = (month_str or "").split("-")
        year, month = int(year_s), int(month_s)
        start_local = datetime(year, month, 1, tzinfo=CENTRAL_TZ)
        if month == 12:
            end_local = datetime(year + 1, 1, 1, tzinfo=CENTRAL_TZ)
        else:
            end_local = datetime(year, month + 1, 1, tzinfo=CENTRAL_TZ)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        return start_utc, end_utc
    except Exception:
        return None

def _central_date_key(value: str | None) -> str | None:
    dt = _parse_iso_datetime(value)
    c = _to_central(dt)
    return c.date().isoformat() if c else None


def _central_month_key(value: str | None) -> str | None:
    dt = _parse_iso_datetime(value)
    c = _to_central(dt)
    return c.strftime('%Y-%m') if c else None

def _to_central(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CENTRAL_TZ)
    except Exception:
        return None

def _format_central_hms(dt: datetime | None) -> str | None:
    c = _to_central(dt)
    if not c:
        return None
    abbr = "CST" if CENTRAL_TZ_IS_FALLBACK else c.strftime("%Z")
    return f"{c.strftime('%H:%M:%S')} {abbr}"

def _format_central_tooltip(dt: datetime | None) -> str | None:
    c = _to_central(dt)
    if not c:
        return None
    abbr = "CST" if CENTRAL_TZ_IS_FALLBACK else c.strftime("%Z")
    return f"{c.strftime('%Y-%m-%d %H:%M:%S')} {abbr}"

# Ensure schema exists even when running under a WSGI server (e.g. gunicorn app:app)
# Safe to call multiple times.
try:
    init_db()
except Exception as e:
    # Don't crash import; the process may not have its volume mounted yet.
    log(f"[WARN] Database init failed: {e}")

AUTH_TOKEN = _env_setting('LOUISIANA911_AUTH_TOKEN', 'CADDO911_AUTH_TOKEN') or None
AUTH_USER = _env_setting('LOUISIANA911_AUTH_USER', 'CADDO911_AUTH_USER') or None
AUTH_PASS = _env_setting('LOUISIANA911_AUTH_PASS', 'CADDO911_AUTH_PASS') or None

REPORT_PAGE_RATE_LIMIT = int(_env_setting('LOUISIANA911_REPORT_PAGE_RATE_LIMIT', 'CADDO911_REPORT_PAGE_RATE_LIMIT', '120'))
REPORT_API_RATE_LIMIT = int(_env_setting('LOUISIANA911_REPORT_API_RATE_LIMIT', 'CADDO911_REPORT_API_RATE_LIMIT', '90'))
# Address lookups call an outside geocoder, so they get a much smaller budget.
REPORT_GEOCODE_RATE_LIMIT = int(_env_setting('LOUISIANA911_REPORT_GEOCODE_RATE_LIMIT', 'CADDO911_REPORT_GEOCODE_RATE_LIMIT', '15'))
INCIDENT_ACTIVE_API_RATE_LIMIT = int(_env_setting('LOUISIANA911_ACTIVE_API_RATE_LIMIT', 'CADDO911_ACTIVE_API_RATE_LIMIT', '120'))
INCIDENT_HISTORY_API_RATE_LIMIT = int(_env_setting('LOUISIANA911_HISTORY_API_RATE_LIMIT', 'CADDO911_HISTORY_API_RATE_LIMIT', '10'))
INCIDENT_OTHER_API_RATE_LIMIT = int(_env_setting('LOUISIANA911_OTHER_API_RATE_LIMIT', 'CADDO911_OTHER_API_RATE_LIMIT', '90'))
INCIDENT_HISTORY_REQUEST_RATE_LIMIT = int(os.environ.get('LOUISIANA911_HISTORY_REQUEST_RATE_LIMIT', '60'))
HISTORY_HOURLY_DATE_LIMIT = int(os.environ.get('LOUISIANA911_HISTORY_HOURLY_DATE_LIMIT', '20'))
HISTORY_DAILY_DATE_LIMIT = int(os.environ.get('LOUISIANA911_HISTORY_DAILY_DATE_LIMIT', '40'))
TRUSTED_PROXY_HOPS = max(0, int(os.environ.get('LOUISIANA911_TRUSTED_PROXY_HOPS', '0')))
ACCESS_LIMIT_DB = os.environ.get('LOUISIANA911_ACCESS_LIMIT_DB', '').strip()
INCIDENT_HISTORY_MAX_LIMIT = int(_env_setting('LOUISIANA911_HISTORY_MAX_LIMIT', 'CADDO911_HISTORY_MAX_LIMIT', '2000'))
REPORT_RATE_WINDOW_SECONDS = int(_env_setting('LOUISIANA911_REPORT_RATE_WINDOW_SECONDS', 'CADDO911_REPORT_RATE_WINDOW_SECONDS', '60'))
HISTORY_UI_SESSION_MAX_AGE_SECONDS = int(_env_setting(
    'LOUISIANA911_HISTORY_UI_SESSION_MAX_AGE_SECONDS',
    'CADDO911_HISTORY_UI_SESSION_MAX_AGE_SECONDS',
    '43200',
))
_history_ui_secret_setting = _env_setting(
    'LOUISIANA911_HISTORY_UI_SECRET',
    'CADDO911_HISTORY_UI_SECRET',
).strip()
HISTORY_UI_SECRET = (
    _history_ui_secret_setting.encode('utf-8')
    if _history_ui_secret_setting
    else secrets.token_bytes(32)
)
HISTORY_UI_COOKIE_NAME = 'l911_history_ui'
HISTORY_UI_REQUEST_HEADER = 'X-Louisiana911-UI'
_report_rate_lock = Lock()
_report_rate_hits: dict[tuple[str, str], list[float]] = {}
_history_date_rate_hits: dict[tuple[str, int], dict[str, float]] = {}

CANONICAL_SITE_HOST = 'louisiana911.com'


def _first_forwarded_header_value(value: str) -> str:
    """Return the first proxy-provided value from a comma-separated header."""
    return (value or '').split(',', 1)[0].strip()


@app.before_request
def _canonical_origin_redirect():
    """Keep public traffic on the single HTTPS, apex-domain canonical origin."""
    forwarded_host = _first_forwarded_header_value(
        request.headers.get('X-Forwarded-Host', '')
    )
    public_host = (forwarded_host or request.host).split(':', 1)[0].lower()
    if public_host not in {CANONICAL_SITE_HOST, f'www.{CANONICAL_SITE_HOST}'}:
        return None

    forwarded_proto = _first_forwarded_header_value(
        request.headers.get('X-Forwarded-Proto', '')
    ).lower()
    cloudflare_visitor = request.headers.get('CF-Visitor', '').lower()
    uses_plain_http = (
        forwarded_proto == 'http'
        or ('"scheme":"http"' in cloudflare_visitor.replace(' ', ''))
    )
    if public_host == f'www.{CANONICAL_SITE_HOST}' or uses_plain_http:
        path_and_query = request.full_path
        if path_and_query.endswith('?'):
            path_and_query = path_and_query[:-1]
        return redirect(
            f'https://{CANONICAL_SITE_HOST}{path_and_query}',
            code=301,
        )
    return None

def _unauthorized():
    resp = jsonify({'error': 'unauthorized'})
    resp.status_code = 401
    if AUTH_USER and AUTH_PASS:
        resp.headers['WWW-Authenticate'] = 'Basic realm="louisiana911-live"'
    return resp

def _check_auth() -> bool:
    # No auth configured → allow
    if not AUTH_TOKEN and not (AUTH_USER and AUTH_PASS):
        return True

    if AUTH_TOKEN:
        provided = request.headers.get('X-Auth-Token')
        return bool(provided) and provided == AUTH_TOKEN

    auth = request.authorization
    return bool(auth) and auth.username == AUTH_USER and auth.password == AUTH_PASS


def _history_ui_user_agent_digest() -> str:
    user_agent = request.headers.get('User-Agent', '')
    return hashlib.sha256(user_agent.encode('utf-8')).hexdigest()[:20]


def _history_ui_signature(payload: str) -> str:
    digest = hmac.new(HISTORY_UI_SECRET, payload.encode('utf-8'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def _new_history_ui_token() -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f'v1:{issued_at}:{nonce}:{_history_ui_user_agent_digest()}'
    return f'{payload}:{_history_ui_signature(payload)}'


def _valid_history_ui_token(token: str | None) -> bool:
    if not token or HISTORY_UI_SESSION_MAX_AGE_SECONDS <= 0:
        return False
    try:
        version, issued_raw, nonce, user_agent_digest, signature = token.split(':', 4)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        return False

    now = int(time.time())
    if version != 'v1' or not nonce or issued_at > now + 60:
        return False
    if now - issued_at > HISTORY_UI_SESSION_MAX_AGE_SECONDS:
        return False
    if not hmac.compare_digest(user_agent_digest, _history_ui_user_agent_digest()):
        return False

    payload = f'{version}:{issued_raw}:{nonce}:{user_agent_digest}'
    return hmac.compare_digest(signature, _history_ui_signature(payload))


def _history_ui_request_guard():
    if request.headers.get(HISTORY_UI_REQUEST_HEADER, '').strip() != 'history':
        return jsonify({'error': 'not_found'}), 404
    if not _valid_history_ui_token(request.cookies.get(HISTORY_UI_COOKIE_NAME)):
        response = jsonify({'error': 'not_found'})
        response.status_code = 404
        _set_history_ui_cookie(response)
        return response
    return None


def _request_uses_https() -> bool:
    forwarded_proto = _first_forwarded_header_value(
        request.headers.get('X-Forwarded-Proto', '')
    ).lower()
    return request.is_secure or forwarded_proto == 'https'


def _is_ui_document_navigation() -> bool:
    fetch_site = request.headers.get('Sec-Fetch-Site', '').strip().lower()
    fetch_mode = request.headers.get('Sec-Fetch-Mode', '').strip().lower()
    fetch_dest = request.headers.get('Sec-Fetch-Dest', '').strip().lower()
    return (
        fetch_site in {'none', 'same-origin'}
        and fetch_mode == 'navigate'
        and fetch_dest == 'document'
    )


def _set_history_ui_cookie(response):
    response.set_cookie(
        HISTORY_UI_COOKIE_NAME,
        _new_history_ui_token(),
        max_age=max(1, HISTORY_UI_SESSION_MAX_AGE_SECONDS),
        secure=_request_uses_https(),
        httponly=True,
        samesite='Strict',
        path='/api/incidents',
    )
    return response


def _carto_basemap_key():
    key = _env_setting('LOUISIANA911_CARTO_BASEMAP_KEY', 'CARTO_BASEMAP_API_KEY').strip()
    if key:
        return key
    try:
        with open(os.path.join(app.instance_path, 'basemaps.json'), encoding='utf-8') as config_file:
            value = json.load(config_file).get('cartoKey', '')
            return value.strip() if isinstance(value, str) else ''
    except (OSError, ValueError, AttributeError):
        return ''


def _read_map_index_html():
    index_path = os.path.join(app.root_path, 'public', 'index.html')
    with open(index_path, 'r', encoding='utf-8') as index_file:
        index_html = index_file.read()
    # Browser basemap keys are intentionally public; keep deployment config out of git.
    key = _carto_basemap_key()
    config = json.dumps({'cartoKey': key}, ensure_ascii=True).replace('<', '\\u003c')
    return index_html.replace(
        '<script type="application/json" id="map-config">{}</script>',
        f'<script type="application/json" id="map-config">{config}</script>',
        1,
    )


def _serve_index_with_history_ui_session():
    response = app.response_class(_read_map_index_html(), mimetype='text/html')
    if _is_ui_document_navigation():
        _set_history_ui_cookie(response)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


def _serve_coverage_map(filename: str, source: str, title: str, summary: str):
    """Keep each indexable coverage page's metadata and article in the map shell."""
    with open(os.path.join(app.root_path, 'public', filename), encoding='utf-8') as page:
        coverage_html = page.read()
    metadata_pattern = (
        r'<title>.*?</title>|'
        r'<meta (?:name="(?:description|robots|twitter:[^"]+)"|property="og:[^"]+")[^>]*>|'
        r'<link rel="canonical"[^>]*>|'
        r'<script type="application/ld\+json">.*?</script>'
    )
    metadata = '\n  '.join(re.findall(metadata_pattern, coverage_html, re.DOTALL))
    index_html = re.sub(metadata_pattern, '', _read_map_index_html(), flags=re.DOTALL)
    index_html = index_html.replace('<head>', '<head>\n  <base href="/">\n  ' + metadata, 1)
    article = re.search(r'<main id="content">(.*?)</main>', coverage_html, re.DOTALL).group(1)
    article = article.replace('<h1>', '<h2>').replace('</h1>', '</h2>')
    panel = f'''
      <section class="coverage-intro" id="coverage-intro" data-source="{source}" aria-labelledby="coverage-title">
        <button class="coverage-close" id="coverage-close" type="button" aria-label="Dismiss coverage information">×</button>
        <p class="coverage-eyebrow">Parish coverage</p>
        <h1 id="coverage-title">{html_module.escape(title)}</h1>
        <p class="coverage-summary">{html_module.escape(summary)}</p>
        <details class="coverage-details">
          <summary>About this feed &amp; coverage</summary>
          <div class="coverage-article">{article}</div>
        </details>
        <noscript>JavaScript is needed to display the interactive map. Feed details and official source links are available above.</noscript>
      </section>
      <button class="coverage-reopen" id="coverage-reopen" type="button" aria-controls="coverage-intro" aria-expanded="true" hidden>About this coverage</button>
    '''
    index_html = index_html.replace('<div id="map"></div>', '<div id="map"></div>' + panel, 1)
    index_html = index_html.replace('<h1>Louisiana 911</h1>', '<span class="brand-title">Louisiana 911</span>', 1)
    response = app.response_class(index_html, mimetype='text/html')
    if _is_ui_document_navigation():
        _set_history_ui_cookie(response)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


INCIDENT_SHARE_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')


def _find_incident_by_share_id(share_id: str) -> dict | None:
    """Find one incident by its stable deduplication hash, including archives."""
    normalized = (share_id or '').strip().lower()
    if not INCIDENT_SHARE_ID_PATTERN.fullmatch(normalized):
        return None

    database_paths = [DB_PATH, *_list_archive_dbs()]
    seen_paths: set[str] = set()
    for database_path in database_paths:
        absolute_path = os.path.abspath(database_path)
        if absolute_path in seen_paths or not os.path.exists(absolute_path):
            continue
        seen_paths.add(absolute_path)
        conn = None
        try:
            conn = sqlite3.connect(absolute_path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout = 5000;')
            row = conn.execute(
                'SELECT * FROM incidents WHERE hash = ? LIMIT 1',
                (normalized,),
            ).fetchone()
            if row is not None:
                incident = dict(row)
                incident['source'] = _normalize_incident_source_for_read(incident.get('source'))
                return incident
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
    return None


def _incident_share_payload(incident: dict) -> dict:
    """Return only fields the public incident page needs."""
    return {
        'id': incident.get('id'),
        'share_id': incident.get('hash'),
        'agency': incident.get('agency'),
        'time': incident.get('time'),
        'reported_at': _incident_reported_at(incident),
        'units': incident.get('units'),
        'description': incident.get('description'),
        'street': incident.get('street'),
        'cross_streets': incident.get('cross_streets'),
        'municipality': incident.get('municipality'),
        'source': _normalize_incident_source_for_read(incident.get('source')),
        'latitude': incident.get('latitude'),
        'longitude': incident.get('longitude'),
        'first_seen': incident.get('first_seen'),
        'last_seen': incident.get('last_seen'),
        'is_active': int(bool(incident.get('is_active'))),
        'geocode_source': incident.get('geocode_source'),
        'geocode_quality': incident.get('geocode_quality'),
    }


def _serve_shared_incident_page(incident: dict):
    """Serve the map shell with incident-specific social metadata and data."""
    payload = _incident_share_payload(incident)
    share_id = str(payload['share_id']).lower()
    share_url = f'https://{CANONICAL_SITE_HOST}/incident/{share_id}'
    description = str(payload.get('description') or 'Public safety incident').strip()
    location = _incident_location_label(incident) or str(payload.get('municipality') or 'Louisiana')
    units = int(payload.get('units') or 0)
    is_active = bool(payload.get('is_active'))

    if payload.get('source') == 'lakecharles':
        status_label = 'CLOSED POLICE CALL'
        summary_lead = 'Lake Charles police call published with a 24-hour delay'
    elif is_active and units >= 5:
        status_label = 'BREAKING - VERY ACTIVE'
        summary_lead = f'Very active public incident with {units} assigned units'
    elif is_active:
        status_label = 'ACTIVE INCIDENT'
        summary_lead = 'Active public incident'
    else:
        status_label = 'INCIDENT REPORT'
        summary_lead = 'Historical public incident'

    page_title = f'{status_label}: {description} | Louisiana911'
    page_description = f'{summary_lead}: {description} near {location}. Public feed details may change.'

    index_html = _read_map_index_html()

    # The main shell historically uses root-relative and document-relative
    # assets. Anchor document-relative URLs at / when it is served from the
    # nested /incident/<id> route.
    index_html = index_html.replace('<head>', '<head>\n  <base href="/">', 1)

    escaped_title = html_module.escape(page_title, quote=True)
    escaped_description = html_module.escape(page_description, quote=True)
    escaped_url = html_module.escape(share_url, quote=True)
    index_html = index_html.replace(
        '<title>Louisiana 911 Calls &amp; Caddo 911 Live Map | Louisiana911</title>',
        f'<title>{escaped_title}</title>',
    )
    index_html = index_html.replace(
        '<meta name="description" content="View live Caddo 911 calls, Baton Rouge and Lafayette traffic, daily New Orleans records, and Lake Charles closed calls with a 24-hour delay on one Louisiana map.">',
        f'<meta name="description" content="{escaped_description}">',
    )
    index_html = index_html.replace(
        '<meta name="robots" content="index,follow,max-image-preview:large">',
        '<meta name="robots" content="noindex,follow,max-image-preview:large">',
    )
    index_html = index_html.replace(
        '<link rel="canonical" href="https://louisiana911.com/">',
        f'<link rel="canonical" href="{escaped_url}">',
    )
    index_html = index_html.replace(
        '<meta property="og:title" content="Louisiana 911 Calls &amp; Caddo 911 Live Map">',
        f'<meta property="og:title" content="{escaped_title}">',
    )
    index_html = index_html.replace(
        '<meta property="og:description" content="Live Caddo 911 calls and traffic feeds alongside delayed New Orleans records and Lake Charles closed calls on one Louisiana map.">',
        f'<meta property="og:description" content="{escaped_description}">',
    )
    index_html = index_html.replace(
        '<meta property="og:url" content="https://louisiana911.com/">',
        f'<meta property="og:url" content="{escaped_url}">',
    )
    index_html = index_html.replace(
        '<meta property="og:type" content="website">',
        '<meta property="og:type" content="article">',
    )
    index_html = index_html.replace(
        '<meta name="twitter:title" content="Louisiana 911 Calls &amp; Caddo 911 Live Map">',
        f'<meta name="twitter:title" content="{escaped_title}">',
    )
    index_html = index_html.replace(
        '<meta name="twitter:description" content="Live and daily public emergency incident feeds from across Louisiana on one map.">',
        f'<meta name="twitter:description" content="{escaped_description}">',
    )
    serialized_payload = json.dumps(payload, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')
    index_html = index_html.replace(
        '</head>',
        f'  <script type="application/json" id="shared-incident-data">{serialized_payload}</script>\n</head>',
        1,
    )

    response = app.response_class(index_html, mimetype='text/html')
    if _is_ui_document_navigation():
        _set_history_ui_cookie(response)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


def _client_ip_for_rate_limit() -> str:
    # Enable trusted hops only behind a proxy that overwrites incoming forwarding
    # headers, with the app port inaccessible to untrusted direct connections.
    candidate = request.remote_addr or 'unknown'
    if TRUSTED_PROXY_HOPS:
        forwarded = [part.strip() for part in request.headers.get('X-Forwarded-For', '').split(',') if part.strip()]
        if len(forwarded) >= TRUSTED_PROXY_HOPS:
            candidate = forwarded[-TRUSTED_PROXY_HOPS]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.remote_addr or 'unknown'


def _report_rate_bucket(path: str) -> tuple[str, int] | None:
    clean_path = path.rstrip('/') or '/'
    if clean_path == '/api/incidents/active':
        return 'incident-active-api', INCIDENT_ACTIVE_API_RATE_LIMIT
    if clean_path == '/api/incidents/history':
        return 'incident-history-api', INCIDENT_HISTORY_REQUEST_RATE_LIMIT
    if clean_path == '/api/incidents/history_counts':
        return 'incident-history-counts-api', INCIDENT_OTHER_API_RATE_LIMIT
    if clean_path == '/api/reports/geocode':
        return 'report-geocode-api', REPORT_GEOCODE_RATE_LIMIT
    if clean_path.startswith('/api/reports/'):
        return 'report-api', REPORT_API_RATE_LIMIT
    if clean_path.startswith('/api/'):
        return 'other-api', INCIDENT_OTHER_API_RATE_LIMIT
    if clean_path == '/reports' or clean_path.startswith('/reports/'):
        return 'report-page', REPORT_PAGE_RATE_LIMIT
    return None


def _rate_limited_response(retry_after_seconds: int):
    resp = jsonify({
        'error': 'rate_limited',
        'message': 'Too many requests. Please wait a moment and try again.',
        'retryAfterSeconds': retry_after_seconds,
    })
    resp.status_code = 429
    resp.headers['Retry-After'] = str(max(1, retry_after_seconds))
    return resp


def _persistent_history_date_limit(client_ip, date):
    """Keep hourly/day quotas across workers and restarts in a separate small DB."""
    now = time.time()
    client_key = hmac.new(HISTORY_UI_SECRET, client_ip.encode(), hashlib.sha256).hexdigest()
    conn = None
    try:
        conn = sqlite3.connect(ACCESS_LIMIT_DB, timeout=2)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS history_access (client_key TEXT, date TEXT, first_seen REAL, PRIMARY KEY(client_key,date))')
        conn.execute('CREATE INDEX IF NOT EXISTS history_access_expiry ON history_access(first_seen)')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM history_access WHERE first_seen <= ?', (now - 86400,))
        visits = dict(conn.execute('SELECT date,first_seen FROM history_access WHERE client_key=?', (client_key,)))
        for seconds, maximum in ((3600, HISTORY_HOURLY_DATE_LIMIT), (86400, HISTORY_DAILY_DATE_LIMIT)):
            within = {day: stamp for day, stamp in visits.items() if stamp > now - seconds}
            if maximum > 0 and date not in within and len(within) >= maximum:
                return _rate_limited_response(max(1, math.ceil(seconds - (now - min(within.values())))))
        conn.execute('INSERT OR IGNORE INTO history_access VALUES (?,?,?)', (client_key, date, now))
        conn.commit()
        return None
    except sqlite3.Error:
        # Losing the quota store must not silently turn history into an unlimited feed.
        return _rate_limited_response(60)
    finally:
        if conn is not None:
            conn.close()


def _check_report_rate_limit():
    if REPORT_RATE_WINDOW_SECONDS <= 0:
        return None

    clean_path = request.path.rstrip('/') or '/'
    if clean_path == '/api/incidents/history':
        date = (request.args.get('date') or '').strip()
        bounds = _central_date_bounds_utc(date)
        if bounds:
            date = _central_date_key(bounds[0])
            now = time.monotonic()
            client_ip = _client_ip_for_rate_limit()
            windows = [(REPORT_RATE_WINDOW_SECONDS, INCIDENT_HISTORY_API_RATE_LIMIT)]
            if ACCESS_LIMIT_DB:
                quota_response = _persistent_history_date_limit(client_ip, date)
                if quota_response is not None:
                    return quota_response
            else:
                windows.extend(((3600, HISTORY_HOURLY_DATE_LIMIT), (86400, HISTORY_DAILY_DATE_LIMIT)))
            with _report_rate_lock:
                pending = {}
                for seconds, maximum in windows:
                    if maximum <= 0:
                        continue
                    key = (client_ip, seconds)
                    dates = {day: stamp for day, stamp in _history_date_rate_hits.get(key, {}).items()
                             if stamp > now - seconds}
                    if date not in dates and len(dates) >= maximum:
                        wait = max(1, math.ceil(seconds - (now - min(dates.values()))))
                        return _rate_limited_response(wait)
                    dates.setdefault(date, now)
                    pending[key] = dates
                _history_date_rate_hits.update(pending)
                if len(_history_date_rate_hits) > 2000:
                    stale = [key for key, dates in _history_date_rate_hits.items()
                             if not any(stamp > now - key[1] for stamp in dates.values())]
                    for key in stale:
                        _history_date_rate_hits.pop(key, None)
        # Repeated pages still consume the request budget, even on the same date.

    bucket = _report_rate_bucket(request.path)
    if bucket is None:
        return None

    scope, limit = bucket
    if limit <= 0:
        return None

    now = time.monotonic()
    cutoff = now - REPORT_RATE_WINDOW_SECONDS
    key = (_client_ip_for_rate_limit(), scope)

    with _report_rate_lock:
        hits = [stamp for stamp in _report_rate_hits.get(key, []) if stamp > cutoff]
        if len(hits) >= limit:
            oldest = min(hits) if hits else now
            retry_after = max(1, int(math.ceil(REPORT_RATE_WINDOW_SECONDS - (now - oldest))))
            _report_rate_hits[key] = hits
            return _rate_limited_response(retry_after)

        hits.append(now)
        _report_rate_hits[key] = hits

        # Opportunistic cleanup so the dict does not grow forever.
        if len(_report_rate_hits) > 2000:
            stale_keys = [
                stored_key for stored_key, stored_hits in _report_rate_hits.items()
                if not any(stamp > cutoff for stamp in stored_hits)
            ]
            for stored_key in stale_keys:
                _report_rate_hits.pop(stored_key, None)

    return None


def _api_request_guard():
    if not request.path.startswith('/api/'):
        return None

    fetch_site = request.headers.get('Sec-Fetch-Site', '').strip().lower()
    fetch_mode = request.headers.get('Sec-Fetch-Mode', '').strip().lower()
    if fetch_site == 'cross-site':
        return jsonify({'error': 'forbidden'}), 403
    if fetch_site != 'same-origin' or fetch_mode == 'navigate':
        return jsonify({'error': 'not_found'}), 404

    origin = request.headers.get('Origin', '').strip()
    if origin:
        try:
            origin_host = (urlsplit(origin).hostname or '').lower()
        except ValueError:
            origin_host = ''
        forwarded_host = _first_forwarded_header_value(
            request.headers.get('X-Forwarded-Host', '')
        )
        request_host = (forwarded_host or request.host).split(':', 1)[0].lower()
        if not origin_host or origin_host != request_host:
            return jsonify({'error': 'forbidden'}), 403
    return None


@app.before_request
def _auth_middleware():
    # Allow container health checks without auth
    if request.path == '/healthz':
        return None
    api_guard = _api_request_guard()
    if api_guard is not None:
        return api_guard
    if not _check_auth():
        return _unauthorized()
    if (request.path.rstrip('/') or '/') in {
        '/api/incidents/history',
        '/api/incidents/history_counts',
    }:
        history_guard = _history_ui_request_guard()
        if history_guard is not None:
            return history_guard
    limited = _check_report_rate_limit()
    if limited is not None:
        return limited
    return None

@app.after_request
def _security_headers(resp):
    # Minimal hardening headers (avoid breaking external map tiles/scripts)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    # Preserve only the site origin on cross-origin requests. OSM's browser tile
    # policy requires a valid Referer while this avoids exposing URL paths.
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy', 'geolocation=(self), microphone=(), camera=()')

    # The HTML and APIs stay fresh, while explicitly versioned shell assets can
    # be reused without a validation round trip. Every deploy changes the
    # version query string before these files change.
    versioned_shell_assets = {
        '/analytics.js',
        '/styles.css',
        '/service-worker.js',
        '/manifest.webmanifest',
    }
    if request.path in versioned_shell_assets and request.args.get('v'):
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store, private'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return resp

# Geocoder setup with caching - try ArcGIS first (better US coverage), fallback to Nominatim
from geopy.geocoders import ArcGIS
geolocator_arcgis = ArcGIS(timeout=5)
geolocator_osm = Nominatim(user_agent=SCRAPER_USER_AGENT, timeout=5)
geocode_cache = {}
geocode_intersection_cache = {}

# Increment whenever stored coordinates need to be reconsidered because the
# validation/ranking algorithm changed. Version 4 removed CAD-only road
# discriminators; version 5 added validated NOPD block/intersection fallbacks;
# version 6 adds Baton Rouge address-range and corridor normalization plus
# official City-Parish traffic-map point enrichment.
# Version 7 recognizes Cove/Cv when validating provider road names.
GEOCODER_VERSION = 7
ARCGIS_INTERSECTION_MIN_SCORE = 90.0
ARCGIS_STREET_MIN_SCORE = 85.0
ARCGIS_ADDRESS_MIN_SCORE = 90.0
MAX_STREET_SEGMENT_METERS = 8000.0

ROAD_TYPE_TOKENS = {
    "ALY", "ALLEY", "AV", "AVE", "AVENUE", "BLVD", "BOULEVARD",
    "CIR", "CIRCLE", "CV", "COVE", "CT", "COURT", "DR", "DRIVE", "EXPY", "EXPRESSWAY",
    "FWY", "FREEWAY", "HWY", "HIGHWAY", "LN", "LANE", "LOOP", "PASSWAY",
    "PKWY", "PARKWAY", "PL", "PLACE", "RD", "ROAD", "ST", "STREET",
    "TER", "TERRACE", "TRL", "TRAIL", "WAY",
}
DIRECTION_ALIASES = {
    "N": "N", "NORTH": "N",
    "S": "S", "SOUTH": "S",
    "E": "E", "EAST": "E",
    "W": "W", "WEST": "W",
    "NE": "NE", "NORTHEAST": "NE",
    "NW": "NW", "NORTHWEST": "NW",
    "SE": "SE", "SOUTHEAST": "SE",
    "SW": "SW", "SOUTHWEST": "SW",
}

# 2020 Census parish outline for Caddo. Using the real shape keeps west Bossier
# hits from slipping through the old coarse longitude cutoff.
CADDO_PARISH_RING_LON_LAT = [
    (-94.043147, 32.693030),
    (-94.043147, 32.693031),
    (-94.042947, 32.767991),
    (-94.043027, 32.776863),
    (-94.042938, 32.780558),
    (-94.042829, 32.785277),
    (-94.042747, 32.786973),
    (-94.043026, 32.797476),
    (-94.042785, 32.871486),
    (-94.043025, 32.880446),
    (-94.042886, 32.880965),
    (-94.042886, 32.881089),
    (-94.042859, 32.892771),
    (-94.042885, 32.898911),
    (-94.043092, 32.910021),
    (-94.043067, 32.937903),
    (-94.043088, 32.955592),
    (-94.042964, 33.019219),
    (-94.041444, 33.019188),
    (-94.035839, 33.019145),
    (-94.027983, 33.019139),
    (-94.024475, 33.019207),
    (-93.814553, 33.019372),
    (-93.842597, 32.946764),
    (-93.785181, 32.857353),
    (-93.824253, 32.792451),
    (-93.783233, 32.784360),
    (-93.819169, 32.736002),
    (-93.782111, 32.712212),
    (-93.739474, 32.590773),
    (-93.767444, 32.538401),
    (-93.699506, 32.497480),
    (-93.661396, 32.427624),
    (-93.685569, 32.395498),
    (-93.659041, 32.406058),
    (-93.471249, 32.237186),
    (-93.614690, 32.237526),
    (-93.666472, 32.317444),
    (-93.791282, 32.340224),
    (-93.951085, 32.195545),
    (-94.042621, 32.196005),
    (-94.042662, 32.218146),
    (-94.042732, 32.269620),
    (-94.042733, 32.269696),
    (-94.042739, 32.363559),
    (-94.042763, 32.373332),
    (-94.042901, 32.392283),
    (-94.042923, 32.399918),
    (-94.042899, 32.400659),
    (-94.042986, 32.435507),
    (-94.042908, 32.439891),
    (-94.042903, 32.470386),
    (-94.042875, 32.471348),
    (-94.042902, 32.472906),
    (-94.042995, 32.478004),
    (-94.042955, 32.480261),
    (-94.043072, 32.484300),
    (-94.043089, 32.486561),
    (-94.042911, 32.492852),
    (-94.042885, 32.505145),
    (-94.043081, 32.513613),
    (-94.043142, 32.559502),
    (-94.043083, 32.564261),
    (-94.042919, 32.610142),
    (-94.042929, 32.618260),
    (-94.042926, 32.622015),
    (-94.042824, 32.640305),
    (-94.042780, 32.643466),
    (-94.042913, 32.655127),
    (-94.043147, 32.693030),
]

GENERIC_CROSS_TOKENS = {
    "DEAD END",
    "DEADEND",
    "EXIT",
    "EXIT INTERCHANGE ROADWAYS",
    "INTERCHANGE ROADWAYS",
    "UNKNOWN",
    "UNKNOWN NAME",
}

CAD_ROAD_DISAMBIGUATOR_TYPES = {
    "ALY", "ALLEY", "AV", "AVE", "AVENUE", "BLVD", "BOULEVARD",
    "CIR", "CIRCLE", "CV", "COVE", "CT", "COURT", "DR", "DRIVE", "LN", "LANE",
    "LOOP", "PASSWAY", "PKWY", "PARKWAY", "PL", "PLACE", "RD", "ROAD",
    "ST", "STREET", "TER", "TERRACE", "TRL", "TRAIL", "WAY",
}

SOURCE_MUNICIPALITY_ALIASES = {
    "caddo": {
        "BLN": "Blanchard",
        "CADD": "",
        "GIL": "Gilliam",
        "GWD": "Greenwood",
        "HOS": "Hosston",
        "MPT": "Mooringsport",
        "SHV": "Shreveport",
        "VIV": "Vivian",
    },
}

# Source geocode profiles (bounds + fallback center).
SOURCE_GEO_PROFILES = {
    "lakecharles": {
        "lat_min": 30.0, "lat_max": 30.45,
        "lon_min": -93.4, "lon_max": -92.95,
        "center_lat": 30.2266, "center_lon": -93.2174,
        "default_city": "Lake Charles", "county": "Calcasieu Parish",
        # Census QuickFacts 2020 land area; not a claim of complete CAD coverage.
        "area_sq_miles": 45.65,
    },
    "caddo": {
        "lat_min": 32.10,
        "lat_max": 33.05,
        "lon_min": -94.10,
        "lon_max": -93.62,
        "center_lat": 32.5252,
        "center_lon": -93.7502,
        "default_city": "Shreveport",
        "county": "Caddo Parish",
        "area_sq_miles": 937.0,
        "polygon": CADDO_PARISH_RING_LON_LAT,
    },
    "lafayette": {
        "lat_min": 29.70,
        "lat_max": 30.55,
        "lon_min": -92.35,
        "lon_max": -91.70,
        "center_lat": 30.2241,
        "center_lon": -92.0198,
        "default_city": "Lafayette",
        "county": "Lafayette Parish",
        "area_sq_miles": 270.0,
    },
    "batonrouge": {
        "lat_min": 30.20,
        "lat_max": 30.75,
        "lon_min": -91.45,
        "lon_max": -90.95,
        "center_lat": 30.4515,
        "center_lon": -91.1871,
        "default_city": "Baton Rouge",
        "county": "East Baton Rouge Parish",
        "area_sq_miles": 470.0,
    },
    "neworleans": {
        "lat_min": 29.80,
        "lat_max": 30.20,
        "lon_min": -90.35,
        "lon_max": -89.60,
        "center_lat": 29.9511,
        "center_lon": -90.0715,
        "default_city": "New Orleans",
        "county": "Orleans Parish",
        "area_sq_miles": 350.0,
    },
}


def _normalize_source_name(source: str | None) -> str:
    s = (source or "caddo").strip().lower()
    return s if s in SOURCE_GEO_PROFILES else "caddo"


def _source_geo_profile(source: str | None) -> dict:
    return SOURCE_GEO_PROFILES[_normalize_source_name(source)]


def _point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    count = len(ring)
    if count < 3:
        return False

    for idx in range(count):
        x1, y1 = ring[idx]
        x2, y2 = ring[(idx + 1) % count]
        crosses_lat = (y1 > lat) != (y2 > lat)
        if not crosses_lat:
            continue
        x_at_lat = (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1
        if lon < x_at_lat:
            inside = not inside
    return inside


def _is_in_source_bounds(lat: float, lon: float, source: str | None) -> bool:
    profile = _source_geo_profile(source)
    if not (
        profile["lat_min"] < lat < profile["lat_max"]
        and profile["lon_min"] < lon < profile["lon_max"]
    ):
        return False

    polygon = profile.get("polygon")
    if polygon:
        return _point_in_ring(lon, lat, polygon)
    return True


def _arcgis_in_source_scope(location, source: str | None) -> bool:
    """Validate a candidate using bounds plus ArcGIS parish metadata."""
    source_name = _normalize_source_name(source)
    profile = _source_geo_profile(source_name)
    try:
        lat = float(location.latitude)
        lon = float(location.longitude)
    except (TypeError, ValueError, AttributeError):
        return False

    if not (
        profile["lat_min"] < lat < profile["lat_max"]
        and profile["lon_min"] < lon < profile["lon_max"]
    ):
        return False

    if source_name != "caddo" or _is_in_source_bounds(lat, lon, source_name):
        return True

    # The detailed Census ring is intentionally strict around the river, but
    # ArcGIS can authoritatively identify a near-edge result as Caddo Parish.
    attrs = _arcgis_attributes(location)
    subregion = _clean_ws(attrs.get("Subregion") or "").lower()
    return subregion == "caddo parish"

def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if not it:
            continue
        key = it.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _strip_cad_road_discriminator(value: str | None) -> str:
    """Remove CAD duplicate-road suffixes without touching route numbers."""
    text = _clean_ws(value or "")
    tokens = text.split()
    if (
        len(tokens) >= 3
        and tokens[-1].isdigit()
        and tokens[-2].upper() in CAD_ROAD_DISAMBIGUATOR_TYPES
    ):
        return " ".join(tokens[:-1])
    return text

def _normalize_location_token(value: str | None) -> str:
    return _clean_ws(value or "").lower()


def _is_generic_cross_token(value: str | None) -> bool:
    token = re.sub(r"[^A-Z0-9]+", " ", _clean_ws(value or "").upper()).strip()
    return token in GENERIC_CROSS_TOKENS


def _is_unknown_location(street: str | None, cross_streets: str | None) -> bool:
    street_norm = _normalize_location_token(street)
    cross_norm = _normalize_location_token(cross_streets)
    return (street_norm in ("", "unknown")) and (cross_norm in ("", "unknown"))

def _split_cross_tokens(text: str | None) -> list[str]:
    """
    Split a "cross streets" field into individual street tokens.
    Handles separators like '&', '/', ' and ', and '@'.
    """
    if not text:
        return []
    s = _clean_ws(text)
    if not s:
        return []

    # Normalize separators to '&'
    s = s.replace("/", " & ").replace("@", " & ")
    s = re.sub(r"\s+\band\b\s+", " & ", s, flags=re.IGNORECASE)
    s = _clean_ws(s)

    parts = [p.strip(" ,") for p in s.split("&")]
    out: list[str] = []
    for p in parts:
        p = _strip_cad_road_discriminator(p)
        if not p:
            continue
        if _is_generic_cross_token(p):
            continue
        out.append(p)
    return _dedupe_keep_order(out)

def _extract_street_and_crosses(street: str | None, cross_streets: str | None) -> tuple[str | None, list[str]]:
    """
    The feed sometimes puts multiple streets in the "street" field, e.g.:
      "E 70TH @ DIXIE GARDEN DR & E DIXIE MEADOW RD"
    In that case, treat the part before '@' as the main street, and fold the rest into crosses.
    """
    street_s = _strip_cad_road_discriminator(street)
    cross_s = _clean_ws(cross_streets or "")

    extra_cross = ""
    if street_s and "@" in street_s:
        main, extra = street_s.split("@", 1)
        street_s = _clean_ws(main)
        extra_cross = _clean_ws(extra)

    crosses = _split_cross_tokens(extra_cross) + _split_cross_tokens(cross_s)
    street_clean = street_s if street_s else None
    return street_clean, crosses

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _normalize_geocode_municipality(municipality: str | None, source: str | None) -> str:
    source_name = _normalize_source_name(source)
    city = _clean_ws(municipality or "")
    if not city:
        return ""

    aliases = SOURCE_MUNICIPALITY_ALIASES.get(source_name, {})
    mapped = aliases.get(city.upper())
    if mapped is not None:
        return mapped

    if re.fullmatch(r"[A-Z .'-]+", city) and len(city) > 4:
        return city.title()
    return city


def _locality_variants_for_geocoder(municipality: str | None, source: str | None) -> list[tuple[str, ...]]:
    profile = _source_geo_profile(source)
    city = _normalize_geocode_municipality(municipality, source)
    county = _clean_ws(profile.get("county") or "")
    state = "LA"
    fallback_city = _clean_ws(profile.get("default_city") or "")

    variants: list[tuple[str, ...]] = []

    def add_variant(*parts: str) -> None:
        cleaned = tuple(part for part in (_clean_ws(p) for p in parts) if part)
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

    if city and county:
        add_variant(city, county, state)
    if city:
        add_variant(city, state)
    elif county:
        add_variant(county, state)

    if fallback_city and fallback_city != city:
        if county:
            add_variant(fallback_city, county, state)
        add_variant(fallback_city, state)

    if not variants:
        add_variant(fallback_city, state)

    return variants


def _road_signature(value: str | None) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return comparable road-name words and explicit direction markers."""
    raw_tokens = re.findall(r"[A-Z0-9]+", _clean_ws(value or "").upper())
    name_tokens: list[str] = []
    directions: set[str] = set()

    for idx, token in enumerate(raw_tokens):
        # In names such as "St Vincent Ave", the leading ST means Saint, not
        # Street. Keeping that distinction avoids reducing the road to a single
        # overly-broad word.
        if token == "ST" and idx == 0 and len(raw_tokens) > 1:
            name_tokens.append("SAINT")
            continue

        direction = DIRECTION_ALIASES.get(token)
        if direction and (idx == 0 or idx == len(raw_tokens) - 1):
            directions.add(direction)
            continue

        if token in ROAD_TYPE_TOKENS:
            continue

        if token == "I" and idx + 1 < len(raw_tokens) and raw_tokens[idx + 1].isdigit():
            token = "INTERSTATE"
        name_tokens.append(token)

    return tuple(name_tokens), frozenset(directions)


def _road_name_matches(requested: str | None, returned: str | None) -> bool:
    """Conservatively compare a requested road with a provider road name."""
    requested_words, requested_dirs = _road_signature(requested)
    returned_words, returned_dirs = _road_signature(returned)
    if not requested_words or not returned_words:
        return False

    if requested_dirs and returned_dirs and requested_dirs.isdisjoint(returned_dirs):
        return False

    if requested_words == returned_words:
        return True

    requested_set = set(requested_words)
    returned_set = set(returned_words)
    overlap = len(requested_set & returned_set)
    union = len(requested_set | returned_set)
    if overlap >= 2 and union and overlap / union >= 0.8:
        return True

    requested_text = " ".join(requested_words)
    returned_text = " ".join(returned_words)
    return min(len(requested_text), len(returned_text)) >= 6 and SequenceMatcher(
        None, requested_text, returned_text
    ).ratio() >= 0.9


def _arcgis_attributes(location) -> dict:
    raw = getattr(location, "raw", None) or {}
    attrs = raw.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _arcgis_score(location) -> float:
    raw = getattr(location, "raw", None) or {}
    attrs = _arcgis_attributes(location)
    value = attrs.get("Score", raw.get("score", 0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _arcgis_intersection_roads(location) -> list[str]:
    attrs = _arcgis_attributes(location)
    roads: list[str] = []
    for suffix in ("1", "2"):
        parts = [
            attrs.get(f"StPreDir{suffix}"),
            attrs.get(f"StPreType{suffix}"),
            attrs.get(f"StName{suffix}"),
            attrs.get(f"StType{suffix}"),
            attrs.get(f"StDir{suffix}"),
        ]
        road = _clean_ws(" ".join(str(part) for part in parts if part))
        if road:
            roads.append(road)

    if len(roads) >= 2:
        return roads[:2]

    match_address = _clean_ws(
        attrs.get("Match_addr")
        or getattr(location, "address", "")
        or (getattr(location, "raw", None) or {}).get("address", "")
    )
    street_part = match_address.split(",", 1)[0]
    split_roads = [
        _clean_ws(part)
        for part in re.split(r"\s+(?:&|AND|AT)\s+", street_part, flags=re.IGNORECASE)
        if _clean_ws(part)
    ]
    return split_roads[:2]


def _arcgis_single_road(location) -> str:
    attrs = _arcgis_attributes(location)
    road = _clean_ws(" ".join(str(part) for part in (
        attrs.get("StPreDir"),
        attrs.get("StPreType"),
        attrs.get("StName"),
        attrs.get("StType"),
        attrs.get("StDir"),
    ) if part))
    if road:
        return road

    match_address = _clean_ws(
        attrs.get("Match_addr")
        or getattr(location, "address", "")
        or (getattr(location, "raw", None) or {}).get("address", "")
    )
    return match_address.split(",", 1)[0]


def _arcgis_matches_intersection(location, road_a: str, road_b: str) -> bool:
    attrs = _arcgis_attributes(location)
    address_type = _clean_ws(attrs.get("Addr_type") or "").lower()
    if address_type not in {"streetint", "intersection"}:
        return False
    if _arcgis_score(location) < ARCGIS_INTERSECTION_MIN_SCORE:
        return False

    returned_roads = _arcgis_intersection_roads(location)
    if len(returned_roads) < 2:
        return False
    first, second = returned_roads[:2]
    return (
        _road_name_matches(road_a, first) and _road_name_matches(road_b, second)
    ) or (
        _road_name_matches(road_a, second) and _road_name_matches(road_b, first)
    )


def _arcgis_matches_single_road(location, requested_road: str) -> bool:
    attrs = _arcgis_attributes(location)
    address_type = _clean_ws(attrs.get("Addr_type") or "").lower()
    if address_type and address_type not in {
        "streetname", "streetaddress", "pointaddress", "subaddress",
    }:
        return False
    if _arcgis_score(location) < ARCGIS_STREET_MIN_SCORE:
        return False
    return _road_name_matches(requested_road, _arcgis_single_road(location))


def _split_numbered_address(value: str | None) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*(\d+[A-Z]?(?:-\d+)?)\s+(.+?)\s*$",
        _clean_ws(value or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).upper(), match.group(2)


def _split_numbered_address_range(value: str | None) -> tuple[str, str, str] | None:
    """Split a CAD block range such as ``6601 - 6799 KLEINPETER RD``."""
    match = re.match(
        r"^\s*(\d+[A-Z]?)\s*-\s*(\d+[A-Z]?)\s+(.+?)\s*$",
        _clean_ws(value or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).upper(), match.group(2).upper(), _clean_ws(match.group(3))


BATON_ROUGE_CROSS_STREET_ALIASES = {
    # EBR CAD uses this corridor label for the Drusilla/Jefferson end of the
    # I-12 segment. Drusilla Ln is the road that actually crosses I-12.
    "DRUSILLA-JEFFERSON": "DRUSILLA LN",
}


def _normalize_baton_rouge_geocode_parts(
    street: str | None,
    crosses: list[str],
) -> tuple[str | None, list[str]]:
    """Remove EBR CAD route references and expand known corridor labels."""
    street_clean = _clean_ws(street or "")
    if street_clean:
        street_clean = re.sub(
            r"^\d+\s+(?=(?:[NSEW]\s+)?(?:INTERSTATE\s+\d+|I\s*-?\s*\d+)\b)",
            "",
            street_clean,
            flags=re.IGNORECASE,
        )

    normalized_crosses = [
        BATON_ROUGE_CROSS_STREET_ALIASES.get(_clean_ws(cross).upper(), cross)
        for cross in crosses
    ]
    return street_clean or None, normalized_crosses


def _arcgis_matches_numbered_address(location, requested_address: str) -> bool:
    requested = _split_numbered_address(requested_address)
    if not requested:
        return False

    attrs = _arcgis_attributes(location)
    address_type = _clean_ws(attrs.get("Addr_type") or "").lower()
    if address_type not in {"pointaddress", "streetaddress", "subaddress"}:
        return False
    if _arcgis_score(location) < ARCGIS_ADDRESS_MIN_SCORE:
        return False

    requested_number, requested_road = requested
    matched_number = _clean_ws(attrs.get("AddNum") or "").upper()
    if not matched_number:
        match_address = _clean_ws(
            attrs.get("Match_addr")
            or getattr(location, "address", "")
            or (getattr(location, "raw", None) or {}).get("address", "")
        )
        matched = _split_numbered_address(match_address.split(",", 1)[0])
        matched_number = matched[0] if matched else ""

    matched_road = _arcgis_single_road(location)
    matched_address_parts = _split_numbered_address(matched_road)
    if matched_address_parts:
        matched_road = matched_address_parts[1]

    return matched_number == requested_number and _road_name_matches(requested_road, matched_road)


def _as_location_list(value) -> list:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _find_arcgis_intersection(
    road_a: str,
    road_b: str,
    locality_variants: list[tuple[str, ...]],
    source_name: str,
    attempt_state: dict | None = None,
) -> dict | None:
    cache_locality = locality_variants[0] if locality_variants else ()
    road_key = tuple(sorted((_normalize_location_token(road_a), _normalize_location_token(road_b))))
    cache_key = (source_name, road_key, cache_locality)
    cached = geocode_intersection_cache.get(cache_key)
    if cached:
        return dict(cached)

    for locality_parts in locality_variants:
        query = ", ".join((f"{road_a} & {road_b}", *locality_parts))
        try:
            locations = _as_location_list(geolocator_arcgis.geocode(
                query,
                exactly_one=False,
                timeout=3,
                out_fields="*",
            ))
            if attempt_state is not None:
                attempt_state["provider_responded"] = True
        except Exception:
            continue

        matches = []
        for location in locations[:10]:
            try:
                if not _arcgis_in_source_scope(location, source_name):
                    continue
                if not _arcgis_matches_intersection(location, road_a, road_b):
                    continue
                matches.append(location)
            except (TypeError, ValueError, AttributeError):
                continue

        if matches:
            location = max(matches, key=_arcgis_score)
            result = {
                "lat": float(location.latitude),
                "lng": float(location.longitude),
                "source": "arcgis",
                "query": query,
                "score": _arcgis_score(location),
            }
            geocode_intersection_cache[cache_key] = dict(result)
            return result
    return None


def _find_arcgis_street(
    road: str,
    quality: str,
    locality_variants: list[tuple[str, ...]],
    source_name: str,
    attempt_state: dict | None = None,
) -> dict | None:
    for locality_parts in locality_variants:
        query = ", ".join((road, *locality_parts))
        try:
            locations = _as_location_list(geolocator_arcgis.geocode(
                query,
                exactly_one=False,
                timeout=3,
                out_fields="*",
            ))
            if attempt_state is not None:
                attempt_state["provider_responded"] = True
        except Exception:
            continue

        matches = []
        for location in locations[:10]:
            try:
                if not _arcgis_in_source_scope(location, source_name):
                    continue
                if not _arcgis_matches_single_road(location, road):
                    continue
                matches.append(location)
            except (TypeError, ValueError, AttributeError):
                continue

        if matches:
            location = max(matches, key=_arcgis_score)
            return {
                "lat": float(location.latitude),
                "lng": float(location.longitude),
                "source": "arcgis",
                "quality": quality,
                "query": query,
            }
    return None


def _find_arcgis_address(
    address: str,
    locality_variants: list[tuple[str, ...]],
    source_name: str,
    attempt_state: dict | None = None,
) -> dict | None:
    for locality_parts in locality_variants:
        query = ", ".join((address, *locality_parts))
        try:
            locations = _as_location_list(geolocator_arcgis.geocode(
                query,
                exactly_one=False,
                timeout=3,
                out_fields="*",
            ))
            if attempt_state is not None:
                attempt_state["provider_responded"] = True
        except Exception:
            continue

        matches = []
        for location in locations[:10]:
            try:
                if not _arcgis_in_source_scope(location, source_name):
                    continue
                if not _arcgis_matches_numbered_address(location, address):
                    continue
                matches.append(location)
            except (TypeError, ValueError, AttributeError):
                continue

        if matches:
            location = max(matches, key=_arcgis_score)
            return {
                "lat": float(location.latitude),
                "lng": float(location.longitude),
                "source": "arcgis",
                "quality": "address",
                "query": query,
            }
    return None


def _osm_matches_road(location, requested_road: str) -> bool:
    raw = getattr(location, "raw", None) or {}
    address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    returned_road = _clean_ws(
        address.get("road")
        or address.get("pedestrian")
        or address.get("residential")
        or (getattr(location, "address", "") or "").split(",", 1)[0]
    )
    return _road_name_matches(requested_road, returned_road)

def geocode_address(street, cross_streets, municipality, source: str = 'caddo'):
    """
    Convert address to lat/lng coordinates.
    Validate exact intersections and understand two-cross-street segments.

    In the Caddo feed, a value such as:
      E BERT KOUNS INDUSTRIAL @ JUMP RUN & YOUREE DR
    means a location on E Bert Kouns bracketed by those two roads. It does not
    mean that Jump Run intersects Youree. When both named-street intersections
    validate, use their midpoint as the best estimate of the segment.
    """
    source_name = _normalize_source_name(source)
    geo_profile = _source_geo_profile(source_name)

    # Skip geocoding entirely if the location is missing/unknown.
    if _is_unknown_location(street, cross_streets):
        log("  [--] skipped | location is empty/unknown")
        return {
            'lat': None,
            'lng': None,
            'source': 'skipped',
            'quality': 'unknown-location',
            'query': None,
        }
    
    locality_variants = _locality_variants_for_geocoder(municipality, source_name)
    cache_locality = locality_variants[0][0] if locality_variants else geo_profile['default_city']

    street_clean, crosses = _extract_street_and_crosses(street, cross_streets)
    address_range = _split_numbered_address_range(street_clean)
    if source_name == 'batonrouge' and not address_range:
        street_clean, crosses = _normalize_baton_rouge_geocode_parts(
            street_clean,
            crosses,
        )
    cross1 = crosses[0] if len(crosses) > 0 else None
    cross2 = crosses[1] if len(crosses) > 1 else None

    # Cache key
    cache_key = f"{source_name}|{street_clean or ''}|{cross1 or ''}|{cross2 or ''}|{cache_locality}"
    if cache_key in geocode_cache:
        return geocode_cache[cache_key]

    attempt_state = {"provider_responded": False}

    # EBR commonly publishes a block/address range bracketed by two streets.
    # Validate both numbered endpoints and use their midpoint; if either end
    # fails, continue below using the named road and the published crosses.
    if source_name == 'batonrouge' and address_range:
        range_start, range_end, range_road = address_range
        start_address = f"{range_start} {range_road}"
        end_address = f"{range_end} {range_road}"
        start_match = _find_arcgis_address(
            start_address,
            locality_variants,
            source_name,
            attempt_state,
        )
        end_match = _find_arcgis_address(
            end_address,
            locality_variants,
            source_name,
            attempt_state,
        )
        if start_match and end_match:
            range_length = _haversine_m(
                start_match['lat'], start_match['lng'],
                end_match['lat'], end_match['lng'],
            )
            if range_length <= MAX_STREET_SEGMENT_METERS:
                result = {
                    'lat': (start_match['lat'] + end_match['lat']) / 2.0,
                    'lng': (start_match['lng'] + end_match['lng']) / 2.0,
                    'source': 'arcgis',
                    'quality': 'approximate-address-range',
                    'query': f"{start_match['query']} || {end_match['query']}",
                }
                geocode_cache[cache_key] = result
                log(
                    f"  [ARC] address-range ({range_length:.0f}m) | "
                    f"{start_address} - {range_end} -> "
                    f"({result['lat']:.5f}, {result['lng']:.5f})"
                )
                return result

        street_clean, crosses = _normalize_baton_rouge_geocode_parts(
            range_road,
            crosses,
        )
        cross1 = crosses[0] if len(crosses) > 0 else None
        cross2 = crosses[1] if len(crosses) > 1 else None

    # Lafayette and Baton Rouge commonly publish a numbered address plus a
    # nearby cross street. The address is more precise than forcing those
    # fields through intersection matching.
    if street_clean and not address_range and _split_numbered_address(street_clean):
        result = _find_arcgis_address(
            street_clean,
            locality_variants,
            source_name,
            attempt_state,
        )
        if result:
            geocode_cache[cache_key] = result
            log(f"  [ARC] address | {result['query']} -> ({result['lat']:.5f}, {result['lng']:.5f})")
            return result

    # First validate the named street against each bracketing cross street.
    # ArcGIS's Addr_type=StreetInt plus returned road components are required;
    # a high-score StreetName result is still only a fuzzy partial match.
    street_intersections: list[dict] = []
    if street_clean:
        for cross in (cross1, cross2):
            if not cross:
                continue
            match = _find_arcgis_intersection(
                street_clean,
                cross,
                locality_variants,
                source_name,
                attempt_state,
            )
            if match:
                street_intersections.append(match)

    if len(street_intersections) >= 2:
        first, second = street_intersections[:2]
        segment_length = _haversine_m(
            first['lat'], first['lng'], second['lat'], second['lng']
        )
        if segment_length <= MAX_STREET_SEGMENT_METERS:
            result = {
                'lat': (first['lat'] + second['lat']) / 2.0,
                'lng': (first['lng'] + second['lng']) / 2.0,
                'source': 'arcgis',
                'quality': 'street-segment',
                'query': f"{first['query']} || {second['query']}",
            }
            geocode_cache[cache_key] = result
            log(
                f"  [ARC] street-segment ({segment_length:.0f}m) | "
                f"{street_clean} @ {cross1} & {cross2} -> "
                f"({result['lat']:.5f}, {result['lng']:.5f})"
            )
            return result

    # One verified named-street intersection is safer than a fuzzy match of the
    # two cross streets to each other.
    if street_intersections:
        result = dict(street_intersections[0])
        result['quality'] = 'street+cross'
        result.pop('score', None)
        geocode_cache[cache_key] = result
        log(f"  [ARC] street+cross | {result['query']} -> ({result['lat']:.5f}, {result['lng']:.5f})")
        return result

    # If the street field is empty or is a place/subdivision label, the two
    # cross streets may themselves be the real intersection.
    if cross1 and cross2:
        match = _find_arcgis_intersection(
            cross1,
            cross2,
            locality_variants,
            source_name,
            attempt_state,
        )
        if match:
            result = dict(match)
            result['quality'] = 'intersection-2'
            result.pop('score', None)
            geocode_cache[cache_key] = result
            log(f"  [ARC] intersection-2 | {result['query']} -> ({result['lat']:.5f}, {result['lng']:.5f})")
            return result

    # Only use a lower-confidence road centroid when the feed supplied no
    # intersection context at all. If cross streets were supplied but could
    # not be validated, leaving the point unresolved is safer than discarding
    # the most specific location evidence.
    fallback_roads: list[tuple[str, str]] = []
    if street_clean and not cross1:
        fallback_roads.append((street_clean, 'street-only'))
    elif cross1 and not street_clean and not cross2:
        fallback_roads.append((cross1, 'cross-only'))

    for road, quality in fallback_roads:
        result = _find_arcgis_street(
            road, quality, locality_variants, source_name, attempt_state
        )
        if result:
            geocode_cache[cache_key] = result
            log(f"  [ARC] {quality} | {result['query']} -> ({result['lat']:.5f}, {result['lng']:.5f})")
            return result

    for road, quality in fallback_roads:
        for locality_parts in locality_variants:
            query = ", ".join((road, *locality_parts))
            try:
                location = geolocator_osm.geocode(
                    query,
                    country_codes='us',
                    exactly_one=True,
                    addressdetails=True,
                    timeout=3,
                )
                attempt_state["provider_responded"] = True
                if not location:
                    continue
                if not _is_in_source_bounds(location.latitude, location.longitude, source_name):
                    continue
                if not _osm_matches_road(location, road):
                    continue
                result = {
                    'lat': float(location.latitude),
                    'lng': float(location.longitude),
                    'source': 'osm',
                    'quality': quality,
                    'query': query,
                }
                geocode_cache[cache_key] = result
                log(f"  [OSM] {quality} | {query} -> ({result['lat']:.5f}, {result['lng']:.5f})")
                return result
            except Exception:
                continue

    # Never fabricate a point near the city center. An unresolved incident can
    # remain visible in the list without putting a confidently wrong marker on
    # the map.
    unresolved = {
        'lat': None,
        'lng': None,
        'source': 'unresolved',
        'quality': 'unresolved',
        'query': None,
        'provider_responded': attempt_state['provider_responded'],
    }
    if attempt_state['provider_responded']:
        geocode_cache[cache_key] = unresolved
    log(f"  [--] unresolved | {street_clean or '?'} @ {cross1 or '?'} {'& ' + cross2 if cross2 else ''}")
    return unresolved

def hash_incident(incident, *, reported_at: str | None = None):
    """Generate unique hash for incident deduplication.

    Feeds without a source ID only publish a clock time, so the same call type
    at the same place and minute on another day produces the same base key.
    Pass ``reported_at`` to derive a distinct hash for that later occurrence.
    """
    source = _normalize_source_name(incident.get('source') if isinstance(incident, dict) else None)
    source_id = _clean_ws(incident.get('source_id') if isinstance(incident, dict) else '')
    if source_id:
        key = f"{source}-{source_id}"
    else:
        key = (
            f"{source}-{incident['agency']}-{incident['time']}-"
            f"{incident['description']}-{incident['street']}-{incident['cross_streets']}"
        )
        if reported_at:
            key = f"{key}-{reported_at}"
    return hashlib.md5(key.encode()).hexdigest()


def _resolve_incident_hash(cursor, incident: dict, observed_at: str) -> str:
    """Return the stored hash for this occurrence of a feed incident.

    A row with the undated base hash is reused while it is still active or when
    both describe the same reported time. A closed row from another day is a
    different call that happens to share the clock time, so it gets a dated hash
    instead of being reopened and merged into the old record.
    """
    base_hash = hash_incident(incident)
    if _clean_ws(incident.get('source_id') or ''):
        return base_hash
    try:
        existing = cursor.execute(
            'SELECT is_active, first_seen, time FROM incidents WHERE hash = ?',
            (base_hash,),
        ).fetchone()
    except sqlite3.OperationalError:
        return base_hash
    if existing is None or existing[0]:
        return base_hash
    source = incident.get('source')
    # A dated occurrence that is still on the feed keeps its hash past midnight.
    try:
        continuing = cursor.execute(
            '''SELECT hash FROM incidents
               WHERE is_active = 1 AND hash != ? AND source IS ? AND agency IS ? AND time IS ?
                 AND description IS ? AND street IS ? AND cross_streets IS ?
               ORDER BY first_seen DESC LIMIT 1''',
            (base_hash, source, incident.get('agency'), incident.get('time'), incident.get('description'),
             incident.get('street'), incident.get('cross_streets')),
        ).fetchone()
    except sqlite3.OperationalError:
        continuing = None
    if continuing:
        return continuing[0]
    reported_at = _incident_reported_at(
        {'source': source, 'time': incident.get('time'), 'first_seen': observed_at}
    )
    existing_reported_at = _incident_reported_at(
        {'source': source, 'time': existing[2], 'first_seen': existing[1]}
    )
    if not reported_at or not existing_reported_at or reported_at == existing_reported_at:
        return base_hash
    return hash_incident(incident, reported_at=reported_at)

def scrape_caddo_incidents():
    """
    Scrape active incidents from Caddo 911 website.
    """
    try:
        return caddo_source.scrape(user_agent=SCRAPER_USER_AGENT, timeout_seconds=15)
    except Exception as e:
        log(f"Caddo scraping error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def scrape_lafayette_incidents():
    """Scrape active incidents from Lafayette traffic feed."""
    try:
        return lafayette_source.scrape(user_agent=SCRAPER_USER_AGENT, timeout_seconds=15)
    except Exception as e:
        log(f"Lafayette scraping error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def scrape_batonrouge_incidents():
    """Scrape active incidents from Baton Rouge traffic feed."""
    try:
        return batonrouge_source.scrape(user_agent=SCRAPER_USER_AGENT, timeout_seconds=15)
    except Exception as e:
        log(f"Baton Rouge scraping error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def scrape_neworleans_incidents():
    """Import the official delayed NOPD calls-for-service log."""
    try:
        return neworleans_source.scrape(user_agent=SCRAPER_USER_AGENT, timeout_seconds=30)
    except Exception as e:
        log(f"New Orleans scraping error: {e}")
        import traceback
        traceback.print_exc()
        return [], None


def scrape_lakecharles_incidents():
    """Import only the anonymous site's published, delayed LCPD closed calls."""
    try:
        return lakecharles_source.scrape(user_agent=SCRAPER_USER_AGENT, timeout_seconds=20)
    except Exception as error:
        log(f"Lake Charles import unavailable: {error}")
        return [], None


# Backwards compatibility for any old call sites.
def scrape_incidents():
    return scrape_caddo_incidents()

# Track last update time
last_update: str | None = None
scrape_interval_seconds: int = SCRAPE_INTERVAL_SECONDS_DEFAULT
source_last_scrape_monotonic: dict[str, float] = {}
SOURCE_MIN_SCRAPE_INTERVAL_SECONDS = {
    # Data.NOLA publishes this dataset in daily batches. Polling it every live
    # feed cycle would create load without making the site any fresher.
    'neworleans': 15 * 60,
    'lakecharles': 30 * 60,
}


NOLA_APPROX_LOCATION_RE = re.compile(
    r"^\s*approx(?:imate)?\s+loc(?:ation)?\s*:\s*",
    flags=re.IGNORECASE,
)
NOLA_UNUSABLE_PUBLIC_LOCATIONS = {
    "",
    "UNKNOWN",
    "REDACTED",
    "REDACTED BLOCK",
    "CONFIDENTIAL",
    "WITHHELD",
}


def _expand_public_block_number(value: str) -> str:
    """Turn a public block mask such as 035XX into a block anchor (3500)."""
    text = _clean_ws(value)
    match = re.match(r"^(\d+)(X{2,})(?=\s)", text, flags=re.IGNORECASE)
    if not match:
        return text
    block_number = int(match.group(1)) * (10 ** len(match.group(2)))
    expanded = f"{block_number}{text[match.end():]}"
    return re.sub(r"^(\d+)\s+BLK\s+", r"\1 ", expanded, flags=re.IGNORECASE)


def _normalize_new_orleans_geocode_text(value: str) -> str:
    """Normalize known public-feed notation without changing its display."""
    text = _clean_ws(value)
    text = re.sub(r"\bCHEF\s+MENTUER\b", "Chef Menteur", text, flags=re.IGNORECASE)
    text = re.sub(r"\bUS\s*(\d+)B\b", r"US \1 BUS", text, flags=re.IGNORECASE)
    text = re.sub(r"\bEARHART\s+ONRAMP\b", "Earhart Blvd", text, flags=re.IGNORECASE)
    return text


def _new_orleans_public_location_parts(incident: dict) -> tuple[str | None, str | None, bool]:
    """Prepare only NOPD's published label for approximate geocoding."""
    public_label = _clean_ws(incident.get('street') or '')
    explicitly_approximate = bool(incident.get('location_is_approximate'))
    prefix_match = NOLA_APPROX_LOCATION_RE.match(public_label)
    if prefix_match:
        explicitly_approximate = True
        public_label = public_label[prefix_match.end():].strip()

    if public_label.upper() in NOLA_UNUSABLE_PUBLIC_LOCATIONS:
        return None, None, explicitly_approximate

    public_label = _normalize_new_orleans_geocode_text(public_label)
    public_cross = _clean_ws(incident.get('cross_streets') or '')
    if not public_cross:
        # NOPD commonly stores intersections entirely in block_address.
        parts = re.split(
            r"\s*(?:&|/|@)\s*|\s+\b(?:AND|AT|AFTER|BEFORE|NEAR)\b\s+",
            public_label,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if len(parts) == 2 and all(_clean_ws(part) for part in parts):
            public_label, public_cross = (_clean_ws(part) for part in parts)

    # NOPD publishes block masks rather than exact premises. A block anchor is
    # used only to obtain an approximate map point; the stored/displayed label
    # remains the original 035XX-style public value.
    public_label = _expand_public_block_number(public_label)
    public_cross = _normalize_new_orleans_geocode_text(public_cross)
    return public_label or None, public_cross or None, explicitly_approximate


def _incident_geocode_result(incident: dict, source_name: str) -> dict:
    raw_lat = incident.get('latitude')
    raw_lon = incident.get('longitude')
    try:
        latitude = float(raw_lat)
        longitude = float(raw_lon)
    except (TypeError, ValueError):
        latitude = None
        longitude = None

    if (
        latitude is not None
        and longitude is not None
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and _is_in_source_bounds(latitude, longitude, source_name)
    ):
        is_approximate = bool(
            incident.get('location_is_approximate')
            or (
                source_name == 'neworleans'
                and NOLA_APPROX_LOCATION_RE.match(_clean_ws(incident.get('street') or ''))
            )
        )
        return {
            'lat': latitude,
            'lng': longitude,
            'source': 'source-feed',
            'quality': 'approximate-published' if is_approximate else 'published',
            'query': (
                'Official source coordinates; source labels location approximate'
                if is_approximate
                else 'Official source coordinates'
            ),
            'provider_responded': True,
        }

    if source_name == 'lakecharles':
        # Respect withheld/missing public coordinates. No private-address or
        # city-center fallback for this generalized closed-call dataset.
        return {
            'lat': None, 'lng': None, 'source': 'source-feed-unmapped',
            'quality': 'location-unavailable', 'query': None,
            'provider_responded': True,
        }

    if source_name == 'neworleans':
        public_street, public_cross, _ = _new_orleans_public_location_parts(incident)
        if not public_street and not public_cross:
            return {
                'lat': None,
                'lng': None,
                'source': 'source-feed-unmapped',
                'quality': 'location-unavailable',
                'query': None,
                'provider_responded': True,
            }

        result = geocode_address(
            public_street,
            public_cross,
            incident.get('municipality'),
            source=source_name,
        )
        if result.get('lat') is not None and result.get('lng') is not None:
            result = dict(result)
            base_quality = result.get('quality') or 'location'
            result['quality'] = f"approximate-{base_quality}"
            # Do not expose the internal block-anchor query as a more specific
            # address. The user-facing location stays exactly as NOPD supplied.
            result['query'] = 'Approximate from public NOPD block/intersection label'
        return result

    result = geocode_address(
        incident.get('street'),
        incident.get('cross_streets'),
        incident.get('municipality'),
        source=source_name,
    )
    if (
        incident.get('location_is_approximate')
        and result.get('lat') is not None
        and result.get('lng') is not None
    ):
        result = dict(result)
        base_quality = str(result.get('quality') or 'location')
        if not base_quality.startswith('approximate-'):
            result['quality'] = f"approximate-{base_quality}"
    return result

def _new_orleans_processing_priority(incident: dict) -> tuple[int, int]:
    """Put visible NOLA rows with official points ahead of slower fallbacks."""
    active_rank = 0 if incident.get('is_active', True) else 1
    try:
        has_official_point = _is_in_source_bounds(
            float(incident.get('latitude')),
            float(incident.get('longitude')),
            'neworleans',
        )
    except (TypeError, ValueError):
        has_official_point = False
    return active_rank, 0 if has_official_point else 1


def _collector_geocode_result(conn, incident, source):
    # Published coordinates are local work and can stay in the current batch.
    # Release SQLite's single writer slot before any possible provider request.
    try:
        has_source_point = _is_in_source_bounds(
            float(incident.get('latitude')), float(incident.get('longitude')), source,
        )
    except (TypeError, ValueError):
        has_source_point = False
    if not has_source_point:
        conn.commit()
    return _incident_geocode_result(incident, source)


def process_incidents(incidents, *, source: str = 'caddo', deactivate_missing: bool = True,
                      allow_empty: bool = False):
    """Store/update incidents in database"""
    global last_update

    # Only a validated empty snapshot may clear the last active calls.
    if incidents is None or (not incidents and not allow_empty):
        return

    source_default = _normalize_source_name(source)
    conn = db_connect()
    try:
        _store_incidents(conn, incidents, source_default, deactivate_missing)
    finally:
        conn.close()
    last_update = datetime.now(timezone.utc).isoformat()
    meta_set('last_update', last_update)


def _store_incidents(conn, incidents, source_default: str, deactivate_missing: bool) -> None:
    cursor = conn.cursor()
    has_source_column = _ensure_incidents_source_column(conn)
    has_peak_units = 'peak_units' in {row[1] for row in cursor.execute('PRAGMA table_info(incidents)')}
    now = datetime.now(timezone.utc).isoformat()
    current_hashes = set()
    ordered_incidents = list(incidents)
    if source_default == 'neworleans':
        # The source publishes many rows at once. Make the current official
        # points queryable immediately, before slower 0,0 location fallbacks.
        ordered_incidents.sort(key=_new_orleans_processing_priority)

    for incident_index, incident in enumerate(ordered_incidents, start=1):
        if source_default == 'neworleans' and incident_index > 1 and (incident_index - 1) % 25 == 0:
            conn.commit()
        incident_source = _normalize_source_name(incident.get('source') if isinstance(incident, dict) else source_default)
        incident['source'] = incident_source
        desired_active = 1 if incident.get('is_active', True) else 0
        occurred_at = incident.get('occurred_at')
        if not isinstance(occurred_at, str) or not _parse_iso_datetime(occurred_at):
            occurred_at = None
        h = _resolve_incident_hash(cursor, incident, occurred_at or now)
        current_hashes.add(h)

        # Check if exists
        try:
            cursor.execute(
                'SELECT id, latitude, longitude, geocode_source, geocode_quality, geocode_version, street FROM incidents WHERE hash = ?',
                (h,)
            )
            existing = cursor.fetchone()
            existing_cols = "versioned"
        except sqlite3.OperationalError:
            try:
                cursor.execute(
                    'SELECT id, latitude, longitude, geocode_source, geocode_quality, street FROM incidents WHERE hash = ?',
                    (h,)
                )
                existing = cursor.fetchone()
                existing_cols = "new"
            except sqlite3.OperationalError:
                cursor.execute('SELECT id, latitude, longitude FROM incidents WHERE hash = ?', (h,))
                existing = cursor.fetchone()
                existing_cols = "old"
        
        if existing:
            if has_peak_units:
                cursor.execute(
                    'UPDATE incidents SET peak_units = MAX(COALESCE(peak_units, 0), COALESCE(units, 0), COALESCE(?, 0)) WHERE hash = ?',
                    (incident.get('units'), h),
                )
            # Unit assignments can change while the incident remains active.
            # Refresh mutable feed fields instead of freezing their first value.
            try:
                cursor.execute(
                    '''UPDATE incidents
                       SET last_seen = ?, is_active = ?, source = ?, agency = ?, time = ?, units = ?,
                           description = ?, street = ?, cross_streets = ?, municipality = ?,
                           first_seen = COALESCE(?, first_seen)
                       WHERE hash = ?''',
                    (
                        now,
                        desired_active,
                        incident_source,
                        incident.get('agency'),
                        incident.get('time'),
                        incident.get('units'),
                        incident.get('description'),
                        incident.get('street'),
                        incident.get('cross_streets'),
                        incident.get('municipality'),
                        occurred_at,
                        h,
                    ),
                )
            except sqlite3.OperationalError:
                cursor.execute(
                    'UPDATE incidents SET last_seen = ?, is_active = ?, units = ? WHERE hash = ?',
                    (now, desired_active, incident.get('units'), h),
                )

            if incident_source == 'neworleans':
                # Re-apply NOPD's current point state on every import. If the
                # official point is 0,0, fall back to the public block or
                # intersection label and mark the result as approximate.
                existing_lat = existing[1]
                existing_lng = existing[2]
                existing_quality = existing[4] if existing_cols in ("new", "versioned") and len(existing) > 4 else None
                existing_version = existing[5] if existing_cols == "versioned" and len(existing) > 5 else None
                existing_street = (
                    existing[6]
                    if existing_cols == "versioned" and len(existing) > 6
                    else existing[5]
                    if existing_cols == "new" and len(existing) > 5
                    else None
                )
                raw_lat = incident.get('latitude')
                raw_lng = incident.get('longitude')
                try:
                    has_official_point = _is_in_source_bounds(
                        float(raw_lat), float(raw_lng), incident_source
                    )
                except (TypeError, ValueError):
                    has_official_point = False

                public_street, public_cross, _ = _new_orleans_public_location_parts(incident)
                has_public_location = bool(public_street or public_cross)
                reusable_approximation = (
                    not has_official_point
                    and has_public_location
                    and existing_lat is not None
                    and existing_lng is not None
                    and str(existing_quality or '').startswith('approximate-')
                    and existing_version == GEOCODER_VERSION
                    and _clean_ws(existing_street or '') == _clean_ws(incident.get('street') or '')
                )
                if reusable_approximation:
                    continue

                current_geo = _collector_geocode_result(conn, incident, incident_source)
                has_current_coords = (
                    current_geo.get('lat') is not None
                    and current_geo.get('lng') is not None
                )
                provider_confirmed = has_current_coords or bool(current_geo.get('provider_responded'))
                if provider_confirmed:
                    cursor.execute(
                        '''UPDATE incidents
                           SET latitude = ?, longitude = ?, geocode_source = ?,
                               geocode_quality = ?, geocode_query = ?, geocoded_at = ?,
                               geocode_version = ?
                           WHERE hash = ?''',
                        (
                            current_geo.get('lat'),
                            current_geo.get('lng'),
                            current_geo.get('source'),
                            current_geo.get('quality'),
                            current_geo.get('query'),
                            now,
                            GEOCODER_VERSION,
                            h,
                        ),
                    )
                continue

            # Some live adapters can publish their own authoritative map point
            # after the text row first appears. Re-apply that source point even
            # when a same-version geocoder fallback already exists.
            raw_lat = incident.get('latitude')
            raw_lng = incident.get('longitude')
            try:
                has_source_point = _is_in_source_bounds(
                    float(raw_lat), float(raw_lng), incident_source
                )
            except (TypeError, ValueError):
                has_source_point = False
            if has_source_point or incident_source == 'lakecharles':
                current_geo = _collector_geocode_result(conn, incident, incident_source)
                cursor.execute(
                    '''UPDATE incidents
                       SET latitude = ?, longitude = ?, geocode_source = ?,
                           geocode_quality = ?, geocode_query = ?, geocoded_at = ?,
                           geocode_version = ?
                       WHERE hash = ?''',
                    (
                        current_geo.get('lat'),
                        current_geo.get('lng'),
                        current_geo.get('source'),
                        current_geo.get('quality'),
                        current_geo.get('query'),
                        now,
                        GEOCODER_VERSION,
                        h,
                    ),
                )
                continue

            # Opportunistic re-geocode: if we previously fell back (or have no coords),
            # try again using the improved intersection logic. This keeps your DB, but improves
            # "bad" points over time.
            try:
                existing_lat = existing[1]
                existing_lng = existing[2]
                existing_source = existing[3] if existing_cols in ("new", "versioned") and len(existing) > 3 else None
                existing_quality = existing[4] if existing_cols in ("new", "versioned") and len(existing) > 4 else None
                existing_version = existing[5] if existing_cols == "versioned" and len(existing) > 5 else None

                needs_geo = (existing_lat is None or existing_lng is None)
                low_quality = (existing_source in (None, "fallback", "skipped", "unresolved")) or (existing_quality in (None, "fallback", "city-only", "cross-only", "unknown-location", "unresolved"))
                stale_version = existing_version != GEOCODER_VERSION
                if (needs_geo or low_quality or stale_version) and (incident.get('street') or incident.get('cross_streets')):
                    geo = _collector_geocode_result(conn, incident, incident_source)
                    if geo:
                        new_has_coords = geo.get('lat') is not None and geo.get('lng') is not None
                        provider_confirmed = new_has_coords or bool(geo.get('provider_responded'))
                        should_update = provider_confirmed and (
                            stale_version or (needs_geo and new_has_coords)
                        )
                        if not needs_geo and new_has_coords:
                            try:
                                dist_m = _haversine_m(float(existing_lat), float(existing_lng), float(geo['lat']), float(geo['lng']))
                                # Only overwrite if materially different (avoid churning minor provider jitter)
                                if dist_m > 75:
                                    should_update = True
                            except Exception:
                                # If distance calc fails, be conservative and avoid overwriting
                                should_update = stale_version

                        if should_update:
                            try:
                                cursor.execute(
                                    'UPDATE incidents SET latitude = ?, longitude = ?, geocode_source = ?, geocode_quality = ?, geocode_query = ?, geocoded_at = ?, geocode_version = ? WHERE hash = ?',
                                    (
                                        geo['lat'],
                                        geo['lng'],
                                        geo.get('source'),
                                        geo.get('quality'),
                                        geo.get('query'),
                                        now,
                                        GEOCODER_VERSION,
                                        h,
                                    )
                                )
                                log(f"Re-geocoded: {incident['description']} -> {geo.get('source')} {geo.get('quality')}")
                            except sqlite3.OperationalError:
                                # Older schema without geocode columns: still update lat/lng if missing
                                cursor.execute(
                                    'UPDATE incidents SET latitude = ?, longitude = ? WHERE hash = ?',
                                    (geo['lat'], geo['lng'], h)
                                )
            except Exception:
                pass
        else:
            # New incident - geocode and insert
            geo = _collector_geocode_result(conn, incident, incident_source)
            first_seen = occurred_at or now
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO incidents 
                    (hash, agency, time, units, description, street, cross_streets, municipality, source, is_active,
                     latitude, longitude, first_seen, last_seen,
                     geocode_source, geocode_quality, geocode_query, geocoded_at, geocode_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    h,
                    incident['agency'],
                    incident['time'],
                    incident['units'],
                    incident['description'],
                    incident['street'],
                    incident['cross_streets'],
                    incident['municipality'],
                    incident_source,
                    desired_active,
                    geo['lat'],
                    geo['lng'],
                    first_seen,
                    now,
                    geo.get('source'),
                    geo.get('quality'),
                    geo.get('query'),
                    now,
                    GEOCODER_VERSION,
                ))
            except sqlite3.OperationalError:
                # Older schema: try insert without geocode metadata columns.
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO incidents 
                        (hash, agency, time, units, description, street, cross_streets, municipality, source, is_active, latitude, longitude, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        h,
                        incident['agency'],
                        incident['time'],
                        incident['units'],
                        incident['description'],
                        incident['street'],
                        incident['cross_streets'],
                        incident['municipality'],
                        incident_source,
                        desired_active,
                        geo['lat'],
                        geo['lng'],
                        first_seen,
                        now
                    ))
                except sqlite3.OperationalError:
                    cursor.execute('''
                        INSERT OR IGNORE INTO incidents 
                        (hash, agency, time, units, description, street, cross_streets, municipality, latitude, longitude, first_seen, last_seen, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        h,
                        incident['agency'],
                        incident['time'],
                        incident['units'],
                        incident['description'],
                        incident['street'],
                        incident['cross_streets'],
                        incident['municipality'],
                        geo['lat'],
                        geo['lng'],
                        first_seen,
                        now,
                        desired_active,
                    ))
            if incident_source not in ('neworleans', 'lakecharles'):
                log(f"New incident: {incident['description']} at {incident['street'] or incident['cross_streets']}")

        if has_peak_units and not existing:
            cursor.execute(
                'UPDATE incidents SET peak_units = MAX(COALESCE(peak_units, 0), COALESCE(units, 0)) WHERE hash = ?',
                (h,),
            )

    if deactivate_missing:
        # Mark incidents no longer in feed as inactive (source-scoped).
        if not has_source_column:
            if source_default != 'caddo':
                active_hashes = []
            else:
                cursor.execute('SELECT hash FROM incidents WHERE is_active = 1')
                active_hashes = [row[0] for row in cursor.fetchall()]
        elif source_default == 'caddo':
            cursor.execute("SELECT hash FROM incidents WHERE is_active = 1 AND (source = 'caddo' OR source IS NULL OR TRIM(source) = '')")
            active_hashes = [row[0] for row in cursor.fetchall()]
        else:
            cursor.execute('SELECT hash FROM incidents WHERE is_active = 1 AND source = ?', (source_default,))
            active_hashes = [row[0] for row in cursor.fetchall()]

        for h in active_hashes:
            if h not in current_hashes:
                cursor.execute('UPDATE incidents SET is_active = 0 WHERE hash = ?', (h,))

    conn.commit()


def _store_feed_refresh(source: str, refreshed_at_text: str | None) -> None:
    global feed_refreshed_at
    if not refreshed_at_text:
        return
    source_name = _normalize_source_name(source)
    feed_refreshed_by_source[source_name] = refreshed_at_text
    meta_set(f'feed_refreshed_at_{source_name}', refreshed_at_text)
    if source_name == 'caddo':
        # Keep old status field for backwards compatibility with frontend clients.
        feed_refreshed_at = refreshed_at_text
        meta_set('feed_refreshed_at', refreshed_at_text)


LIVE_SNAPSHOT_SOURCES = {'caddo', 'batonrouge', 'lafayette'}


def background_scrape():
    """Background task to scrape incidents periodically"""
    global feed_refreshed_at, last_scrape_started_at, last_scrape_finished_at

    last_scrape_started_at = datetime.now(timezone.utc).isoformat()
    meta_set('last_scrape_started_at', last_scrape_started_at)

    source_jobs = [
        ('caddo', 'Caddo 911', scrape_caddo_incidents),
        ('batonrouge', 'Baton Rouge Traffic', scrape_batonrouge_incidents),
        ('lafayette', 'Lafayette 911', scrape_lafayette_incidents),
        ('neworleans', 'New Orleans daily calls for service', scrape_neworleans_incidents),
        ('lakecharles', 'Lake Charles closed police calls (24-hour delay)', scrape_lakecharles_incidents),
    ]
    for source_name, label, scraper in source_jobs:
        min_interval = SOURCE_MIN_SCRAPE_INTERVAL_SECONDS.get(source_name, 0)
        previous_run = source_last_scrape_monotonic.get(source_name)
        monotonic_now = time.monotonic()
        if previous_run is not None and monotonic_now - previous_run < min_interval:
            continue
        source_last_scrape_monotonic[source_name] = monotonic_now
        log(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping {label}...")
        incidents, refreshed_at_text = scraper()
        _store_feed_refresh(source_name, refreshed_at_text)
        if incidents or (source_name in LIVE_SNAPSHOT_SOURCES and incidents is not None):
            process_incidents(incidents, source=source_name,
                              allow_empty=source_name in LIVE_SNAPSHOT_SOURCES and incidents is not None)
            active_count = sum(1 for incident in incidents if incident.get('is_active', True))
            log(
                f"[{datetime.now().strftime('%H:%M:%S')}] Processed {len(incidents)} "
                f"incidents ({active_count} latest) from {source_name}"
            )
        else:
            log(f"[{datetime.now().strftime('%H:%M:%S')}] No incidents found or scraping failed for {source_name}")

    last_scrape_finished_at = datetime.now(timezone.utc).isoformat()
    meta_set('last_scrape_finished_at', last_scrape_finished_at)


VALID_SOURCES = {'caddo', 'lafayette', 'batonrouge', 'neworleans', 'lakecharles'}


def _normalize_source_filter(value: str | None) -> str:
    s = (value or 'all').strip().lower()
    if s == 'all':
        return 'all'
    return s if s in VALID_SOURCES else 'all'


def _normalize_incident_source_for_read(source: str | None) -> str:
    return _normalize_source_name(source or 'caddo')


def _incidents_table_has_source_column(cursor: sqlite3.Cursor) -> bool:
    try:
        cursor.execute("PRAGMA table_info(incidents)")
        rows = cursor.fetchall() or []
    except sqlite3.Error:
        return False
    for row in rows:
        col_name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if str(col_name).strip().lower() == 'source':
            return True
    return False


def _ensure_incidents_source_column(conn: sqlite3.Connection) -> bool:
    cursor = conn.cursor()
    if _incidents_table_has_source_column(cursor):
        return True
    try:
        cursor.execute("ALTER TABLE incidents ADD COLUMN source TEXT DEFAULT 'caddo'")
        cursor.execute("UPDATE incidents SET source = 'caddo' WHERE source IS NULL OR TRIM(source) = ''")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON incidents(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_active_first_seen ON incidents(source, is_active, first_seen DESC)")
        conn.commit()
        log("[DB] Added missing incidents.source column at runtime")
        return True
    except sqlite3.OperationalError as e:
        log(f"[DB] Could not add incidents.source column yet: {e}")
        return False


def _incident_matches_source_filter(incident: dict, source_filter: str) -> bool:
    if source_filter == 'all':
        return True
    return _normalize_incident_source_for_read(incident.get('source')) == source_filter


def _incident_location_label(incident: dict) -> str | None:
    street = _clean_ws(incident.get('street') or '')
    cross = _clean_ws(incident.get('cross_streets') or '')
    municipality = _clean_ws(incident.get('municipality') or '')

    if street and cross:
        return f"{street} @ {cross}"
    if street:
        return street
    if cross:
        return cross
    if municipality:
        return municipality
    return None


# Severity mirrors getSeverity() in public/index.html so report counts use the
# same colors as the map. tests/severity_cases.json keeps the two in step.
SEVERITY_ORDER = ['high', 'medical', 'medium', 'low', 'service', 'custody']
FORCE_HIGH_SEVERITY_TERMS = [
    'mass casualty', 'mass casualties', 'multiple casualty',
    'mass casulty', 'mass causality',
    'suicide attempt', 'attempted suicide',
]
SEVERITY_TERMS = {
    'custody': ['prisoner'],
    'medical': ['medical emergency', 'caddo ems event'],
    'service': ['special event stand by', 'public service'],
    'low': [
        'deliver message', 'prisoner medical security', 'periodic check', 'taken by other agency',
        'follow up', 'followup', 'follow', 'investigation', 'report', 'information',
        'citizen assist', 'citizen assistance', 'assist citizen', 'civil',
        'welfare check', 'wellness check', 'property check', 'extra patrol', 'directed patrol',
        'noise', 'complaint', 'parking', 'traffic control', 'traffic stop',
        'lost property', 'found property',
        'hang up', 'hangup', 'open line to 911', 'open 911 line', '911 open line',
    ],
    'high': [
        'shots fired', 'shooting', 'shot fired', 'gun', 'armed', 'weapon',
        'stabbing', 'stab', 'knife',
        'assault', 'battery', 'domestic', 'fight',
        'robbery', 'home invasion', 'kidnap', 'hostage',
        'homicide', 'murder', 'rape', 'sexual',
        'theft', 'burglary', 'vehicle burglary', 'break in', 'breaking entering',
        'structure fire', 'house fire', 'apartment fire', 'building fire',
        'fire', 'smoke', 'explosion',
        'medical', 'ems', 'unconscious', 'not breathing', 'difficulty breathing', 'choking',
        'overdose', 'cardiac', 'heart', 'stroke', 'seizure',
        'hit and run', 'hit run', 'injury accident', 'accident with injuries', 'fatal', 'entrap', 'rollover',
    ],
    'medium': [
        'stolen', 'shoplift', 'vandal',
        'accident', 'crash', 'wreck', 'collision', 'mvc', 'mva',
        'road hazard', 'debris', 'disabled vehicle',
        'loose livestock', 'livestock', 'loose animal', 'animal in roadway', 'cow', 'horse',
        'disturbance', 'suspicious', 'trespass', 'harassment', 'fraud', 'alarm',
        'juvenile complaint',
    ],
}


@functools.lru_cache(maxsize=8192)
def incident_severity(description: str | None, agency: str | None) -> str:
    d = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(description or '').lower())).strip()
    agency = str(agency or '').upper()

    def has_any(terms):
        return any(term in d for term in terms)

    if 'minor accident' in d or 'minor crash' in d or 'fender bender' in d:
        return 'low'
    if has_any(FORCE_HIGH_SEVERITY_TERMS) or ' mci ' in f' {d} ':
        return 'high'
    if has_any(SEVERITY_TERMS['custody']):
        return 'custody'
    if 'EMS' in agency or has_any(SEVERITY_TERMS['medical']):
        return 'medical'
    for level in ('service', 'high', 'low', 'medium'):
        if has_any(SEVERITY_TERMS[level]):
            return level
    return 'medium'


REPORT_SOURCE_LABELS = {
    'caddo': 'Caddo Parish',
    'batonrouge': 'Baton Rouge',
    'lafayette': 'Lafayette Parish',
    'neworleans': 'New Orleans',
    'lakecharles': 'Lake Charles',
}
REPORT_NEARBY_RADII = (0.1, 0.25, 0.5, 1.0, 2.0)
REPORT_NEARBY_DAYS = (30, 90, 365, 0)  # 0 = everything on record
REPORT_NEARBY_LIST_LIMIT = 100
REPORT_NEARBY_POINT_LIMIT = 1500
REPORT_CACHE_TTL_SECONDS = 120
_report_cache: dict[tuple, tuple[float, dict]] = {}
_report_cache_lock = Lock()
_report_geocode_cache: dict[str, list[dict]] = {}


def _report_cache_get(key: tuple, ttl: float | None) -> dict | None:
    with _report_cache_lock:
        hit = _report_cache.get(key)
    if hit and (ttl is None or time.monotonic() - hit[0] <= ttl):
        return hit[1]
    return None


def _report_cache_put(key: tuple, value: dict) -> None:
    with _report_cache_lock:
        _report_cache[key] = (time.monotonic(), value)
        if len(_report_cache) > 256:
            _report_cache.pop(min(_report_cache, key=lambda k: _report_cache[k][0]), None)


def _report_db_paths(months: list[str] | None) -> list[str]:
    """Main DB first, then the archives for the given Central months (None = all)."""
    if months is None:
        return [DB_PATH] + _list_archive_dbs()
    paths = [DB_PATH]
    for month in months:
        paths.extend(_get_archive_dbs_for_month(month))
    return paths


def _iter_report_rows(paths: list[str], sql_where: str, args: tuple, columns: str):
    """Yield incident rows once each across the main DB and archives."""
    seen: set[str] = set()
    for path in paths:
        try:
            if path == DB_PATH:
                conn = db_connect(row_factory=True)
            else:
                conn = _archive_db_connect(path, row_factory=True)
        except Exception as e:
            log(f"[REPORTS] Error opening {path}: {e}")
            continue
        try:
            rows = conn.execute(f"SELECT hash, {columns} FROM incidents WHERE {sql_where}", args).fetchall()
        except sqlite3.OperationalError as e:
            log(f"[REPORTS] Error reading {path}: {e}")
            rows = []
        finally:
            conn.close()
        for index, row in enumerate(rows):
            key = row['hash'] or f"{path}:{index}"
            if key in seen:
                continue
            seen.add(key)
            yield row


def _report_available_months() -> list[str]:
    cached = _report_cache_get(('months',), REPORT_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached['months']
    months: set[str] = set()
    for path in _list_archive_dbs():
        match = re.search(r'_(\d{4})_(\d{2})\.db$', path)
        if match:
            months.add(f"{match.group(1)}-{match.group(2)}")
    conn = db_connect()
    try:
        first, last = conn.execute('SELECT MIN(first_seen), MAX(first_seen) FROM incidents').fetchone()
    finally:
        conn.close()
    start, end = _central_month_key(first), _central_month_key(last)
    if start and end:
        year, month = map(int, start.split('-'))
        while f"{year:04d}-{month:02d}" <= end:
            months.add(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    result = sorted(months, reverse=True)
    _report_cache_put(('months',), {'months': result})
    return result


def _previous_month(month: str) -> str:
    year, mon = map(int, month.split('-'))
    return f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"


def _month_source_rows(month: str):
    start_utc, end_utc = _central_month_bounds_utc(month)
    return _iter_report_rows(
        _report_db_paths([month]),
        'first_seen >= ? AND first_seen < ?',
        (start_utc, end_utc),
        'source, description, agency',
    )


def _build_parish_summary(month: str) -> dict:
    per_source: dict[str, dict] = {}
    for row in _month_source_rows(month):
        source = _normalize_incident_source_for_read(row['source'])
        entry = per_source.setdefault(source, {'total': 0, 'severity': dict.fromkeys(SEVERITY_ORDER, 0), 'types': {}})
        severity = incident_severity(row['description'], row['agency'])
        description = _clean_ws(row['description'] or '') or 'Unlisted'
        entry['total'] += 1
        entry['severity'][severity] += 1
        type_entry = entry['types'].setdefault(description, {'count': 0, 'severity': dict.fromkeys(SEVERITY_ORDER, 0)})
        type_entry['count'] += 1
        type_entry['severity'][severity] += 1

    previous_totals: dict[str, int] = {}
    previous = _previous_month(month)
    if previous in _report_available_months():
        for row in _month_source_rows(previous):
            source = _normalize_incident_source_for_read(row['source'])
            previous_totals[source] = previous_totals.get(source, 0) + 1

    sources = []
    for source in REPORT_SOURCE_LABELS:
        entry = per_source.get(source)
        if not entry:
            continue
        ranked = sorted(entry['types'].items(), key=lambda item: (-item[1]['count'], item[0]))
        sources.append({
            'key': source,
            'label': REPORT_SOURCE_LABELS[source],
            'total': entry['total'],
            'previousTotal': previous_totals.get(source),
            'severity': entry['severity'],
            'types': [
                {
                    'description': description,
                    'count': data['count'],
                    # A description can straddle levels only through agency (EMS); report the majority.
                    'severity': max(SEVERITY_ORDER, key=lambda level: data['severity'][level]),
                }
                for description, data in ranked[:40]
            ],
        })
    return {'month': month, 'sources': sources}


def _parse_report_float(name: str) -> float | None:
    value = request.args.get(name, type=float)
    return value if value is not None and math.isfinite(value) else None


def _in_report_coverage(lat: float, lon: float) -> bool:
    return any(
        profile['lat_min'] - 0.05 <= lat <= profile['lat_max'] + 0.05
        and profile['lon_min'] - 0.05 <= lon <= profile['lon_max'] + 0.05
        for profile in SOURCE_GEO_PROFILES.values()
    )


def _build_nearby_report(lat: float, lon: float, radius_miles: float, days: int) -> dict:
    lat_pad = radius_miles / 69.0
    lon_pad = radius_miles / (69.0 * max(math.cos(math.radians(lat)), 0.2))
    where = 'latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?'
    args: tuple = (lat - lat_pad, lat + lat_pad, lon - lon_pad, lon + lon_pad)
    months = None
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        where += ' AND first_seen >= ?'
        args += (since.isoformat(),)
        months = []
        cursor_month = _central_month_key(since.isoformat())
        current = datetime.now(CENTRAL_TZ).strftime('%Y-%m')
        while cursor_month and cursor_month <= current:
            months.append(cursor_month)
            year, mon = map(int, cursor_month.split('-'))
            cursor_month = f"{year + 1:04d}-01" if mon == 12 else f"{year:04d}-{mon + 1:02d}"

    radius_m = radius_miles * 1609.344
    matches = []
    # Wide searches see the same streets, descriptions, and hours thousands of
    # times; memoize the text normalization per request.
    unknown_cache: dict[tuple, bool] = {}
    for row in _iter_report_rows(
        _report_db_paths(months), where, args,
        'source, description, agency, time, units, street, cross_streets, municipality, '
        'latitude, longitude, first_seen, geocode_quality',
    ):
        location_key = (row['street'], row['cross_streets'])
        unknown = unknown_cache.get(location_key)
        if unknown is None:
            unknown = unknown_cache[location_key] = _is_unknown_location(*location_key)
        if unknown:
            continue
        distance_m = _haversine_m(lat, lon, row['latitude'], row['longitude'])
        if distance_m > radius_m:
            continue
        incident = dict(row)
        incident['source'] = _normalize_incident_source_for_read(incident['source'])
        incident['severity'] = incident_severity(incident['description'], incident['agency'])
        incident['distance_m'] = distance_m
        matches.append(incident)

    matches.sort(key=lambda item: item['first_seen'] or '', reverse=True)
    severity_counts = dict.fromkeys(SEVERITY_ORDER, 0)
    source_counts: dict[str, int] = {}
    month_counts: dict[str, int] = {}
    types: dict[str, dict] = {}
    blocks: dict[str, dict] = {}
    approximate = 0
    month_cache: dict[str, str | None] = {}
    description_cache: dict[str, str] = {}
    label_cache: dict[tuple, str | None] = {}
    for incident in matches:
        severity_counts[incident['severity']] += 1
        source_counts[incident['source']] = source_counts.get(incident['source'], 0) + 1
        hour = (incident['first_seen'] or '')[:13]  # the Central month can only change on the hour
        if hour not in month_cache:
            month_cache[hour] = _central_month_key(incident['first_seen'])
        month_key = month_cache[hour]
        if month_key:
            month_counts[month_key] = month_counts.get(month_key, 0) + 1
        if str(incident.get('geocode_quality') or '').startswith('approximate'):
            approximate += 1
        raw_description = incident['description'] or ''
        if raw_description not in description_cache:
            description_cache[raw_description] = _clean_ws(raw_description) or 'Unlisted'
        description = description_cache[raw_description]
        entry = types.setdefault(description, {'description': description, 'count': 0, 'severity': incident['severity']})
        entry['count'] += 1
        # The published street + cross streets (or block address) is the closest thing to a block.
        label_key = (incident['street'], incident['cross_streets'], incident['municipality'])
        if label_key not in label_cache:
            label_cache[label_key] = _incident_location_label(incident)
        label = label_cache[label_key]
        if label:
            block = blocks.setdefault(label.upper(), {
                'label': label, 'count': 0, 'severity': dict.fromkeys(SEVERITY_ORDER, 0), '_lat': 0.0, '_lon': 0.0,
            })
            block['count'] += 1
            block['severity'][incident['severity']] += 1
            block['_lat'] += incident['latitude']
            block['_lon'] += incident['longitude']

    def list_item(incident: dict) -> dict:
        return {
            'description': incident['description'],
            'severity': incident['severity'],
            'source': incident['source'],
            'location': _incident_location_label(incident),
            'reported_at': _incident_reported_at(incident) or incident['first_seen'],
            'units': incident['units'],
            'distance_mi': round(incident['distance_m'] / 1609.344, 2),
            'approximate': str(incident.get('geocode_quality') or '').startswith('approximate'),
            'lat': round(incident['latitude'], 5),
            'lon': round(incident['longitude'], 5),
        }

    return {
        'center': {'lat': lat, 'lon': lon},
        'radiusMiles': radius_miles,
        'days': days,
        'total': len(matches),
        'approximateCount': approximate,
        'severity': severity_counts,
        'sources': [
            {'key': key, 'label': REPORT_SOURCE_LABELS.get(key, key), 'count': count}
            for key, count in sorted(source_counts.items(), key=lambda item: -item[1])
        ],
        'months': [{'month': key, 'count': month_counts[key]} for key in sorted(month_counts)],
        'types': sorted(types.values(), key=lambda item: (-item['count'], item['description']))[:25],
        'blocks': [
            {
                'label': block['label'], 'count': block['count'], 'severity': block['severity'],
                'lat': round(block['_lat'] / block['count'], 5), 'lon': round(block['_lon'] / block['count'], 5),
            }
            for block in sorted(blocks.values(), key=lambda item: (-item['count'], item['label']))[:300]
        ],
        # Compact map points: [lat, lon, severity index]
        'points': [
            [round(item['latitude'], 5), round(item['longitude'], 5), SEVERITY_ORDER.index(item['severity'])]
            for item in matches[:REPORT_NEARBY_POINT_LIMIT]
        ],
        'pointsTruncated': len(matches) > REPORT_NEARBY_POINT_LIMIT,
        'incidents': [list_item(item) for item in matches[:REPORT_NEARBY_LIST_LIMIT]],
    }


def _geocode_report_query(query: str) -> list[dict]:
    """Resolve a typed place to points inside Louisiana911 coverage."""
    key = query.lower()
    if key in _report_geocode_cache:
        return _report_geocode_cache[key]
    with _report_cache_lock:
        if len(_report_geocode_cache) > 512:
            _report_geocode_cache.pop(next(iter(_report_geocode_cache)), None)

    search = query if re.search(r'\b(la|louisiana)\b', query, re.IGNORECASE) else f"{query}, Louisiana"
    results: list[dict] = []
    try:
        locations = geolocator_arcgis.geocode(search, exactly_one=False, timeout=5) or []
    except Exception as e:
        log(f"[REPORTS] Geocode failed for {query!r}: {e}")
        locations = []
    for location in locations[:8]:
        lat, lon = float(location.latitude), float(location.longitude)
        if not _in_report_coverage(lat, lon):
            continue
        results.append({'label': location.address, 'lat': round(lat, 6), 'lon': round(lon, 6)})
        if len(results) >= 5:
            break
    _report_geocode_cache[key] = results
    return results


# API Routes
@app.route('/')
def index():
    return _serve_index_with_history_ui_session()


@app.route('/index.html')
def index_html():
    return _serve_index_with_history_ui_session()


@app.route('/incident/<share_id>')
def shared_incident(share_id: str):
    incident = _find_incident_by_share_id(share_id)
    if incident is None:
        abort(404)
    return _serve_shared_incident_page(incident)


@app.route('/about/')
def about():
    return send_from_directory('public', 'about.html')

@app.route('/about')
def about_redirect():
    return redirect('/about/', code=301)


@app.route('/caddo911/')
def caddo911_landing():
    return _serve_coverage_map('caddo911.html', 'caddo', 'Caddo 911 active calls',
                               'Shreveport and Caddo Parish calls from the public Caddo 911 feed. Tap a marker for incident details. Independent and unofficial.')


@app.route('/caddo911')
def caddo911_landing_redirect():
    return redirect('/caddo911/', code=301)


@app.route('/coverage/')
def coverage():
    return send_from_directory('public', 'coverage.html')


@app.route('/coverage')
def coverage_redirect():
    return redirect('/coverage/', code=301)


@app.route('/coverage/baton-rouge/')
def baton_rouge_coverage():
    return _serve_coverage_map('coverage-baton-rouge.html', 'batonrouge', 'Baton Rouge traffic incidents',
                               'Public traffic incidents in Baton Rouge and East Baton Rouge Parish. Tap a marker for details. This feed does not include every 911 call.')


@app.route('/coverage/baton-rouge')
def baton_rouge_coverage_redirect():
    return redirect('/coverage/baton-rouge/', code=301)


@app.route('/coverage/lafayette/')
def lafayette_coverage():
    return _serve_coverage_map('coverage-lafayette.html', 'lafayette', 'Lafayette Parish traffic incidents',
                               'Current traffic incidents from the public Lafayette 911 feed. Tap a marker for details. This feed does not include every 911 call.')


@app.route('/coverage/lafayette')
def lafayette_coverage_redirect():
    return redirect('/coverage/lafayette/', code=301)


@app.route('/coverage/new-orleans/')
def new_orleans_coverage():
    return _serve_coverage_map('coverage-new-orleans.html', 'neworleans', 'New Orleans calls for service',
                               'The latest published NOPD calls for Orleans Parish. These are delayed daily records, not live dispatch. Tap a marker for details.')


@app.route('/coverage/new-orleans')
def new_orleans_coverage_redirect():
    return redirect('/coverage/new-orleans/', code=301)


@app.route('/reports/')
def reports():
    with open(os.path.join(app.root_path, 'public', 'reports.html'), encoding='utf-8') as page:
        html = page.read()
    config = json.dumps({'cartoKey': _carto_basemap_key()}, ensure_ascii=True).replace('<', '\\u003c')
    html = html.replace('<script type="application/json" id="map-config">{}</script>',
                        f'<script type="application/json" id="map-config">{config}</script>', 1)
    return app.response_class(html, mimetype='text/html')

@app.route('/reports')
def reports_redirect():
    return redirect('/reports/', code=301)


@app.route('/reports/monthly/')
@app.route('/reports/monthly')
def monthly_reports_redirect():
    return redirect('/reports/', code=301)

@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})

PUBLIC_INCIDENT_FIELDS = (
    'id', 'hash', 'agency', 'time', 'units', 'peak_units', 'description', 'street', 'cross_streets',
    'municipality', 'source', 'latitude', 'longitude', 'first_seen', 'last_seen',
    'is_active', 'geocode_source', 'geocode_quality',
)


def _incident_reported_at(incident):
    """Keep full source timestamps or infer an undated clock's calendar date.

    Lafayette and delayed sources store their published date in first_seen.
    Caddo/Baton Rouge and legacy Lafayette rows need an estimate anchored to
    first_seen, never last_seen or today. An outage can make this estimate late.
    """
    source = _normalize_incident_source_for_read(incident.get('source'))
    first_seen = _parse_iso_datetime(incident.get('first_seen'))
    if source in ('neworleans', 'lakecharles'):
        return first_seen.isoformat() if first_seen else None
    if source not in LIVE_SNAPSHOT_SOURCES:
        return None
    raw = str(incident.get('time') if incident.get('time') is not None else '').strip()
    if not first_seen or not re.fullmatch(r'\d{1,4}', raw):
        return None
    hour, minute = divmod(int(raw), 100)
    if hour > 23 or minute > 59:
        return None
    local = first_seen.astimezone(CENTRAL_TZ)
    for days_back in (0, 1):
        day = local.date() - timedelta(days=days_back)
        candidates = []
        for fold in (0, 1):
            candidate = datetime(day.year, day.month, day.day, hour, minute,
                                 tzinfo=CENTRAL_TZ, fold=fold).astimezone(timezone.utc)
            roundtrip = candidate.astimezone(CENTRAL_TZ)
            if (candidate <= first_seen and roundtrip.date() == day
                    and (roundtrip.hour, roundtrip.minute) == (hour, minute)):
                candidates.append(candidate)
        if candidates:
            return max(candidates).isoformat()
    return None


def _public_incident(incident):
    # Never serialize a database row wholesale: new internal columns stay private.
    payload = {field: incident.get(field) for field in PUBLIC_INCIDENT_FIELDS}
    payload['reported_at'] = _incident_reported_at(incident)
    return payload


@app.route('/api/incidents/active')
def get_active_incidents():
    source_filter = _normalize_source_filter(request.args.get('source'))
    conn = db_connect(row_factory=True)
    cursor = conn.cursor()
    has_source_column = _ensure_incidents_source_column(conn)
    try:
        if source_filter == 'all' or not has_source_column:
            cursor.execute('SELECT * FROM incidents WHERE is_active = 1 ORDER BY time DESC')
        elif source_filter == 'caddo':
            cursor.execute("SELECT * FROM incidents WHERE is_active = 1 AND (source = 'caddo' OR source IS NULL OR TRIM(source) = '') ORDER BY time DESC")
        else:
            cursor.execute('SELECT * FROM incidents WHERE is_active = 1 AND source = ? ORDER BY time DESC', (source_filter,))
        incidents = [dict(row) for row in cursor.fetchall()]
        if has_source_column and source_filter in ('all', 'lakecharles'):
            # Latest includes a bounded recent slice of closed calls. Keep
            # is_active=0 so they are never counted as active and are available
            # in History immediately, under their original call date.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lakecharles_source.RECENT_DAYS)).isoformat()
            cursor.execute(
                "SELECT * FROM incidents WHERE source = 'lakecharles' AND is_active = 0 "
                "AND first_seen >= ? ORDER BY first_seen DESC, id DESC LIMIT ?",
                (cutoff, lakecharles_source.RECENT_LIMIT),
            )
            incidents.extend(dict(row) for row in cursor.fetchall())
    except sqlite3.OperationalError:
        cursor.execute('SELECT * FROM incidents WHERE is_active = 1 ORDER BY time DESC')
        incidents = [dict(row) for row in cursor.fetchall()]
    for incident in incidents:
        incident['source'] = _normalize_incident_source_for_read(incident.get('source'))
    if source_filter != 'all':
        incidents = [incident for incident in incidents if _incident_matches_source_filter(incident, source_filter)]
    conn.close()
    return jsonify([_public_incident(incident) for incident in incidents])

def _query_history_candidates(conn, bounds, source_filter, candidate_limit, *, archived=False):
    """Read only the prefix needed to merge one page across live/archive databases."""
    cursor = conn.cursor()
    has_source = _incidents_table_has_source_column(cursor)
    if source_filter not in ('all', 'caddo') and not has_source:
        return [], 0
    clauses = ['first_seen >= ?', 'first_seen < ?']
    args = list(bounds)
    if not archived:
        clauses.append('is_active = 0')
    if source_filter == 'caddo' and has_source:
        clauses.append("(source = 'caddo' OR source IS NULL OR TRIM(source) = '')")
    elif source_filter != 'all' and has_source:
        clauses.append('source = ?')
        args.append(source_filter)
    where = ' AND '.join(clauses)
    # Count and page use the same snapshot even if the collector updates rows.
    if not conn.in_transaction:
        conn.execute('BEGIN')
    total = cursor.execute(f'SELECT COUNT(*) FROM incidents WHERE {where}', args).fetchone()[0]
    rows = cursor.execute(
        f'SELECT * FROM incidents WHERE {where} ORDER BY first_seen DESC, id DESC LIMIT ?',
        (*args, candidate_limit),
    ).fetchall()
    return [dict(row) for row in rows], total


@app.route('/api/incidents/history')
def get_history():
    ui_guard = _history_ui_request_guard()
    if ui_guard is not None:
        return ui_guard

    requested_limit = request.args.get('limit', 100, type=int)
    requested_offset = request.args.get('offset', 0, type=int)
    limit = max(1, min(requested_limit or 100, max(1, INCIDENT_HISTORY_MAX_LIMIT)))
    offset = max(0, requested_offset or 0)
    date = request.args.get('date')
    source_filter = _normalize_source_filter(request.args.get('source'))
    bounds = _central_date_bounds_utc(date)
    if not bounds:
        return jsonify({'error': 'a valid date is required (YYYY-MM-DD)'}), 400

    # Any result in the merged first K rows must be in its own database's first K.
    # This keeps page one bounded even when a day contains thousands of calls.
    candidate_limit = min(offset + limit, 2**63 - 1)
    conn = db_connect(row_factory=True)
    try:
        all_incidents, total = _query_history_candidates(conn, bounds, source_filter, candidate_limit)
    finally:
        conn.close()

    for archive_path in _get_archive_dbs_for_date(date):
        archive_conn = None
        try:
            archive_conn = _archive_db_connect(archive_path, row_factory=True)
            rows, count = _query_history_candidates(
                archive_conn, bounds, source_filter, candidate_limit, archived=True,
            )
            all_incidents.extend(rows)
            total += count
        except sqlite3.Error as error:
            log(f'[ARCHIVE] Error reading {archive_path}: {error}')
            # A partial page would falsely tell the UI that the missing calls do not exist.
            return jsonify({'error': 'History is temporarily unavailable. Please retry.'}), 503
        finally:
            if archive_conn is not None:
                archive_conn.close()

    # IDs resolve equal timestamps consistently so pagination does not shuffle.
    all_incidents.sort(key=lambda item: (item.get('first_seen') or '', item.get('id') or 0), reverse=True)
    paginated = all_incidents[offset:offset + limit]
    for incident in paginated:
        incident['source'] = _normalize_incident_source_for_read(incident.get('source'))
    return jsonify({'incidents': [_public_incident(incident) for incident in paginated], 'total': total})

@app.route('/api/incidents/history_counts')
def get_history_counts():
    ui_guard = _history_ui_request_guard()
    if ui_guard is not None:
        return ui_guard

    month = request.args.get('month')  # YYYY-MM format
    source_filter = _normalize_source_filter(request.args.get('source'))

    if not month or len(month) != 7:
        return jsonify({'error': 'month is required (YYYY-MM)'}), 400

    counts: dict[str, int] = {}
    bounds = _central_month_bounds_utc(month)
    
    # Query main database
    conn = db_connect(row_factory=True)
    cursor = conn.cursor()
    has_source_column = _ensure_incidents_source_column(conn)
    if bounds:
        start_utc, end_utc = bounds
        if source_filter == 'all':
            cursor.execute(
                'SELECT first_seen FROM incidents WHERE is_active = 0 AND first_seen >= ? AND first_seen < ?',
                (start_utc, end_utc)
            )
        elif source_filter == 'caddo' and has_source_column:
            cursor.execute(
                "SELECT first_seen FROM incidents WHERE is_active = 0 AND first_seen >= ? AND first_seen < ? AND (source = 'caddo' OR source IS NULL OR TRIM(source) = '')",
                (start_utc, end_utc)
            )
        elif source_filter == 'caddo':
            cursor.execute(
                'SELECT first_seen FROM incidents WHERE is_active = 0 AND first_seen >= ? AND first_seen < ?',
                (start_utc, end_utc)
            )
        elif not has_source_column:
            cursor.execute('SELECT first_seen FROM incidents WHERE 1 = 0')
        else:
            cursor.execute(
                'SELECT first_seen FROM incidents WHERE is_active = 0 AND first_seen >= ? AND first_seen < ? AND source = ?',
                (start_utc, end_utc, source_filter)
            )
        for row in cursor.fetchall():
            day = _central_date_key(row['first_seen'])
            if day:
                counts[day] = counts.get(day, 0) + 1
    conn.close()
    
    # Also query archive database if it exists for this month
    archive_dbs = _get_archive_dbs_for_month(month)
    for archive_path in archive_dbs:
        try:
            archive_conn = _archive_db_connect(archive_path, row_factory=True)
            archive_cursor = archive_conn.cursor()
            if bounds:
                start_utc, end_utc = bounds
                try:
                    if source_filter == 'all':
                        archive_cursor.execute(
                            'SELECT first_seen FROM incidents WHERE first_seen >= ? AND first_seen < ?',
                            (start_utc, end_utc)
                        )
                    elif source_filter == 'caddo':
                        archive_cursor.execute(
                            "SELECT first_seen FROM incidents WHERE first_seen >= ? AND first_seen < ? AND (source = 'caddo' OR source IS NULL OR TRIM(source) = '')",
                            (start_utc, end_utc)
                        )
                    else:
                        archive_cursor.execute(
                            'SELECT first_seen FROM incidents WHERE first_seen >= ? AND first_seen < ? AND source = ?',
                            (start_utc, end_utc, source_filter)
                        )
                except sqlite3.OperationalError:
                    # Legacy archive DBs are Caddo-only and have no source column.
                    if source_filter != 'caddo':
                        archive_conn.close()
                        continue
                    archive_cursor.execute(
                        'SELECT first_seen FROM incidents WHERE first_seen >= ? AND first_seen < ?',
                        (start_utc, end_utc)
                    )
                for row in archive_cursor.fetchall():
                    day = _central_date_key(row['first_seen'])
                    if day:
                        counts[day] = counts.get(day, 0) + 1
            archive_conn.close()
        except Exception as e:
            log(f"[ARCHIVE] Error reading {archive_path}: {e}")

    return jsonify({'counts': counts})

@app.route('/api/reports/summary')
def get_report_summary():
    months = _report_available_months()
    month = (request.args.get('month') or (months[0] if months else '')).strip()
    if len(month) != 7 or _central_month_bounds_utc(month) is None:
        return jsonify({'error': 'month must be in YYYY-MM format'}), 400
    if month not in months:
        return jsonify({'error': 'No records for that month.', 'months': months}), 404

    current_month = datetime.now(CENTRAL_TZ).strftime('%Y-%m')
    # Finished months never change; the current month refreshes every few minutes.
    ttl = REPORT_CACHE_TTL_SECONDS if month >= _previous_month(current_month) else None
    summary = _report_cache_get(('summary', month), ttl)
    if summary is None:
        summary = _build_parish_summary(month)
        _report_cache_put(('summary', month), summary)
    return jsonify({**summary, 'months': months, 'isCurrentMonth': month == current_month})


@app.route('/api/reports/nearby')
def get_nearby_report():
    lat = _parse_report_float('lat')
    lon = _parse_report_float('lon')
    radius = _parse_report_float('radius')
    days = request.args.get('days', default=90, type=int)
    if lat is None or lon is None or not _in_report_coverage(lat, lon):
        return jsonify({'error': 'Pick a point inside an area Louisiana911 covers.'}), 400
    if radius not in REPORT_NEARBY_RADII:
        return jsonify({'error': 'radius must be one of ' + ', '.join(f'{r:g}' for r in REPORT_NEARBY_RADII)}), 400
    if days not in REPORT_NEARBY_DAYS:
        return jsonify({'error': 'days must be 30, 90, 365, or 0 for all records'}), 400

    lat, lon = round(lat, 5), round(lon, 5)
    key = ('nearby', lat, lon, radius, days)
    report = _report_cache_get(key, REPORT_CACHE_TTL_SECONDS)
    if report is None:
        report = _build_nearby_report(lat, lon, radius, days)
        _report_cache_put(key, report)
    return jsonify(report)


@app.route('/api/reports/geocode')
def get_report_geocode():
    query = _clean_ws(request.args.get('q') or '')
    if not 3 <= len(query) <= 140 or not re.fullmatch(r"[\w .,#&@'/-]+", query):
        return jsonify({'error': 'Enter an address, intersection, or place name.'}), 400
    return jsonify({'results': _geocode_report_query(query)})


@app.route('/api/stats')
def get_stats():
    conn = db_connect(row_factory=True)
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) as count FROM incidents WHERE is_active = 1')
    active = cursor.fetchone()['count']
    
    today = 0
    today_bounds = _central_date_bounds_utc(datetime.now(CENTRAL_TZ).date().isoformat())
    if today_bounds:
        start_utc, end_utc = today_bounds
        cursor.execute(
            "SELECT COUNT(*) as count FROM incidents WHERE first_seen >= ? AND first_seen < ?",
            (start_utc, end_utc)
        )
        today = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM incidents')
    total = cursor.fetchone()['count']
    
    cursor.execute('SELECT agency, COUNT(*) as count FROM incidents WHERE is_active = 1 GROUP BY agency')
    by_agency = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute('''
        SELECT description, COUNT(*) as count FROM incidents 
        WHERE is_active = 1 GROUP BY description ORDER BY count DESC LIMIT 10
    ''')
    by_type = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return jsonify({
        'active': active,
        'today': today,
        'total': total,
        'byAgency': by_agency,
        'byType': by_type
    })

@app.route('/api/status')
def get_status():
    meta = meta_get_many([
        'last_update',
        'feed_refreshed_at',
        'feed_refreshed_at_caddo',
        'feed_refreshed_at_lafayette',
        'feed_refreshed_at_batonrouge',
        'feed_refreshed_at_neworleans',
        'feed_refreshed_at_lakecharles',
        'last_scrape_started_at',
        'last_scrape_finished_at',
        'scrape_interval_seconds',
    ])

    interval_seconds = int(meta.get('scrape_interval_seconds') or scrape_interval_seconds)
    now_central = datetime.now(CENTRAL_TZ)

    last_update_dt = _parse_iso_datetime(meta.get('last_update') or last_update)
    last_scrape_finished_dt = _parse_iso_datetime(meta.get('last_scrape_finished_at') or last_scrape_finished_at)
    # Prefer scrape-finished time for "last update" display; otherwise fall back to last_update.
    display_base = last_scrape_finished_dt or last_update_dt

    refreshed_by_source = {
        'caddo': meta.get('feed_refreshed_at_caddo') or feed_refreshed_by_source.get('caddo'),
        'lafayette': meta.get('feed_refreshed_at_lafayette') or feed_refreshed_by_source.get('lafayette'),
        'batonrouge': meta.get('feed_refreshed_at_batonrouge') or feed_refreshed_by_source.get('batonrouge'),
        'neworleans': meta.get('feed_refreshed_at_neworleans') or feed_refreshed_by_source.get('neworleans'),
        'lakecharles': meta.get('feed_refreshed_at_lakecharles') or feed_refreshed_by_source.get('lakecharles'),
    }

    return jsonify({
        # lastUpdate: UTC ISO timestamp of when we last processed/saved a scrape
        'lastUpdate': meta.get('last_update') or last_update,
        # feedRefreshedAt: backwards-compatible Caddo source refresh text.
        'feedRefreshedAt': meta.get('feed_refreshed_at') or feed_refreshed_at,
        'feedRefreshedBySource': refreshed_by_source,
        'lastScrapeStartedAt': meta.get('last_scrape_started_at') or last_scrape_started_at,
        'lastScrapeFinishedAt': meta.get('last_scrape_finished_at') or last_scrape_finished_at,
        'serverNow': datetime.now(timezone.utc).isoformat(),
        # Central-time helpers for the UI (so clients always see Louisiana time, not browser locale)
        'centralTzAbbr': ("CST" if CENTRAL_TZ_IS_FALLBACK else now_central.strftime("%Z")),
        'centralDate': now_central.date().isoformat(),
        'lastUpdateDisplay': _format_central_hms(display_base),
        'lastUpdateTooltip': _format_central_tooltip(display_base),
        # milliseconds (frontend expects ms)
        'scrapeInterval': interval_seconds * 1000,
    })

@app.route('/api/refresh', methods=['POST'])
def force_refresh():
    try:
        # Disabled by default for safety; enable explicitly if you really need it.
        enabled = _env_setting('LOUISIANA911_ENABLE_REFRESH_ENDPOINT', 'CADDO911_ENABLE_REFRESH_ENDPOINT', '0').strip().lower() in ('1', 'true', 'yes')
        if not enabled:
            return jsonify({'error': 'refresh endpoint disabled'}), 403

        global feed_refreshed_at, last_scrape_started_at, last_scrape_finished_at
        last_scrape_started_at = datetime.now(timezone.utc).isoformat()
        meta_set('last_scrape_started_at', last_scrape_started_at)
        total_count = 0
        for source_name, scraper in (
            ('caddo', scrape_caddo_incidents),
            ('batonrouge', scrape_batonrouge_incidents),
            ('lafayette', scrape_lafayette_incidents),
        ):
            incidents, refreshed_at_text = scraper()
            _store_feed_refresh(source_name, refreshed_at_text)
            process_incidents(incidents, source=source_name,
                              allow_empty=source_name in LIVE_SNAPSHOT_SOURCES and incidents is not None)
            total_count += len(incidents or [])
        last_scrape_finished_at = datetime.now(timezone.utc).isoformat()
        meta_set('last_scrape_finished_at', last_scrape_finished_at)
        return jsonify({
            'success': True,
            'count': total_count,
            'feedRefreshedAt': feed_refreshed_at,
            'feedRefreshedBySource': {
                'caddo': feed_refreshed_by_source.get('caddo'),
                'lafayette': feed_refreshed_by_source.get('lafayette'),
                'batonrouge': feed_refreshed_by_source.get('batonrouge'),
            },
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _background_archive():
    """Background task to archive old incidents (runs daily)."""
    try:
        log(f"[{datetime.now().strftime('%H:%M:%S')}] Running daily archive check...")
        result = archive_old_incidents(dry_run=False)
        if result['archived'] > 0:
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Archived {result['archived']} incidents to {len(result['files'])} file(s)")
        else:
            log(f"[{datetime.now().strftime('%H:%M:%S')}] No incidents to archive")
    except Exception as e:
        log(f"[{datetime.now().strftime('%H:%M:%S')}] Archive error: {e}")

def _background_weekly_backup():
    """Background task to create weekly SQLite backup snapshots."""
    try:
        log(f"[{datetime.now().strftime('%H:%M:%S')}] Running weekly backup snapshot...")
        result = create_backup_snapshot(include_archives=True)
        created = len(result["created"])
        removed = len(result["removed"])
        log(
            f"[{datetime.now().strftime('%H:%M:%S')}] Backup complete: "
            f"{created} file(s) created in {result['backup_dir']}"
            + (f", {removed} old backup(s) pruned" if removed else "")
        )
    except Exception as e:
        log(f"[{datetime.now().strftime('%H:%M:%S')}] Weekly backup error: {e}")


def start_collector(
    *,
    interval_seconds: int = SCRAPE_INTERVAL_SECONDS_DEFAULT,
    initial_scrape: bool = True,
    initial_scrape_async: bool = False,
    enable_archive: bool = True,
    enable_weekly_backup: bool = True,
) -> BackgroundScheduler:
    """Start background scraping + DB persistence, return the scheduler."""
    global scrape_interval_seconds
    scrape_interval_seconds = int(interval_seconds)
    meta_set('scrape_interval_seconds', str(scrape_interval_seconds))
    init_db()
    
    if initial_scrape:
        background_scrape()

    scheduler = BackgroundScheduler(timezone=CENTRAL_TZ)
    
    # In web-serving mode, schedule the initial collection immediately in the
    # scheduler thread so Flask can accept requests without waiting on feeds or
    # fallback geocoding. Collector-only modes retain their synchronous start.
    if initial_scrape:
        first_run_time = datetime.now(timezone.utc) + timedelta(seconds=int(scrape_interval_seconds))
    elif initial_scrape_async:
        first_run_time = datetime.now(timezone.utc)
    else:
        first_run_time = datetime.now(timezone.utc) + timedelta(seconds=int(scrape_interval_seconds))
    
    # Scrape job - runs every N seconds (normal mode with geocoding)
    scheduler.add_job(
        background_scrape,
        'interval',
        seconds=int(scrape_interval_seconds),
        next_run_time=first_run_time,
        max_instances=1,
        coalesce=True,
        id='scrape_job',
    )
    
    # Archive job - runs daily at 3:00 AM Central time
    if enable_archive:
        scheduler.add_job(
            _background_archive,
            'cron',
            hour=3,
            minute=0,
            max_instances=1,
            coalesce=True,
            id='archive_job',
        )
        log(f"[LOUISIANA 911] Daily archive scheduled for 3:00 AM Central")

    # Weekly snapshot backup - Sunday night (Central)
    if enable_weekly_backup:
        scheduler.add_job(
            _background_weekly_backup,
            'cron',
            day_of_week='sun',
            hour=23,
            minute=30,
            max_instances=1,
            coalesce=True,
            id='weekly_backup_job',
        )
        log("[LOUISIANA 911] Weekly backup scheduled for Sundays at 11:30 PM Central")
    
    scheduler.start()
    return scheduler

def run_webserver(*, host: str = '0.0.0.0', port: int = 3911) -> None:
    """Run the Flask web UI server (blocking)."""
    log(f"[LOUISIANA 911] Web UI running at http://localhost:{port}")
    log("            Press Ctrl+C to stop")
    app.run(host=host, port=port, debug=False, use_reloader=False)

def _read_key_nonblocking() -> str | None:
    """
    Non-blocking key read.
    - Windows: reads single key presses via msvcrt (no Enter needed)
    - Other: falls back to reading stdin when available (may require Enter)
    """
    if os.name == 'nt':
        try:
            import msvcrt  # type: ignore
            if msvcrt.kbhit():
                return msvcrt.getwch()
        except Exception:
            return None
        return None

    try:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
    except Exception:
        return None
    return None

def run_interactive_mode(
    *,
    host: str = '0.0.0.0',
    port: int = 3911,
    scrape_interval_seconds: int = 60,
    enable_archive: bool = True,
    enable_weekly_backup: bool = True,
) -> None:
    """
    Start in 'event gather' mode (collector only), and allow starting the web UI on demand.
    Press '2' to start the web UI. Press 'q' to quit.
    """
    scheduler = start_collector(
        interval_seconds=scrape_interval_seconds,
        initial_scrape=True,
        enable_archive=enable_archive,
        enable_weekly_backup=enable_weekly_backup,
    )
    web_thread: Thread | None = None

    log("[LOUISIANA 911] Event gather mode is running (collector only).")
    log("          Press '2' to start the web UI, 'q' to quit.")

    try:
        while True:
            key = _read_key_nonblocking()
            if key:
                key = key.strip().lower()
                if key == '2':
                    if web_thread is None or not web_thread.is_alive():
                        log("[LOUISIANA 911] Starting web UI...")
                        web_thread = Thread(target=run_webserver, kwargs={'host': host, 'port': port}, daemon=True)
                        web_thread.start()
                    else:
                        log("[LOUISIANA 911] Web UI already running.")
                elif key in ('q',):
                    break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        log("[LOUISIANA 911] Collector stopped.")

def run_gather_mode(
    *,
    scrape_interval_seconds: int = 60,
    enable_archive: bool = True,
    enable_weekly_backup: bool = True,
) -> None:
    """Collector-only mode. No web server, just keeps filling the DB."""
    scheduler = start_collector(
        interval_seconds=scrape_interval_seconds,
        initial_scrape=True,
        enable_archive=enable_archive,
        enable_weekly_backup=enable_weekly_backup,
    )
    log("[LOUISIANA 911] Collector-only mode running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        log("[LOUISIANA 911] Collector stopped.")

def run_archive(*, dry_run: bool = False) -> None:
    """Run the archive process to move old incidents to monthly archive DBs."""
    init_db()
    result = archive_old_incidents(dry_run=dry_run)
    if result['archived'] == 0:
        log("[ARCHIVE] Nothing to archive.")
    else:
        log(f"[ARCHIVE] Done. Archived {result['archived']} incidents.")

def run_backup(*, include_archives: bool = True) -> None:
    """Run a one-time SQLite backup snapshot."""
    init_db()
    result = create_backup_snapshot(include_archives=include_archives)
    created = result['created']
    skipped = result['skipped']
    removed = result['removed']
    if created:
        log(f"[BACKUP] Created {len(created)} backup file(s) in {result['backup_dir']}")
    else:
        log(f"[BACKUP] No backup files created (check paths/permissions).")
    if skipped:
        log(f"[BACKUP] Skipped missing DBs: {len(skipped)}")
    if removed:
        log(f"[BACKUP] Pruned {len(removed)} old backup file(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Louisiana 911 public incident monitor")
    parser.add_argument("--mode", choices=["serve", "gather", "interactive"], default="serve",
                        help="serve: collector + web UI (default); gather: collector only; interactive: collector only, press 2 to start UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3911)
    parser.add_argument("--interval", type=int, default=60, help="scrape interval in seconds")
    parser.add_argument("--quiet", action="store_true", help="reduce console output (recommended for gather mode)")
    parser.add_argument("--archive", action="store_true", help=f"archive incidents older than {ARCHIVE_AFTER_DAYS} days to monthly DBs, then exit")
    parser.add_argument("--archive-dry-run", action="store_true", help="like --archive but don't move anything (preview only)")
    parser.add_argument("--no-auto-archive", action="store_true", help="disable daily automatic archiving (3 AM Central)")
    parser.add_argument("--backup", action="store_true", help="create a one-time SQLite backup snapshot, then exit")
    parser.add_argument("--backup-main-only", action="store_true", help="when used with --backup, only back up the main DB (skip archive DBs)")
    parser.add_argument("--no-auto-backup", action="store_true", help="disable weekly automatic backup snapshots (Sunday 11:30 PM Central)")
    args = parser.parse_args()

    global QUIET
    QUIET = bool(args.quiet or args.mode == "gather")

    # Handle archive modes (one-time, then exit)
    if args.archive or args.archive_dry_run:
        run_archive(dry_run=args.archive_dry_run)
        return

    # Handle one-time backup mode
    if args.backup:
        run_backup(include_archives=not args.backup_main_only)
        return

    # Store archive preference for start_collector
    enable_archive = not args.no_auto_archive
    enable_weekly_backup = not args.no_auto_backup

    if args.mode == "gather":
        run_gather_mode(
            scrape_interval_seconds=args.interval,
            enable_archive=enable_archive,
            enable_weekly_backup=enable_weekly_backup,
        )
        return

    if args.mode == "interactive":
        run_interactive_mode(
            host=args.host,
            port=args.port,
            scrape_interval_seconds=args.interval,
            enable_archive=enable_archive,
            enable_weekly_backup=enable_weekly_backup,
        )
        return

    # serve mode: collector + web UI immediately
    scheduler = start_collector(
        interval_seconds=args.interval,
        initial_scrape=False,
        initial_scrape_async=True,
        enable_archive=enable_archive,
        enable_weekly_backup=enable_weekly_backup,
    )
    try:
        run_webserver(host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        log("[LOUISIANA 911] Shutting down.")

if __name__ == '__main__':
    main()

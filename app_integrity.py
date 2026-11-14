"""Google Play Integrity checks for the Android app's session endpoint.

Only used when LOUISIANA911_APP_VERIFY=play-integrity. Decoding goes through
Google's server API with a service account that has the Play Integrity API
enabled; set LOUISIANA911_PLAY_INTEGRITY_CREDENTIALS to its JSON key file and
install google-auth (not in requirements.txt until this mode is switched on).
"""
import hashlib
import time

import requests

DECODE_URL = 'https://playintegrity.googleapis.com/v1/{package}:decodeIntegrityToken'
SCOPE = 'https://www.googleapis.com/auth/playintegrity'
MAX_TOKEN_AGE_MS = 10 * 60 * 1000

_credentials = None


def request_hash(challenge: str) -> str:
    """The requestHash the app must pass to Play Integrity for a challenge."""
    return hashlib.sha256(challenge.encode('utf-8')).hexdigest()


def _access_token(credentials_file: str) -> str:
    global _credentials
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    if _credentials is None:
        _credentials = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=[SCOPE]
        )
    if not _credentials.valid:
        _credentials.refresh(Request())
    return _credentials.token


def decode(package: str, integrity_token: str, credentials_file: str) -> dict:
    response = requests.post(
        DECODE_URL.format(package=package),
        json={'integrityToken': integrity_token},
        headers={'Authorization': f'Bearer {_access_token(credentials_file)}'},
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get('tokenPayloadExternal') or {}


def verdict_problem(payload: dict, package: str, challenge: str, now_ms: int | None = None) -> str | None:
    """Return why a decoded verdict is not acceptable, or None when it passes."""
    details = payload.get('requestDetails') or {}
    if details.get('requestPackageName') != package:
        return 'wrong_package'
    if details.get('requestHash') != request_hash(challenge):
        return 'wrong_request'
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        issued_ms = int(details.get('timestampMillis', 0))
    except (TypeError, ValueError):
        return 'stale'
    if abs(now_ms - issued_ms) > MAX_TOKEN_AGE_MS:
        return 'stale'
    app_verdict = (payload.get('appIntegrity') or {}).get('appRecognitionVerdict')
    if app_verdict != 'PLAY_RECOGNIZED':
        return 'unrecognized_app'
    device = (payload.get('deviceIntegrity') or {}).get('deviceRecognitionVerdict') or []
    if not device:
        return 'device_integrity'
    return None

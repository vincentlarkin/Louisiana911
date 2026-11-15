"""Apple App Attest checks for the iOS app's session endpoint.

The iOS counterpart of app_integrity.py. Only used when
LOUISIANA911_APP_VERIFY=play-integrity and the client header says ios/<ver>.

The first time a phone runs the app it makes a key in its Secure Enclave and
sends an attestation: a certificate chain from Apple proving the key belongs
to our app on a real iPhone. We keep that key's public half. After that the
phone signs each challenge with it (an assertion), which is cheap to check.

Everything here follows Apple's "Validating apps that connect to your server":
https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server
The app hashes the server's challenge string with SHA-256 to get clientDataHash.

Needs LOUISIANA911_APPLE_TEAM_ID (the 10-character team ID from the Apple
Developer account) so we can check the attestation is for our bundle ID.
"""
import hashlib
import os
import sqlite3
import struct
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT_CA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certs', 'Apple_App_Attestation_Root_CA.pem')
NONCE_OID = x509.ObjectIdentifier('1.2.840.113635.100.8.2')
AAGUID_PRODUCTION = b'appattest' + b'\x00' * 7
AAGUID_DEVELOPMENT = b'appattestdevelop'


class AttestError(Exception):
    """Carries the short reason code sent back to the app."""


# --- A small CBOR reader: just what attestation and assertion objects use. ---

def _cbor_item(data: bytes, pos: int):
    if pos >= len(data):
        raise AttestError('bad_format')
    initial = data[pos]
    major, info = initial >> 5, initial & 0x1F
    pos += 1
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise AttestError('bad_format')
        value = int.from_bytes(data[pos:pos + size], 'big')
        pos += size
    else:
        raise AttestError('bad_format')  # indefinite lengths, floats: not used here
    if major == 0:
        return value, pos
    if major == 1:
        return -1 - value, pos
    if major in (2, 3):
        if pos + value > len(data):
            raise AttestError('bad_format')
        raw = data[pos:pos + value]
        return (raw if major == 2 else raw.decode('utf-8', 'replace')), pos + value
    if major == 4:
        items = []
        for _ in range(value):
            item, pos = _cbor_item(data, pos)
            items.append(item)
        return items, pos
    if major == 5:
        result = {}
        for _ in range(value):
            key, pos = _cbor_item(data, pos)
            result[key], pos = _cbor_item(data, pos)
        return result, pos
    raise AttestError('bad_format')


def cbor_decode(data: bytes):
    if len(data) > 20000:
        raise AttestError('bad_format')
    value, _ = _cbor_item(data, 0)
    return value


# --- Authenticator data: rpIdHash(32) flags(1) counter(4) [aaguid(16) idLen(2) credId ...] ---

def _auth_data(raw: bytes) -> dict:
    if len(raw) < 37:
        raise AttestError('bad_format')
    parsed = {'rp_id_hash': raw[:32], 'counter': struct.unpack('>I', raw[33:37])[0]}
    if len(raw) >= 55:
        parsed['aaguid'] = raw[37:53]
        id_len = struct.unpack('>H', raw[53:55])[0]
        parsed['credential_id'] = raw[55:55 + id_len]
    return parsed


def app_id_hash(team_id: str, bundle_id: str) -> bytes:
    return hashlib.sha256(f'{team_id}.{bundle_id}'.encode('utf-8')).digest()


def _load_root(root_pem: bytes | None) -> x509.Certificate:
    if root_pem is None:
        with open(ROOT_CA_PATH, 'rb') as handle:
            root_pem = handle.read()
    return x509.load_pem_x509_certificate(root_pem)


def _verify_chain(cert_ders: list, root: x509.Certificate, now: float) -> x509.Certificate:
    """Check leaf <- intermediate <- Apple's App Attest root; return the leaf."""
    if not isinstance(cert_ders, list) or len(cert_ders) < 2:
        raise AttestError('bad_certificate')
    try:
        certs = [x509.load_der_x509_certificate(der) for der in cert_ders]
    except (ValueError, TypeError):
        raise AttestError('bad_certificate')
    chain = certs + [root]
    from datetime import datetime, timezone
    moment = datetime.fromtimestamp(now, timezone.utc)
    for child, parent in zip(chain, chain[1:]):
        if child.issuer != parent.subject:
            raise AttestError('bad_certificate')
        if not (child.not_valid_before_utc <= moment <= child.not_valid_after_utc):
            raise AttestError('bad_certificate')
        try:
            child.verify_directly_issued_by(parent)
        except (ValueError, TypeError, InvalidSignature):
            raise AttestError('bad_certificate')
    return certs[0]


def _certificate_nonce(leaf: x509.Certificate) -> bytes:
    """The nonce Apple puts in the leaf: SEQUENCE { [1] { OCTET STRING nonce } }."""
    try:
        der = leaf.extensions.get_extension_for_oid(NONCE_OID).value.value
    except x509.ExtensionNotFound:
        raise AttestError('bad_certificate')
    # The nonce is always 32 bytes, so it is the last 32 bytes behind an OCTET STRING header.
    if len(der) < 34 or der[-34:-32] != b'\x04\x20':
        raise AttestError('bad_certificate')
    return der[-32:]


def _uncompressed_point(public_key) -> bytes:
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise AttestError('bad_certificate')
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def verify_attestation(
    attestation: bytes,
    key_id: bytes,
    client_data_hash: bytes,
    team_id: str,
    bundle_id: str,
    *,
    allow_development: bool = False,
    root_pem: bytes | None = None,
    now: float | None = None,
) -> bytes:
    """Check a first-run attestation; return the key's public point to store."""
    obj = cbor_decode(attestation)
    if not isinstance(obj, dict) or obj.get('fmt') != 'apple-appattest':
        raise AttestError('bad_format')
    statement = obj.get('attStmt') or {}
    auth_raw = obj.get('authData')
    if not isinstance(statement, dict) or not isinstance(auth_raw, bytes):
        raise AttestError('bad_format')
    leaf = _verify_chain(statement.get('x5c'), _load_root(root_pem), time.time() if now is None else now)
    nonce = hashlib.sha256(auth_raw + client_data_hash).digest()
    if _certificate_nonce(leaf) != nonce:
        raise AttestError('wrong_request')
    point = _uncompressed_point(leaf.public_key())
    if hashlib.sha256(point).digest() != key_id:
        raise AttestError('wrong_key')
    auth = _auth_data(auth_raw)
    if auth['rp_id_hash'] != app_id_hash(team_id, bundle_id):
        raise AttestError('wrong_package')
    if auth['counter'] != 0:
        raise AttestError('bad_format')
    allowed = {AAGUID_PRODUCTION} | ({AAGUID_DEVELOPMENT} if allow_development else set())
    if auth.get('aaguid') not in allowed:
        raise AttestError('wrong_environment')
    if auth.get('credential_id') != key_id:
        raise AttestError('wrong_key')
    return point


def verify_assertion(
    assertion: bytes,
    public_point: bytes,
    stored_counter: int,
    client_data_hash: bytes,
    team_id: str,
    bundle_id: str,
) -> int:
    """Check a signed challenge from an attested key; return the new counter."""
    obj = cbor_decode(assertion)
    if not isinstance(obj, dict):
        raise AttestError('bad_format')
    signature, auth_raw = obj.get('signature'), obj.get('authenticatorData')
    if not isinstance(signature, bytes) or not isinstance(auth_raw, bytes):
        raise AttestError('bad_format')
    nonce = hashlib.sha256(auth_raw + client_data_hash).digest()
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_point)
        key.verify(signature, nonce, ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        raise AttestError('bad_signature')
    auth = _auth_data(auth_raw)
    if auth['rp_id_hash'] != app_id_hash(team_id, bundle_id):
        raise AttestError('wrong_package')
    if auth['counter'] <= stored_counter:
        raise AttestError('replayed')
    return auth['counter']


# --- Key storage (in the main SQLite database). ---

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        'CREATE TABLE IF NOT EXISTS app_attest_keys ('
        'key_id BLOB PRIMARY KEY, public_key BLOB NOT NULL, counter INTEGER NOT NULL, '
        'created_at REAL NOT NULL, last_used REAL NOT NULL)'
    )


def check(
    conn: sqlite3.Connection,
    body: dict,
    challenge: str,
    team_id: str,
    bundle_id: str,
    *,
    allow_development: bool = False,
    root_pem: bytes | None = None,
) -> str | None:
    """Return why the app's App Attest answer is not acceptable, or None when it passes."""
    import base64
    import binascii

    def b64(name):
        value = body.get(name)
        if not isinstance(value, str) or not value or len(value) > 20000:
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None

    key_id = b64('keyId')
    if key_id is None or len(key_id) != 32:
        return 'integrity_required'
    client_data_hash = hashlib.sha256(challenge.encode('utf-8')).digest()
    attestation, assertion = b64('attestation'), b64('assertion')
    now = time.time()
    try:
        _ensure_table(conn)
        if attestation is not None:
            point = verify_attestation(
                attestation, key_id, client_data_hash, team_id, bundle_id,
                allow_development=allow_development, root_pem=root_pem,
            )
            # A key attests only once; a second attestation for it is a replay.
            inserted = conn.execute(
                'INSERT OR IGNORE INTO app_attest_keys VALUES (?, ?, 0, ?, ?)',
                (key_id, point, now, now),
            ).rowcount
            conn.commit()
            return None if inserted else 'replayed'
        if assertion is not None:
            row = conn.execute(
                'SELECT public_key, counter FROM app_attest_keys WHERE key_id = ?', (key_id,)
            ).fetchone()
            if row is None:
                # Unknown key (reinstall, our database reset): the app attests again.
                return 'attestation_required'
            counter = verify_assertion(assertion, row[0], row[1], client_data_hash, team_id, bundle_id)
            updated = conn.execute(
                'UPDATE app_attest_keys SET counter = ?, last_used = ? WHERE key_id = ? AND counter < ?',
                (counter, now, key_id, counter),
            ).rowcount
            conn.commit()
            return None if updated else 'replayed'
    except AttestError as exc:
        return str(exc)
    return 'integrity_required'

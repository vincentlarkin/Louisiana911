"""App Attest checks, using a stand-in for Apple's CA and a software 'Secure Enclave' key."""
import base64
import datetime
import hashlib
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

os.environ.setdefault(
    "LOUISIANA911_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"louisiana911-app-attest-tests-{os.getpid()}.db"),
)
import app
import app_attest

TEAM = 'ABCDE12345'
BUNDLE = 'com.louisiana911.app'
IOS = {'X-Louisiana911-Client': 'ios/0.1.0', 'Host': 'localhost'}


def cbor(value) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, 'big')
    if isinstance(value, int):
        return head(0, value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b''.join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b''.join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


def name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def cert(subject, issuer, public_key, signing_key, *, ca, extensions=()):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(subject)).issuer_name(name(issuer))
        .public_key(public_key).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    for ext in extensions:
        builder = builder.add_extension(ext, critical=False)
    return builder.sign(signing_key, hashes.SHA256())


class FakeApple:
    """Plays Apple's CA plus one iPhone's App Attest key."""

    def __init__(self):
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        self.root = cert('Fake App Attest Root', 'Fake App Attest Root', self.root_key.public_key(), self.root_key, ca=True)
        self.mid_key = ec.generate_private_key(ec.SECP384R1())
        self.mid = cert('Fake App Attest CA 1', 'Fake App Attest Root', self.mid_key.public_key(), self.root_key, ca=True)
        self.root_pem = self.root.public_bytes(serialization.Encoding.PEM)
        self.device_key = ec.generate_private_key(ec.SECP256R1())
        point = self.device_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.key_id = hashlib.sha256(point).digest()
        self.counter = 0

    def attest(self, challenge, *, team=TEAM, aaguid=app_attest.AAGUID_PRODUCTION, nonce_override=None):
        auth = (app_attest.app_id_hash(team, BUNDLE) + b'\x41' + struct.pack('>I', 0)
                + aaguid + struct.pack('>H', 32) + self.key_id + b'\xa0')
        nonce = nonce_override or hashlib.sha256(auth + hashlib.sha256(challenge.encode()).digest()).digest()
        ext = x509.UnrecognizedExtension(app_attest.NONCE_OID, b'\x30\x24\xa1\x22\x04\x20' + nonce)
        leaf = cert(self.key_id.hex(), 'Fake App Attest CA 1', self.device_key.public_key(), self.mid_key,
                    ca=False, extensions=[ext])
        x5c = [c.public_bytes(serialization.Encoding.DER) for c in (leaf, self.mid)]
        return cbor({'fmt': 'apple-appattest', 'attStmt': {'x5c': x5c, 'receipt': b''}, 'authData': auth})

    def assert_(self, challenge, *, counter=None):
        self.counter = self.counter + 1 if counter is None else counter
        auth = app_attest.app_id_hash(TEAM, BUNDLE) + b'\x01' + struct.pack('>I', self.counter)
        nonce = hashlib.sha256(auth + hashlib.sha256(challenge.encode()).digest()).digest()
        signature = self.device_key.sign(nonce, ec.ECDSA(hashes.SHA256()))
        return cbor({'signature': signature, 'authenticatorData': auth})


def b64(raw):
    return base64.b64encode(raw).decode()


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.apple = FakeApple()

    def verify(self, attestation, challenge='c', **kw):
        return app_attest.verify_attestation(
            attestation, self.apple.key_id, hashlib.sha256(challenge.encode()).digest(),
            TEAM, BUNDLE, root_pem=self.apple.root_pem, **kw)

    def test_good_attestation_returns_key(self):
        point = self.verify(self.apple.attest('c'))
        self.assertEqual(self.apple.key_id, hashlib.sha256(point).digest())

    def test_rejects_other_challenge_team_root_and_dev_env(self):
        with self.assertRaisesRegex(app_attest.AttestError, 'wrong_request'):
            self.verify(self.apple.attest('c'), challenge='other')
        with self.assertRaisesRegex(app_attest.AttestError, 'wrong_package'):
            self.verify(self.apple.attest('c', team='ZZZZZ99999'))
        with self.assertRaisesRegex(app_attest.AttestError, 'bad_certificate'):
            app_attest.verify_attestation(
                self.apple.attest('c'), self.apple.key_id, hashlib.sha256(b'c').digest(),
                TEAM, BUNDLE, root_pem=FakeApple().root_pem)
        dev = self.apple.attest('c', aaguid=app_attest.AAGUID_DEVELOPMENT)
        with self.assertRaisesRegex(app_attest.AttestError, 'wrong_environment'):
            self.verify(dev)
        self.verify(dev, allow_development=True)

    def test_garbage_is_bad_format(self):
        for junk in (b'', b'\xff', b'\x5b' + b'\x00' * 8, cbor({'fmt': 'packed'})):
            with self.assertRaises(app_attest.AttestError):
                self.verify(junk)

    def test_real_apple_root_loads(self):
        root = app_attest._load_root(None)
        self.assertIn('Apple App Attestation Root CA', root.subject.rfc4514_string())


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.apple = FakeApple()
        root_file = tempfile.NamedTemporaryFile('wb', suffix='.pem', delete=False)
        root_file.write(self.apple.root_pem)
        root_file.close()
        self.addCleanup(os.unlink, root_file.name)
        for target, value in ((app_attest, {'ROOT_CA_PATH': root_file.name}),
                              (app, {'APP_VERIFY_MODE': 'play-integrity', 'APPLE_TEAM_ID': TEAM})):
            patcher = patch.multiple(target, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def challenge(self):
        response = self.client.post('/api/app/session', headers=IOS, json={})
        self.assertEqual(401, response.status_code)
        return response.get_json()['challenge']

    def session(self, **fields):
        challenge = self.challenge()
        body = {'challenge': challenge, 'platform': 'ios', 'keyId': b64(self.apple.key_id)}
        body.update({k: b64(v(challenge)) for k, v in fields.items()})
        return self.client.post('/api/app/session', headers=IOS, json=body)

    def test_attest_then_assert_gets_verified_tokens(self):
        first = self.session(attestation=self.apple.attest)
        self.assertEqual(200, first.status_code, first.get_json())
        self.assertTrue(first.get_json()['token'].startswith('a2:'))
        again = self.session(assertion=self.apple.assert_)
        self.assertEqual(200, again.status_code, again.get_json())
        token = again.get_json()['token']
        ok = self.client.get('/api/incidents/active?source=all',
                             headers=dict(IOS, **{'X-Louisiana911-App-Token': token}))
        self.assertEqual(200, ok.status_code)

    def test_replays_fail(self):
        self.assertEqual(200, self.session(attestation=self.apple.attest).status_code)
        second = self.session(attestation=self.apple.attest)
        self.assertEqual('replayed', second.get_json()['error'])
        self.assertEqual(200, self.session(assertion=lambda c: self.apple.assert_(c, counter=5)).status_code)
        stale = self.session(assertion=lambda c: self.apple.assert_(c, counter=5))
        self.assertEqual(401, stale.status_code)
        self.assertEqual('replayed', stale.get_json()['error'])

    def test_unknown_key_must_attest(self):
        response = self.session(assertion=self.apple.assert_)
        self.assertEqual(401, response.status_code)
        self.assertEqual('attestation_required', response.get_json()['error'])
        self.assertTrue(response.get_json()['challenge'])

    def test_bad_signature_and_missing_proof(self):
        self.assertEqual(200, self.session(attestation=self.apple.attest).status_code)
        other = FakeApple()
        forged = self.session(assertion=lambda c: other.assert_(c))
        self.assertEqual('bad_signature', forged.get_json()['error'])
        bare = self.client.post('/api/app/session', headers=IOS, json={'challenge': self.challenge()})
        self.assertEqual('integrity_required', bare.get_json()['error'])

    def test_android_challenge_cannot_be_used_by_ios(self):
        android = {'X-Louisiana911-Client': 'android/0.2.0', 'Host': 'localhost'}
        challenge = self.client.post('/api/app/session', headers=android, json={}).get_json()['challenge']
        body = {'challenge': challenge, 'keyId': b64(self.apple.key_id), 'attestation': b64(self.apple.attest(challenge))}
        self.assertEqual('integrity_required',
                         self.client.post('/api/app/session', headers=IOS, json=body).get_json()['error'])

    def test_no_team_id_is_unavailable(self):
        with patch.object(app, 'APPLE_TEAM_ID', ''):
            response = self.session(attestation=self.apple.attest)
        self.assertEqual(503, response.status_code)


if __name__ == '__main__':
    unittest.main()

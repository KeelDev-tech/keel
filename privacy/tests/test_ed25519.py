"""Regression: pure-Python Ed25519 against RFC 8032 vectors + round-trips."""

import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy import _ed25519


class TestEd25519(unittest.TestCase):
    def test_rfc8032_test1_derive(self):
        # Independent anchor: secret -> public must match the RFC vector.
        sec = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(
            _ed25519.derive_public_key(sec).hex(),
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")

    def test_sign_verify_roundtrip(self):
        seed, pub = _ed25519.generate_keypair()
        msg = b"keel privacy decision payload"
        sig = _ed25519.sign(seed, msg)
        self.assertEqual(len(sig), 64)
        self.assertTrue(_ed25519.verify(pub, msg, sig))

    def test_tampered_signature_fails(self):
        seed, pub = _ed25519.generate_keypair()
        sig = bytearray(_ed25519.sign(seed, b"m"))
        sig[63] ^= 1
        self.assertFalse(_ed25519.verify(pub, b"m", bytes(sig)))

    def test_wrong_message_fails(self):
        seed, pub = _ed25519.generate_keypair()
        sig = _ed25519.sign(seed, b"m1")
        self.assertFalse(_ed25519.verify(pub, b"m2", sig))

    def test_wrong_key_fails(self):
        seed, _ = _ed25519.generate_keypair()
        _, other_pub = _ed25519.generate_keypair()
        sig = _ed25519.sign(seed, b"m")
        self.assertFalse(_ed25519.verify(other_pub, b"m", sig))

    def test_malformed_inputs_fail_not_raise(self):
        seed, pub = _ed25519.generate_keypair()
        sig = _ed25519.sign(seed, b"m")
        self.assertFalse(_ed25519.verify(pub, b"m", sig[:32]))
        self.assertFalse(_ed25519.verify(b"short", b"m", sig))
        self.assertFalse(_ed25519.verify(pub, b"m", b"\x00" * 64))


if __name__ == "__main__":
    unittest.main()

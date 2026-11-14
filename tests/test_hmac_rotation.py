"""guide-signup only mints the interest-form token: current key signs it,
the old key never does, an empty key means no token at all. Fake keys only."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)
import hmac, hashlib, base64  # noqa: E402
import lambda_function as lf  # noqa: E402


def sign(key, v):
    return base64.urlsafe_b64encode(hmac.new(key.encode(), str(v).encode(), hashlib.sha256).digest()).decode().rstrip("=")


class Mint(unittest.TestCase):
    def test_current_key_signs(self):
        with mock.patch.object(lf, "HMAC_SECRET", "test-new-hmac"):
            self.assertEqual(lf.guide_link(7), f"{lf.GUIDE_URL}#p=7&t={sign('test-new-hmac', 7)}")
            self.assertNotIn(sign("test-old-hmac", 7), lf.guide_link(7))

    def test_empty_key_no_token(self):
        with mock.patch.object(lf, "HMAC_SECRET", ""):
            self.assertEqual(lf.make_token(7), "")
            self.assertEqual(lf.guide_link(7), lf.GUIDE_URL)

    def test_no_person(self):
        with mock.patch.object(lf, "HMAC_SECRET", "test-new-hmac"):
            self.assertEqual(lf.guide_link(None), lf.GUIDE_URL)


if __name__ == "__main__":
    unittest.main()

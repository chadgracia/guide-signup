"""New guide leads start on Investor Level Hold (7161646); existing people are
never written. Fake data only; no network."""
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
import lambda_function as lf  # noqa: E402


class NewLeadInvestorLevel(unittest.TestCase):
    def test_constant_is_hold(self):
        self.assertEqual(lf.NEW_LEAD_CUSTOM_FIELDS["custom_label_3923758"], 7161646)

    def test_new_lead_posted_with_hold(self):
        calls = []

        def api(method, endpoint, payload=None, jwt=None):
            calls.append((method, endpoint, payload))
            if method == "GET":
                return {"status": 200, "data": {"entries": []}}
            return {"status": 200, "data": {"id": 99}}
        with mock.patch.object(lf, "call_pipeline_api", api), mock.patch.object(lf, "get_jwt", lambda: "test-jwt"):
            self.assertEqual(lf.find_or_create_person("new@x.com", "New", "", "t")[0], 99)
        post = [c for c in calls if c[0] == "POST"]
        self.assertEqual(post[0][2]["person"]["custom_fields"]["custom_label_3923758"], 7161646)

    def test_existing_lead_unchanged(self):
        calls = []

        def api(method, endpoint, payload=None, jwt=None):
            calls.append((method, endpoint, payload))
            return {"status": 200, "data": {"entries": [{"id": 5, "email": "old@x.com"}]}}
        with mock.patch.object(lf, "call_pipeline_api", api), mock.patch.object(lf, "get_jwt", lambda: "test-jwt"):
            self.assertEqual(lf.find_or_create_person("old@x.com", "Old", "", "t"), (5, True))
        self.assertEqual([c[0] for c in calls], ["GET"])


if __name__ == "__main__":
    unittest.main()

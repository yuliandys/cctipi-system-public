#!/usr/bin/env python3
"""Unit tests for Tuya IoT Cloud integration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Data runtime test (auth.json, data/, dll.) ke folder sementara — jangan mengotori repo.
import os as _os  # noqa: E402
_os.environ.setdefault("CCTIPI_STATE_DIR", tempfile.mkdtemp(prefix="cctipi-test-"))

import tuya


class TestTuyaService(unittest.TestCase):
    def test_sign_request_hmac_sha256_deterministic(self) -> None:
        sig = tuya.TuyaService.sign_request(
            "id123", "sec456", "GET", "/v1.0/token?grant_type=1", b"", "1700000000000"
        )
        self.assertEqual(len(sig), 64)
        self.assertEqual(sig, sig.upper())
        sig2 = tuya.TuyaService.sign_request(
            "id123", "sec456", "GET", "/v1.0/token?grant_type=1", b"", "1700000000000"
        )
        self.assertEqual(sig, sig2)

    def test_public_settings_redacts_secret_and_validates_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = tuya.TuyaService(Path(tmp) / "tuya_settings.json")
            pub = svc.update_settings({
                "enabled": True,
                "region": "sg",
                "access_id": "my_id",
                "access_secret": "super_secret",
                "uid": "u123",
                "stream_type": "rtsp",
            })
            self.assertNotIn("access_secret", pub)
            self.assertTrue(pub["has_secret"])
            self.assertTrue(pub["configured"])
            with self.assertRaises(ValueError):
                svc.update_settings({"region": "mars"})

    def test_allocate_stream_caches_and_extracts_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = tuya.TuyaService(Path(tmp) / "tuya_settings.json")
            svc.update_settings({
                "enabled": True,
                "region": "sg",
                "access_id": "aid",
                "access_secret": "sec",
                "device_ids": "dev1",
                "stream_type": "hls",
            })
            calls = []

            def fake_api(method, path, body_obj=None):
                calls.append((method, path, body_obj))
                return {"success": True, "result": {"url": "https://stream.tuyacn.com/live/abc.m3u8"}}

            with mock.patch.object(svc, "_api", side_effect=fake_api):
                url1, st1 = svc.allocate_stream("dev1", "hls")
                url2, st2 = svc.allocate_stream("dev1", "hls")
            self.assertEqual(url1, "https://stream.tuyacn.com/live/abc.m3u8")
            self.assertEqual(url1, url2)
            self.assertEqual(st1, "hls")
            self.assertEqual(len(calls), 1)
            self.assertIn("stream.tuyacn.com", svc.cached_stream_hosts())

    def test_send_ptz_maps_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = tuya.TuyaService(Path(tmp) / "tuya_settings.json")
            sent = []
            with mock.patch.object(svc, "_api", side_effect=lambda m, p, b=None: sent.append((m, p, b)) or {"success": True}):
                svc.send_ptz("dev1", "left")
                svc.send_ptz("dev1", "stop")
            self.assertEqual(sent[0][2]["commands"][0], {"code": "ptz_control", "value": "6"})
            self.assertEqual(sent[1][2]["commands"][0], {"code": "ptz_stop", "value": True})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Regresi audit keamanan lanjutan: SSRF via DNS/redirect, XSS proxy, XFF, limiter, RBAC, hash."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os as _os  # noqa: E402
_os.environ.setdefault("CCTIPI_STATE_DIR", tempfile.mkdtemp(prefix="cctipi-test-"))

import camera_store  # noqa: E402
import net_guard  # noqa: E402
import proxy_routes  # noqa: E402
import server  # noqa: E402
import users  # noqa: E402


def _fake_resolve(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return lambda *_a, **_k: [(family, socket.SOCK_STREAM, 6, "", (ip, 80))]


class TestDnsSsrf(unittest.TestCase):
    def test_hostname_resolving_to_internal_ip_is_rejected(self) -> None:
        for ip in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "100.64.0.9", "::1", "::ffff:127.0.0.1"):
            with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve(ip)):
                with self.assertRaises(net_guard.UnsafeAddressError, msg=ip):
                    net_guard.resolve_safe("127.0.0.1.nip.io", 80, allow_private=False)
                with self.assertRaises(ValueError, msg=ip):
                    camera_store.validate_camera_url("http://127.0.0.1.nip.io/live.m3u8", resolve=True, allow_private=False)

    def test_public_ip_allowed_and_private_only_with_flag(self) -> None:
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("93.184.216.34")):
            net_guard.resolve_safe("contoh.id", 80, allow_private=False)
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("192.168.1.5")):
            net_guard.resolve_safe("kamera.lan", 80, allow_private=True)
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("127.0.0.1")):
            with self.assertRaises(net_guard.UnsafeAddressError):
                net_guard.resolve_safe("kamera.lan", 80, allow_private=True)  # loopback selalu ditolak

    def test_cgnat_literal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            camera_store.validate_stream_host("100.64.0.1", allow_private=False)

    def test_safe_connection_checks_ip_before_connect(self) -> None:
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("127.0.0.1")), \
                mock.patch("net_guard.socket.socket") as sock_cls:
            with self.assertRaises(net_guard.UnsafeAddressError):
                net_guard.SafeHTTPConnection("evil.example", 80, timeout=2).connect()
            sock_cls.assert_not_called()

    def test_safe_opener_blocks_redirect_to_internal_and_non_http(self) -> None:
        handler = net_guard._SafeRedirectHandler()
        req = urllib.request.Request("http://contoh.id/a")
        with self.assertRaises(net_guard.UnsafeAddressError):
            handler.redirect_request(req, None, 302, "Found", {}, "ftp://contoh.id/x")
        # Redirect ke host internal ditolak oleh koneksi hop berikutnya:
        opener = net_guard.build_safe_opener()
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("169.254.169.254")):
            with self.assertRaises(Exception) as ctx:
                opener.open("http://metadata.evil.example/latest", timeout=2)
        self.assertIn("SSRF", str(ctx.exception))

    def test_proxy_upstream_resolving_to_loopback_refused(self) -> None:
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("127.0.0.1")):
            with self.assertRaises(net_guard.UnsafeAddressError):
                proxy_routes.fetch_upstream_pooled("http://stream.kuduskab.go.id/x.m3u8", {})

    def test_ffmpeg_input_rejected_for_internal_host(self) -> None:
        import ai
        with mock.patch("net_guard.socket.getaddrinfo", _fake_resolve("127.0.0.1")):
            self.assertFalse(ai.ffmpeg_input_allowed("rtsp://kamera.evil.example/live"))
        with mock.patch("net_guard.socket.getaddrinfo", side_effect=socket.gaierror("offline")):
            self.assertTrue(ai.ffmpeg_input_allowed("rtsp://kamera.offline.example/live"))


class TestProxyContentType(unittest.TestCase):
    def test_active_content_types_are_neutralised(self) -> None:
        for ct in ("text/html; charset=utf-8", "image/svg+xml", "application/xhtml+xml", "text/javascript", ""):
            self.assertEqual(proxy_routes.safe_content_type(ct), "application/octet-stream", ct)
        for ct in ("video/mp2t", "application/vnd.apple.mpegurl", "multipart/x-mixed-replace; boundary=f", "image/jpeg"):
            self.assertEqual(proxy_routes.safe_content_type(ct), ct)


class _FakeHandler:
    def __init__(self, headers: dict, peer: str = "127.0.0.1"):
        self.headers = headers
        self.client_address = (peer, 5555)
        self.sent = None

    def get_ctx(self):
        return None

    def _send_json(self, payload, status=200):
        self.sent = payload


class TestServerHardening(unittest.TestCase):
    def test_client_ip_uses_rightmost_forwarded_entry(self) -> None:
        h = _FakeHandler({"X-Forwarded-For": "6.6.6.6, 203.0.113.7"})
        with mock.patch.object(server, "TRUST_PROXY", True), mock.patch.object(server, "TRUST_PROXY_HOPS", 1):
            self.assertEqual(server.CctipiHandler.client_ip(h), "203.0.113.7")
        with mock.patch.object(server, "TRUST_PROXY", False):
            self.assertEqual(server.CctipiHandler.client_ip(h), "127.0.0.1")

    def test_unauthenticated_health_is_minimal(self) -> None:
        h = _FakeHandler({})
        with mock.patch.object(server, "build_health_report") as full:
            server.handle_health(h)
            full.assert_not_called()
        self.assertEqual(h.sent, {"ok": True, "status": "alive"})

    def test_cross_site_fetch_metadata_without_origin_rejected(self) -> None:
        h = _FakeHandler({"Sec-Fetch-Site": "cross-site"})
        self.assertTrue(server.CctipiHandler._cross_origin(h))
        self.assertFalse(server.CctipiHandler._cross_origin(_FakeHandler({"Sec-Fetch-Site": "same-origin"})))
        self.assertFalse(server.CctipiHandler._cross_origin(_FakeHandler({})))


class TestLoginLimiter(unittest.TestCase):
    def test_attacker_from_one_ip_cannot_lock_out_user_globally(self) -> None:
        lim = server.LoginLimiter()
        attacker = ["ip:6.6.6.6", "userip:admin|6.6.6.6", "user:admin"]
        for _ in range(lim.MAX_FAILS + 2):
            lim.fail(attacker)
        self.assertGreater(lim.retry_after(attacker), 0)
        self.assertEqual(lim.retry_after(["ip:10.0.0.9", "userip:admin|10.0.0.9", "user:admin"]), 0)

    def test_tracking_tables_are_bounded(self) -> None:
        lim = server.LoginLimiter()
        lim.MAX_TRACKED_KEYS = 50
        with mock.patch("auth_routes.time.time", return_value=1000.0):
            for i in range(60):
                lim.fail([f"user:acak{i}"])
        with mock.patch("auth_routes.time.time", return_value=1000.0 + lim.WINDOW + 1):
            lim.fail(["user:baru"])
        self.assertLessEqual(len(lim._fails), 2)


class TestRbacEscalation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = users.UserStore(Path(self.tmp.name) / "auth.json")
        self.store.save_role({"name": "HRD", "permissions": ["users.manage", "jepretan.view"]})
        self.store.create_user({"username": "hrd", "password": "hrdhrd12345", "role": "hrd"})
        self.store.create_user({"username": "bos", "password": "bosbos12345", "role": "admin"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_non_admin_manager_cannot_escalate(self) -> None:
        with self.assertRaises(users.UserError):
            self.store.create_user({"username": "xadm", "password": "xadmxadm123", "role": "admin"}, actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.create_user({"username": "xopr", "password": "xoprxopr123", "role": "operator"}, actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.update_user("bos", {"password": "diambilalih123"}, actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.delete_user("bos", actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.save_role({"name": "Super", "permissions": ["settings.system"]}, actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.save_role({"id": "hrd", "name": "HRD", "permissions": []}, actor="hrd")
        with self.assertRaises(users.UserError):
            self.store.update_user("hrd", {"regions": []}, actor="hrd")
        # Yang masih dalam batas tetap boleh:
        self.store.create_user({"username": "vw1", "password": "vw1vw1vw1vw1", "role": "viewer"}, actor="hrd")
        self.store.update_user("vw1", {"password": "v1baru12345"}, actor="hrd")
        # Admin tetap bebas:
        self.store.create_user({"username": "adm2", "password": "adm2adm2adm2", "role": "admin"}, actor="bos")


class TestPasswordHashUpgrade(unittest.TestCase):
    def test_legacy_hash_verifies_and_upgrades_on_login(self) -> None:
        legacy_salt = "abcd" * 8
        legacy = f"{legacy_salt}${users._pbkdf2('rahasia12345', legacy_salt, users.LEGACY_PBKDF2_ITERATIONS)}"
        self.assertTrue(users.verify_password("rahasia12345", legacy))
        self.assertTrue(users.needs_rehash(legacy))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "auth.json"
            path.write_text(json.dumps({"username": "andy", "passwordHash": legacy}))
            store = users.UserStore(path)
            self.assertIsNotNone(store.authenticate("andy", "rahasia12345"))
            stored = json.loads(path.read_text())["users"][0]["passwordHash"]
            self.assertTrue(stored.startswith(f"pbkdf2_sha256${users.PBKDF2_ITERATIONS}$"))
            self.assertFalse(users.needs_rehash(stored))
            self.assertIsNotNone(store.authenticate("andy", "rahasia12345"))
            self.assertIsNone(store.authenticate("andy", "salah123456"))


class TestRunAsScript(unittest.TestCase):
    """Regresi: `python server.py` (modul __main__) dulu membuat semua handler mendapat None."""

    def test_login_works_when_started_as_script(self) -> None:
        import subprocess
        import time
        import urllib.error

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as state:
            env = {**_os.environ, "CCTIPI_STATE_DIR": state, "PORT": str(port), "HOST": "127.0.0.1"}
            proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(ROOT), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                            r.read()
                        break
                    except (urllib.error.URLError, ConnectionError):
                        time.sleep(0.1)
                req = urllib.request.Request(
                    base + "/api/login", method="POST", headers={"Content-Type": "application/json"},
                    data=json.dumps({"username": "admin", "password": users.DEFAULT_PASSWORD}).encode(),
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.assertEqual(r.status, 200)
                    self.assertTrue(json.loads(r.read())["ok"])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()

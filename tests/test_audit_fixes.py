#!/usr/bin/env python3
"""Unit & integration tests untuk seluruh perbaikan temuan audit sistem (P0, P1, P2)."""

from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Data runtime test (auth.json, data/, dll.) ke folder sementara — jangan mengotori repo.
import os as _os  # noqa: E402
_os.environ.setdefault("CCTIPI_STATE_DIR", tempfile.mkdtemp(prefix="cctipi-test-"))

import ai
import audit as audit_mod
import camera_store
import live_ai
import recorder
import recording_routes
import scrapers
import server
import system_health
import users as users_mod
from storage_utils import redact_sensitive_text


class _HttpTestMixin:
    """Utilitas server HTTP lokal terisolasi untuk pengujian end-to-end."""

    def start_isolated_server(self, *, extra_patches=()):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.data_dir = d
        self.sessions = server.SessionStore(d / "sessions.json")
        self.users = users_mod.UserStore(d / "auth.json", on_revoke=self.sessions.revoke_user)
        self.users.reload()
        self.audit = audit_mod.AuditLog(d / "audit.db")
        self.login_lim = server.LoginLimiter()
        self.act_lim = server.SlidingWindowLimiter()
        self._patches = [
            mock.patch.object(server, "DATA_DIR", d),
            mock.patch.object(server, "PREFS_DIR", d / "prefs"),
            mock.patch.object(server, "SESSIONS", self.sessions),
            mock.patch.object(server, "USERS", self.users),
            mock.patch.object(server, "AUDIT", self.audit),
            mock.patch.object(server, "LOGIN_LIMITER", self.login_lim),
            mock.patch.object(server, "ACTION_LIMITER", self.act_lim),
            mock.patch.object(camera_store, "MANUAL_CAMERAS_FILE", d / "manual_cameras.json"),
            mock.patch.object(camera_store, "CUSTOM_SOURCES_FILE", d / "sources.json"),
            mock.patch.object(camera_store, "CAMERAS_CACHE_FILE", d / "cameras_cache.json"),
            *extra_patches,
        ]
        try:
            for p in self._patches:
                p.start()
        except Exception:
            for p in reversed(self._patches):
                try:
                    p.stop()
                except Exception:
                    pass
            self.tmp.cleanup()
            raise
        self.httpd = server.FastThreadingHTTPServer(("127.0.0.1", 0), server.CctipiHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop_isolated_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def make_client(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar

    def request_raw(self, opener, method: str, path: str, body=None, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        hdrs = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with opener.open(req, timeout=5) as r:
                raw = r.read()
                ct = r.headers.get_content_type()
                return r.status, dict(r.headers), (json.loads(raw) if ct == "application/json" else raw)
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
                ct = e.headers.get_content_type() if e.headers else ""
                parsed = json.loads(raw or b"{}") if ct == "application/json" else raw
                return e.code, dict(e.headers), parsed


# ==============================================================================
# 1. P0 #1 & #2: Paksa Ganti Password, Revokasi Sesi, Non-Loopback Block & HTTPS Cookie
# ==============================================================================
class TestAuthStartupAndHttps(unittest.TestCase, _HttpTestMixin):
    def setUp(self) -> None:
        self.start_isolated_server(extra_patches=[
            mock.patch("server.get_cameras", return_value={"cameras": [], "apiStatus": {}}),
        ])

    def tearDown(self) -> None:
        self.stop_isolated_server()

    def test_first_login_blocks_operational_api_and_password_change_revokes_other_sessions(self) -> None:
        dev1, _ = self.make_client()
        dev2, _ = self.make_client()

        st1, _, d1 = self.request_raw(dev1, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
        st2, _, _ = self.request_raw(dev2, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
        self.assertEqual((st1, st2), (200, 200))
        self.assertTrue(d1["mustChange"])

        st_me, _, me = self.request_raw(dev1, "GET", "/api/auth/me")
        self.assertEqual(st_me, 200)
        self.assertTrue(me["mustChange"])

        st_cam, _, err_cam = self.request_raw(dev1, "GET", "/api/cameras")
        self.assertEqual(st_cam, 403)
        self.assertTrue(err_cam.get("mustChange"))

        st_same, _, _ = self.request_raw(dev1, "POST", "/api/auth/password", {
            "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": users_mod.DEFAULT_PASSWORD,
        })
        self.assertEqual(st_same, 400)

        st_pw, _, _ = self.request_raw(dev1, "POST", "/api/auth/password", {
            "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": "PasswordKuatBaru99!",
        })
        self.assertEqual(st_pw, 200)
        self.assertEqual(self.request_raw(dev1, "GET", "/api/cameras")[0], 200)
        self.assertEqual(self.request_raw(dev2, "GET", "/api/cameras")[0], 401)

    def test_startup_validation_blocks_non_loopback_with_default_password(self) -> None:
        with self.assertRaises(system_health.ConfigError):
            system_health.validate_startup_config(
                host="0.0.0.0", port_raw="9999", users_store=self.users,
                data_dir=self.data_dir, sources=[], ai_service=None,
            )
        cfg_local = system_health.validate_startup_config(
            host="127.0.0.1", port_raw="9999", users_store=self.users,
            data_dir=self.data_dir, sources=[], ai_service=None,
        )
        self.assertTrue(cfg_local["warnings"])

        self.users.change_own("admin", users_mod.DEFAULT_PASSWORD, "admin", "PasswordAman999!")
        cfg_lan = system_health.validate_startup_config(
            host="0.0.0.0", port_raw="9999", users_store=self.users,
            data_dir=self.data_dir, sources=[], ai_service=None,
        )
        self.assertEqual(cfg_lan["host"], "0.0.0.0")

    def test_https_proxy_activates_secure_cookie_and_hsts_automatically(self) -> None:
        plain, _ = self.make_client()
        st, hdrs, _ = self.request_raw(plain, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
        self.assertEqual(st, 200)
        self.assertNotIn("Secure", hdrs.get("Set-Cookie", ""))
        self.assertNotIn("Strict-Transport-Security", hdrs)

        with mock.patch.object(server, "TRUST_PROXY", True):
            opener = urllib.request.build_opener()
            st, hdrs, _ = self.request_raw(
                opener, "POST", "/api/login",
                {"username": "admin", "password": users_mod.DEFAULT_PASSWORD},
                headers={"X-Forwarded-Proto": "https"},
            )
            self.assertEqual(st, 200)
            self.assertIn("; Secure", hdrs.get("Set-Cookie", ""))
            self.assertIn("Strict-Transport-Security", hdrs)


# ==============================================================================
# 2. P0 #3, P1 #5, P1 #6: Validasi SSRF Allowlist, Redirect Jahat E2E & Rate Limit
# ==============================================================================
class TestSsrfProxyRedirectAndRateLimit(unittest.TestCase, _HttpTestMixin):
    def setUp(self) -> None:
        self.start_isolated_server()
        self.admin, _ = self.make_client()
        self.request_raw(self.admin, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
        self.request_raw(self.admin, "POST", "/api/auth/password", {
            "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": "AdminStrongPassword1!",
        })

    def tearDown(self) -> None:
        self.stop_isolated_server()

    def test_manual_camera_and_onvif_reject_ssrf_targets_and_log_new_hosts(self) -> None:
        for bad_url in (
            "http://127.0.0.1:9999/api/health",
            "http://localhost/live.m3u8",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.50/stream.m3u8",
            "http://bad_host_name!/stream.m3u8",
        ):
            st, _, _ = self.request_raw(self.admin, "POST", "/api/manual-cameras", {
                "name": "Uji SSRF", "url": bad_url, "type": "hls",
            })
            self.assertEqual(st, 400, f"Seharusnya menolak URL berbahaya: {bad_url}")

        st_onvif, _, _ = self.request_raw(self.admin, "POST", "/api/onvif/discover", {"url": "http://127.0.0.1:8000"})
        self.assertEqual(st_onvif, 400)

        with mock.patch.object(camera_store, "ALLOW_PRIVATE_CAMERA_HOSTS", True):
            self.assertEqual(self.request_raw(self.admin, "POST", "/api/manual-cameras", {
                "name": "Loopback Tetap Ditolak", "url": "http://127.0.0.1/live.m3u8", "type": "hls",
            })[0], 400)
            self.assertEqual(self.request_raw(self.admin, "POST", "/api/manual-cameras", {
                "name": "Metadata Tetap Ditolak", "url": "http://169.254.169.254/live.m3u8", "type": "hls",
            })[0], 400)
            with mock.patch("server._trigger_bg_refresh"):
                st_lan, _, _ = self.request_raw(self.admin, "POST", "/api/manual-cameras", {
                    "name": "Kamera LAN", "url": "rtsp://192.168.10.25:554/live", "type": "rtsp",
                })
            self.assertEqual(st_lan, 200)

        with mock.patch("server._trigger_bg_refresh"):
            st_pub, _, _ = self.request_raw(self.admin, "POST", "/api/manual-cameras", {
                "name": "Publik Baru", "url": "https://cctv-baru.contoh.go.id/live/index.m3u8", "type": "hls",
            })
        self.assertEqual(st_pub, 200)
        allow_logs = self.audit.query(action="allowlist.")
        self.assertTrue(any("cctv-baru.contoh.go.id" in it["target"] for it in allow_logs))

    def test_proxy_blocks_malicious_upstream_redirect_end_to_end(self) -> None:
        secret_hit = {"called": False}

        class EvilUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                if self.path == "/secret":
                    secret_hit["called"] = True
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"RAHASIA_INTERNAL")
                    return
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/secret")
                self.end_headers()

        up = ThreadingHTTPServer(("127.0.0.1", 0), EvilUpstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        up_port = up.server_address[1]
        try:
            def allow_only_public(u: str) -> bool:
                return urllib.parse.urlparse(u).path == "/public.m3u8"

            with mock.patch("server.is_allowed_stream_url", side_effect=allow_only_public):
                q = urllib.parse.quote(f"http://127.0.0.1:{up_port}/public.m3u8", safe="")
                st, _, body = self.request_raw(self.admin, "GET", f"/api/proxy?url={q}")
            self.assertEqual(st, 502)
            self.assertFalse(secret_hit["called"])
            self.assertNotIn("RAHASIA_INTERNAL", json.dumps(body))
        finally:
            up.shutdown()
            up.server_close()

    def test_sensitive_endpoint_rate_limiter_and_xff_spoof_protection(self) -> None:
        with mock.patch("server.rate_limit_rule_for", return_value=("test_del", 2, 60)):
            s1, _, _ = self.request_raw(self.admin, "DELETE", "/api/jepretan/tidak-ada", headers={"X-Forwarded-For": "1.1.1.1"})
            s2, _, _ = self.request_raw(self.admin, "DELETE", "/api/jepretan/tidak-ada", headers={"X-Forwarded-For": "2.2.2.2"})
            s3, hdrs3, d3 = self.request_raw(self.admin, "DELETE", "/api/jepretan/tidak-ada", headers={"X-Forwarded-For": "3.3.3.3"})
            self.assertNotEqual(s1, 429)
            self.assertNotEqual(s2, 429)
            self.assertEqual(s3, 429)
            self.assertIn("Retry-After", hdrs3)
            self.assertGreaterEqual(d3["retryAfter"], 1)


# ==============================================================================
# 3. P1 #8: Seluruh 10 Scraper Daerah + Fallback Cache Saat HTML Upstream Berubah/Kosong
# ==============================================================================
class TestAllTenRegionalScrapersAndFallback(unittest.TestCase):
    def test_all_ten_regional_parsers_extract_valid_cameras(self) -> None:
        # 1. Jepara (HTMLParser <select id="pilih-cctv">)
        jepara_html = """
        <select id="pilih-cctv">
          <option value="">-- Pilih --</option>
          <option value="https://stream-cctv.jepara.go.id/alun.m3u8">Alun-Alun Jepara</option>
        </select>
        """
        jepara = scrapers.parse_cameras_from_html(jepara_html)
        self.assertEqual(len(jepara), 1)
        self.assertEqual((jepara[0]["name"], jepara[0]["folder"]), ("Alun-Alun Jepara", "Jepara"))

        # 2. Kudus (JSON API)
        kudus = scrapers.parse_kudus_json({
            "data": [{"nama": "Simpang Tujuh", "lat": "-6.8048", "long": "110.8405",
                      "url": "https://stream.kuduskab.go.id/simpang7.m3u8"}]
        })
        self.assertEqual(len(kudus), 1)
        self.assertAlmostEqual(kudus[0]["lat"], -6.8048)

        # 3. Demak (Balanced JSON ytInitialData)
        demak_html = (
            '<script>var ytInitialData = {"contents":{"videoRenderer":'
            '{"videoId":"dQw4w9WgXcQ","title":{"simpleText":"🔴 [LIVE CCTV] Alun Demak by DISHUB KAB. DEMAK"}}}};</script>'
        )
        demak = scrapers.parse_demak_html(demak_html)
        self.assertEqual(len(demak), 1)
        self.assertEqual((demak[0]["name"], demak[0]["type"]), ("Alun Demak", "youtube"))
        self.assertIn("dQw4w9WgXcQ", demak[0]["url"])

        # 4. Jombang (HTMLParser + cctvData)
        jombang_html = (
            '<div class="cam-card" data-cam-name="Ringin Contong" '
            'data-url="https://cctv.jombangkab.go.id/zm/cgi-bin/nph-zms?monitor=1&amp;token=xyz"></div>'
        )
        jombang = scrapers.parse_jombang_html(jombang_html)
        self.assertEqual(len(jombang), 1)
        self.assertEqual((jombang[0]["name"], jombang[0]["type"]), ("Ringin Contong", "mjpeg"))

        # 5. Tol Kertosono-Mojokerto (HTMLParser <source src="...m3u8"> + heading)
        toll_html = """
        <div class="card-body"><h6>KM 675 A</h6>
        <video><source src="https://api-cctv.margaharjaya.co.id/stream/km675.m3u8"></video></div>
        """
        toll = scrapers.parse_toll_html(toll_html)
        self.assertEqual(len(toll), 1)
        self.assertEqual(toll[0]["name"], "KM 675 A")

        # 6. Jogja (JSON API)
        jogja = scrapers.parse_jogja_json({
            "data": [{"cctv_title": "Malioboro Utara", "cctv_status": "0",
                      "cctv_link": "https://cctvjss.jogjakota.go.id/malioboro.m3u8",
                      "cctv_latitude": "-7.7925", "cctv_longitude": "110.3658"}]
        })
        self.assertEqual(len(jogja), 1)
        self.assertEqual(jogja[0]["folder"], "Jogja")

        # 7. Semarang (Balanced JSON embedded array)
        semarang_html = (
            '<script>var list = [{"cctv_id":1,"owner_name":"Tugu Muda","lat":"-6.9844","lng":"110.4092",'
            '"links":[{"name":"Tugu Muda","url":"https://livepantau.semarangkota.go.id/hls/tugu.m3u8"}]}];</script>'
        )
        semarang = scrapers.parse_semarang_html(semarang_html)
        self.assertEqual(len(semarang), 1)
        self.assertEqual(semarang[0]["name"], "Tugu Muda")

        # 8. Pati (JS object list + konversi restreamer .html -> .m3u8)
        pati_html = (
            '<script>const cams = [{ nama: "Simpang Lima Pati", '
            'url: "https://streaming.patikab.go.id/1234abcd-5678.html" }];</script>'
        )
        pati = scrapers.parse_pati_html(pati_html)
        self.assertGreaterEqual(len(pati), 1)
        self.assertTrue(any(c["name"] == "Simpang Lima Pati" and c["url"].endswith(".m3u8") for c in pati))

        # 9. Rembang (JSON API)
        rembang = scrapers.parse_rembang_json({
            "data": [{
                "name": "Tugu Adipura", "is_active": 1,
                "stream_url": "https://cctv.rembangkab.go.id/adipura.m3u8",
                "coordinate": "-6.708, 111.342",
            }]
        })
        self.assertEqual(len(rembang), 1)
        self.assertEqual(rembang[0]["name"], "Tugu Adipura")

        # 10. Solo (HTMLParser data-url + data-name)
        solo_html = (
            '<div class="cam" data-url="https://surakarta.atcsindonesia.info:8080/live/gladag.flv" '
            'data-name="Simpang Gladag"></div>'
        )
        solo = scrapers.parse_solo_cameras_from_html(solo_html)
        self.assertEqual(len(solo), 1)
        self.assertEqual((solo[0]["name"], solo[0]["type"]), ("Simpang Gladag", "flv"))

    def test_broken_upstream_html_triggers_warning_and_uses_last_region_cache(self) -> None:
        self.assertEqual(scrapers.parse_pati_html("<html><body>Portal sedang maintenance</body></html>"), [])
        self.assertEqual(scrapers.parse_semarang_html("<html><body>Struktur berubah</body></html>"), [])
        self.assertEqual(scrapers.parse_solo_cameras_from_html("<html></html>"), [])

        cached_pati = [{"id": 1, "name": "Alun Pati Lama", "url": "https://streaming.patikab.go.id/a.m3u8", "provider": "pati", "folder": "Pati"}]
        logs: list[str] = []
        with mock.patch("server.load_sources_config", return_value=[{"folder": "Pati", "title": "Pati"}]), \
             mock.patch("server.load_region_cache", return_value={"Pati": cached_pati}), \
             mock.patch("server.save_region_cache"), \
             mock.patch("server.load_manual_cameras", return_value=[]), \
             mock.patch.dict("server.BUILTIN_FETCHERS", {"Pati": lambda: []}), \
             mock.patch("camera_store.log_event", side_effect=lambda lvl, comp, msg, **kw: logs.append(f"{lvl}:{comp}:{msg}")):
            res = server._refresh_cameras_sync()

        self.assertFalse(res["apiStatus"]["Pati"]["online"])
        self.assertEqual([c["name"] for c in res["cameras"]], ["Alun Pati Lama"])
        self.assertTrue(any("WARNING:scraper:" in ln and "Pati" in ln for ln in logs))


# ==============================================================================
# 4. P1 #5 & P2 #10: Kuota Rekaman, HTTP Range (200/206/416), Disk Full & Crash Recovery
# ==============================================================================
class TestRecorderQuotaRangeAndRecovery(unittest.TestCase, _HttpTestMixin):
    def test_parse_byte_range_and_http_range_playback_200_206_416(self) -> None:
        self.assertEqual(recording_routes.parse_byte_range("", 1000), (0, 999, False))
        self.assertEqual(recording_routes.parse_byte_range("bytes=0-99", 1000), (0, 99, True))
        self.assertEqual(recording_routes.parse_byte_range("bytes=900-", 1000), (900, 999, True))
        self.assertEqual(recording_routes.parse_byte_range("bytes=-100", 1000), (900, 999, True))
        self.assertIsNone(recording_routes.parse_byte_range("bytes=1000-1200", 1000))
        self.assertIsNone(recording_routes.parse_byte_range("bytes=500-100", 1000))

        with tempfile.TemporaryDirectory() as rec_tmp:
            rec_dir = Path(rec_tmp)
            with mock.patch.object(recorder, "DATA_DIR", rec_dir), \
                 mock.patch.object(recorder, "REKAMAN_DIR", rec_dir / "rekaman"), \
                 mock.patch.object(recorder, "DB_FILE", rec_dir / "rekaman.db"):
                svc = recorder.RecorderService(lambda: [])
                sample_mp4 = rec_dir / "rekaman" / "gerbang" / "2026-11-27" / "seg01.mp4"
                sample_mp4.parent.mkdir(parents=True, exist_ok=True)
                payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100  # 3600 byte
                sample_mp4.write_bytes(payload)
                svc._insert_recording({
                    "id": "rec_test_1", "camera_id": "m:g", "camera_name": "Gerbang",
                    "started_at": 1700000000.0, "duration_s": 60.0, "date": "2026-11-27",
                    "size_bytes": len(payload), "mode": "berkelanjutan",
                    "path": "rekaman/gerbang/2026-11-27/seg01.mp4",
                })

                self.start_isolated_server(extra_patches=[
                    mock.patch.object(server, "recorder_service", svc),
                    mock.patch("server.get_cameras", return_value={"cameras": [], "apiStatus": {}}),
                ])
                try:
                    admin, _ = self.make_client()
                    self.request_raw(admin, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
                    self.request_raw(admin, "POST", "/api/auth/password", {
                        "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": "AdminRangePassword1!",
                    })

                    st200, h200, b200 = self.request_raw(admin, "GET", "/api/rekaman/rec_test_1/file")
                    self.assertEqual(st200, 200)
                    self.assertEqual(h200.get("Accept-Ranges"), "bytes")
                    self.assertEqual(b200, payload)

                    st206, h206, b206 = self.request_raw(admin, "GET", "/api/rekaman/rec_test_1/file", headers={"Range": "bytes=10-29"})
                    self.assertEqual(st206, 206)
                    self.assertEqual(h206.get("Content-Range"), f"bytes 10-29/{len(payload)}")
                    self.assertEqual(b206, payload[10:30])

                    st416, h416, _ = self.request_raw(admin, "GET", "/api/rekaman/rec_test_1/file", headers={"Range": f"bytes={len(payload) + 10}-{len(payload) + 50}"})
                    self.assertEqual(st416, 416)
                    self.assertEqual(h416.get("Content-Range"), f"bytes */{len(payload)}")
                finally:
                    self.stop_isolated_server()

    def test_recording_quota_enforced_and_disk_full_or_ffmpeg_crash_recovers_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as d_raw:
            d = Path(d_raw)
            with mock.patch.object(recorder, "DATA_DIR", d), \
                 mock.patch.object(recorder, "REKAMAN_DIR", d / "rekaman"), \
                 mock.patch.object(recorder, "SETTINGS_FILE", d / "recorder_settings.json"), \
                 mock.patch.object(recorder, "DB_FILE", d / "rekaman.db"):
                svc = recorder.RecorderService(lambda: [])
                svc._settings["quota_gb"] = 2000 / (1024 ** 3)
                sub = d / "rekaman" / "cam" / "2026-11-27"
                sub.mkdir(parents=True, exist_ok=True)
                for idx in range(3):
                    fp = sub / f"f{idx}.mp4"
                    fp.write_bytes(b"x" * 1000)
                    svc._insert_recording({
                        "id": f"id{idx}", "camera_id": "m:1", "camera_name": "Cam",
                        "started_at": time.time() - 100 + idx * 10, "duration_s": 10.0, "date": "2026-11-27",
                        "size_bytes": 1000, "mode": "berkelanjutan",
                        "path": f"rekaman/cam/2026-11-27/f{idx}.mp4",
                    })
                summary = svc.cleanup_now()
                self.assertGreaterEqual(summary["quota_removed"], 1)
                self.assertLessEqual(svc.status()["total_size_bytes"], 2000)
                self.assertFalse((sub / "f0.mp4").exists())

                # Skenario 1: Disk penuh saat preflight -> menolak merekam & memicu cleanup tanpa crash
                out_preflight = sub / "preflight.mp4"
                fake_usage = mock.Mock(total=10_000_000_000, used=9_999_000_000, free=1024)
                with mock.patch("recorder.shutil.disk_usage", return_value=fake_usage):
                    ok, info = svc._run_ffmpeg_record(
                        {"name": "Gerbang", "url": "rtsp://admin:Rahasia99@10.0.0.2/live"},
                        out_preflight, 5, tag="test:preflight",
                    )
                self.assertFalse(ok)
                self.assertIn("disk", info.lower())

                # Skenario 2: ffmpeg menggantung melewati timeout watchdog & menulis ENOSPC + rahasia di stderr
                class HungDiskFullProc:
                    def __init__(self):
                        self.returncode = 1
                        self.killed = False
                        self._calls = 0

                    def poll(self):
                        return None if not self.killed else -9

                    def communicate(self, timeout=None):
                        self._calls += 1
                        if self._calls == 1 and timeout is not None:
                            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
                        return b"", b"rtsp://admin:SuperSecret@10.0.0.5/live: No space left on device\n"

                    def kill(self):
                        self.killed = True

                proc_inst = HungDiskFullProc()
                out_crash = sub / "crash.mp4"
                out_crash.write_bytes(b"partial")
                with mock.patch("camera_store.ALLOW_PRIVATE_CAMERA_HOSTS", True), \
                     mock.patch("recorder.shutil.which", return_value="ffmpeg"), \
                     mock.patch("recorder.subprocess.Popen", return_value=proc_inst):
                    ok2, info2 = svc._run_ffmpeg_record(
                        {"name": "Gerbang", "url": "rtsp://admin:SuperSecret@10.0.0.5/live"},
                        out_crash, 5, tag="test:crash",
                    )
                self.assertFalse(ok2)
                self.assertTrue(proc_inst.killed)
                self.assertFalse(out_crash.exists(), "file rekaman parsial harus dibersihkan")
                self.assertNotIn("SuperSecret", info2)


# ==============================================================================
# 5. P1 #7: ConcurrencyGuard (HTTP 503), /api/health & Uji Beban (8 Kamera + 2 Rekaman)
# ==============================================================================
class TestConcurrencyHealthAndLoad(unittest.TestCase, _HttpTestMixin):
    def test_load_8_cameras_plus_2_recordings_and_503_when_capacity_exceeded(self) -> None:
        class FastStreamUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.0,\nhttps://stream.kuduskab.go.id/seg1.ts\n"
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        up = ThreadingHTTPServer(("127.0.0.1", 0), FastStreamUpstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        up_port = up.server_address[1]

        guard = system_health.ConcurrencyGuard(
            max_ffmpeg=6, max_rtsp_bridges=2, max_recordings=2,
            max_viewers_per_camera=4, max_total_viewers=8,
        )
        cams = [
            {"id": i, "name": f"Kamera {i}", "folder": "Semarang", "type": "hls",
             "url": f"http://127.0.0.1:{up_port}/cam{i}.m3u8"}
            for i in range(1, 10)
        ]
        self.start_isolated_server(extra_patches=[
            mock.patch.object(server, "LOAD_GUARD", guard),
            mock.patch("server.get_cameras", return_value={"cameras": cams, "apiStatus": {"Semarang": {"online": True}}}),
            mock.patch("server.is_allowed_stream_url", return_value=True),
            mock.patch("net_guard._TEST_ALLOW_LOOPBACK", True),  # upstream palsu di 127.0.0.1
        ])
        try:
            admin, _ = self.make_client()
            self.request_raw(admin, "POST", "/api/login", {"username": "admin", "password": users_mod.DEFAULT_PASSWORD})
            self.request_raw(admin, "POST", "/api/auth/password", {
                "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": "LoadTestPassword99!",
            })

            # Simulasikan 2 rekaman ffmpeg berjalan serentak di ConcurrencyGuard sambil 8 kamera ditonton bersamaan
            with guard.ffmpeg_slot("recorder"), guard.ffmpeg_slot("recorder"):
                results: list[int] = []
                barrier = threading.Barrier(8)

                def watch_camera(idx: int):
                    client_i, _ = self.make_client()
                    # Berbagi cookie sesi admin agar tidak perlu 8x PBKDF2 login
                    for cookie in admin.handlers:
                        if isinstance(cookie, urllib.request.HTTPCookieProcessor):
                            for c in cookie.cookiejar:
                                client_i.handlers[0]
                    q = urllib.parse.quote(cams[idx]["url"], safe="")
                    barrier.wait(timeout=5)
                    st, _, _ = self.request_raw(admin, "GET", f"/api/proxy?url={q}")
                    results.append(st)

                threads = [threading.Thread(target=watch_camera, args=(i,)) for i in range(8)]
                t0 = time.perf_counter()
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0

                self.assertEqual(results, [200] * 8)
                self.assertLess(elapsed_ms, 5000.0)

                st_h, _, health = self.request_raw(admin, "GET", "/api/health")
                self.assertEqual(st_h, 200)
                self.assertEqual(health["ffmpeg"]["active_processes"], 2)
                self.assertIsNotNone(health["disk"]["percent"])
                if health["memory"]["rss_mb"]:
                    self.assertGreater(health["memory"]["rss_mb"], 0)

                with self.assertRaises(system_health.CapacityError):
                    with guard.ffmpeg_slot("recorder"):
                        pass

            # Saat seluruh 8 slot penonton ditahan serentak, penonton ke-9 mendapat HTTP 503 jelas
            with ExitStack() as stack:
                for i in range(8):
                    stack.enter_context(guard.viewer_slot(f"cam-{i}"))
                q9 = urllib.parse.quote(cams[8]["url"], safe="")
                st503, _, err503 = self.request_raw(admin, "GET", f"/api/proxy?url={q9}")
                self.assertEqual(st503, 503)
                self.assertTrue(err503.get("error"))
        finally:
            self.stop_isolated_server()
            up.shutdown()
            up.server_close()


# ==============================================================================
# 6. P2 #11 & #13: Checksum SHA-256 YOLO11n, Auto-Stop Live AI & Redaksi Rahasia
# ==============================================================================
class TestModelChecksumLiveAiAndRedaction(unittest.TestCase):
    def test_yolo_model_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fake_model = Path(d) / "yolo11n.onnx"
            content = b"dummy-onnx-weights-for-test"
            fake_model.write_bytes(content)
            good_hash = hashlib.sha256(content).hexdigest()

            ok, msg = ai.verify_model_checksum(fake_model, good_hash)
            self.assertTrue(ok, msg)

            ok_bad, msg_bad = ai.verify_model_checksum(fake_model, "0" * 64)
            self.assertFalse(ok_bad)
            self.assertIn("Checksum", msg_bad)

    def test_live_ai_auto_stop_terminates_ffmpeg_without_viewers(self) -> None:
        svc = live_ai.LiveAiService(lambda: {"cameras": []}, None)
        stopped_flag = {"called": False}

        class FakeReader:
            ended = False
            last_frame_at = time.time()

            def end_reason(self):
                return ""

            def stop(self):
                stopped_flag["called"] = True

        now = time.time()
        session = {
            "key": "m:test",
            "cam": {"name": "Kamera Uji"},
            "started_at": now - 10,
            "last_viewer_at": now - 120,
            "viewers": 0,
            "stop": threading.Event(),
            "reader": FakeReader(),
            "thread": None,
        }
        with svc._lock:
            svc._session = session

        reason = svc._end_reason(session, now)
        self.assertIsNotNone(reason)
        self.assertIn("Tidak ada yang menonton", reason)
        svc.stop("m:test", reason=reason)
        self.assertTrue(stopped_flag["called"])
        self.assertFalse(svc.status()["active"])

    def test_stream_reader_stop_kills_hung_ffmpeg_process(self) -> None:
        reader = live_ai._StreamReader({"url": "rtsp://admin:Rahasia77@10.0.0.9/live"}, 640, 360)

        class HungFfmpeg:
            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 1)
                return -9

            def kill(self):
                self.killed = True

        proc = HungFfmpeg()
        reader._proc = proc
        reader.stop()
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)

    def test_credentials_never_leak_in_logs_audit_or_error_text(self) -> None:
        dirty = (
            'Gagal konek ke rtsp://admin:SangatRahasia123@192.168.1.99:554/Streaming/Channels/101 '
            'dan https://api.telegram.org/bot99887766:AAH_SecretBotToken_XYZ123/sendPhoto '
            'serta {"access_secret": "TuyaTopSecret999"} '
            'dan <wsse:Password>OnvifPass888</wsse:Password>'
        )
        cleaned = redact_sensitive_text(dirty)
        for secret in ("SangatRahasia123", "AAH_SecretBotToken_XYZ123", "TuyaTopSecret999", "OnvifPass888"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("rtsp://***@192.168.1.99:554", cleaned)

        with tempfile.TemporaryDirectory() as d:
            alog = audit_mod.AuditLog(Path(d) / "audit.db")
            alog.log("admin", "camera.add", "rtsp://user:LeakPass99@cam.contoh.id/1", dirty)
            row = alog.query()[0]
            self.assertNotIn("LeakPass99", row["target"])
            self.assertNotIn("SangatRahasia123", row["detail"])
            self.assertNotIn("AAH_SecretBotToken_XYZ123", row["detail"])


if __name__ == "__main__":
    unittest.main()

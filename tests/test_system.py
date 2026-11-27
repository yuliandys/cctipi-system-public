#!/usr/bin/env python3
"""Automated unit tests for CCTipi System."""

from __future__ import annotations

import io
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

import ai
import enhance
import scrapers
import server
from storage_utils import write_private


class TestStorageUtils(unittest.TestCase):
    def test_write_private_creates_parents_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "secret.json"
            write_private(target, '{"ok": true}')
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}')


class TestAiKeys(unittest.TestCase):
    def test_normalize_stream_url_strips_volatile_params(self) -> None:
        url = "https://example.com/stream.m3u8?cam=1&token=abc123&jwt=xyz&quality=hd"
        self.assertEqual(
            ai.normalize_stream_url(url),
            "https://example.com/stream.m3u8?cam=1&quality=hd",
        )

    def test_camera_key_and_normalize_key(self) -> None:
        self.assertEqual(ai.camera_key({"manualId": "m1", "url": "rtsp://1.2.3.4"}), "m:m1")
        self.assertEqual(
            ai.normalize_key("u:https://host/live.m3u8?sig=999&id=2"),
            "u:https://host/live.m3u8?id=2",
        )


class TestEnhance(unittest.TestCase):
    def test_enhance_image_auto_skips_bright_and_enhances_dark(self) -> None:
        import numpy as np

        bright = np.full((64, 64, 3), 180, dtype=np.uint8)
        out_b, info_b = enhance.enhance_image(bright, mode="auto")
        self.assertFalse(info_b["applied"])
        self.assertEqual(out_b.shape, bright.shape)

        dark = np.full((64, 64, 3), 20, dtype=np.uint8)
        out_d, info_d = enhance.enhance_image(dark, mode="auto")
        self.assertTrue(info_d["applied"])
        self.assertGreater(info_d["brightness_after"], info_d["brightness_before"])

        out_off, info_off = enhance.enhance_image(dark, mode="off")
        self.assertFalse(info_off["applied"])
        self.assertEqual(out_off.shape, dark.shape)


class TestServerProxyAndSecurity(unittest.TestCase):
    def test_rewrite_m3u8_rewrites_segments_and_key_uris(self) -> None:
        playlist = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:4.0,\n"
            "seg001.ts\n"
        )
        out = server.rewrite_m3u8(playlist, "https://stream.kuduskab.go.id/hls/index.m3u8")
        key_url = "https://stream.kuduskab.go.id/hls/key.bin"
        seg_url = "https://stream.kuduskab.go.id/hls/seg001.ts"
        sign = server.proxy_routes.sign_url
        self.assertIn(f'URI="/api/proxy?url=https%3A%2F%2Fstream.kuduskab.go.id%2Fhls%2Fkey.bin&sig={sign(key_url)}"', out)
        self.assertIn(f"/api/proxy?url=https%3A%2F%2Fstream.kuduskab.go.id%2Fhls%2Fseg001.ts&sig={sign(seg_url)}", out)
        self.assertTrue(server.proxy_routes.verify_url_sig(seg_url, sign(seg_url)))
        self.assertFalse(server.proxy_routes.verify_url_sig(seg_url, "0" * 32))

    def test_is_allowed_stream_url(self) -> None:
        self.assertTrue(server.is_allowed_stream_url("https://stream.kuduskab.go.id/live.m3u8"))
        self.assertFalse(server.is_allowed_stream_url("http://127.0.0.1:9999/api/auth/me"))
        self.assertFalse(server.is_allowed_stream_url("file:///etc/passwd"))

    def test_fetch_upstream_pooled_blocks_disallowed_redirect(self) -> None:
        fake_resp = mock.MagicMock()
        fake_resp.status = 302
        fake_resp.getheader.return_value = "http://127.0.0.1:9999/secret"
        fake_resp.read.return_value = b""

        fake_conn = mock.MagicMock()
        fake_conn.getresponse.return_value = fake_resp

        with mock.patch("server._acquire_conn", return_value=fake_conn), mock.patch(
            "server._release_conn"
        ):
            with self.assertRaises(PermissionError):
                server.fetch_upstream_pooled(
                    "https://stream.kuduskab.go.id/live.m3u8",
                    {},
                    url_validator=server.is_allowed_stream_url,
                )

    def test_fetch_upstream_pooled_redirect_without_location_keeps_conn_open(self) -> None:
        fake_resp = mock.MagicMock()
        fake_resp.status = 302
        fake_resp.getheader.return_value = None

        fake_conn = mock.MagicMock()
        fake_conn.getresponse.return_value = fake_resp

        with mock.patch("server._acquire_conn", return_value=fake_conn), mock.patch(
            "server._release_conn"
        ) as release_mock:
            resp, conn, *_ = server.fetch_upstream_pooled(
                "https://stream.kuduskab.go.id/live.m3u8", {}
            )
            self.assertIs(resp, fake_resp)
            self.assertIs(conn, fake_conn)
            release_mock.assert_not_called()
            fake_conn.close.assert_not_called()

    def test_upstream_origin_referer_matches_source_or_target(self) -> None:
        origin_pati, referer_pati = server._upstream_origin_referer(
            "https://streaming.patikab.go.id/memfs/abc.m3u8"
        )
        self.assertEqual(origin_pati, "https://cctvkabpati.biz.id")
        self.assertEqual(referer_pati, "https://cctvkabpati.biz.id/")

        origin_custom, referer_custom = server._upstream_origin_referer(
            "https://custom-cam.example.com:8443/live.m3u8"
        )
        self.assertEqual(origin_custom, "https://custom-cam.example.com:8443")
        self.assertEqual(referer_custom, "https://custom-cam.example.com:8443/")

        self.assertTrue(server._host_is_mjpeg("multipart/x-mixed-replace; boundary=frame"))
        self.assertFalse(server._host_is_mjpeg("video/mp2t"))


class TestAuthAndSessions(unittest.TestCase):
    def test_session_store_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SessionStore(Path(tmpdir) / "sessions.json")
            tok = store.create("admin")
            self.assertEqual(store.verify(tok), "admin")
            store.revoke(tok)
            self.assertIsNone(store.verify(tok))

            tok2 = store.create("admin")
            store.revoke_all()
            self.assertIsNone(store.verify(tok2))

    def test_login_limiter_locks_after_max_fails(self) -> None:
        limiter = server.LoginLimiter()
        keys = ["ip:127.0.0.1", "user:admin"]
        for _ in range(limiter.MAX_FAILS):
            limiter.fail(keys)
        self.assertGreater(limiter.retry_after(keys), 0)
        limiter.success(keys)
        self.assertEqual(limiter.retry_after(keys), 0)

    def test_legacy_auth_json_migrates_to_admin(self) -> None:
        import users
        with tempfile.TemporaryDirectory() as tmpdir:
            auth_file = Path(tmpdir) / "auth.json"
            auth_file.write_text(json.dumps({"username": "andy", "passwordHash": users.hash_password("rahasia123")}))
            store = users.UserStore(auth_file)
            ctx = store.authenticate("andy", "rahasia123")
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["role"], "admin")
            self.assertIn("users.manage", ctx["permissions"])
            self.assertIsNone(ctx["regions"])
            saved = json.loads(auth_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertEqual({r["id"] for r in saved["roles"]}, {"admin", "operator", "viewer"})


class TestScrapersAndTokens(unittest.TestCase):
    def test_parse_cameras_from_html_jepara(self) -> None:
        sample = """
        <select id="pilih-cctv">
          <option value="https://cctv-jepara.lifemedia.id/hls/alun.m3u8">Alun-Alun 1</option>
          <option value="https://stream-cctv.jepara.go.id/live/simpang.m3u8">Simpang &amp; Tiga</option>
          <option value="">Kosong</option>
        </select>
        """
        cams = scrapers.parse_cameras_from_html(sample)
        self.assertEqual(len(cams), 2)
        self.assertEqual(cams[0]["provider"], "lifemedia")
        self.assertEqual(cams[1]["provider"], "diskominfo")
        self.assertEqual(cams[1]["name"], "Simpang & Tiga")

    def test_jwt_expiry_and_mark_token_expiry(self) -> None:
        import base64

        payload = base64.urlsafe_b64encode(b'{"exp": 1000}').decode("ascii").rstrip("=")
        token = f"hdr.{payload}.sig"
        cams = [
            {"name": "Expired", "url": f"https://host/live.m3u8?token={token}"},
            {"name": "NoToken", "url": "https://host/plain.m3u8"},
        ]
        marked = scrapers.mark_token_expiry(cams, now=2000.0)
        self.assertTrue(marked[0].get("tokenExpired"))
        self.assertNotIn("tokenExpired", marked[1])

    def test_fetch_custom_region_cameras_direct_m3u8_and_mjpeg(self) -> None:
        hls = scrapers.fetch_custom_region_cameras({
            "folder": "Test",
            "title": "Cam HLS",
            "apiUrl": "https://example.com/stream.m3u8?x=1",
            "format": "HLS",
        })
        self.assertEqual(len(hls), 1)
        self.assertEqual(hls[0]["type"], "hls")

        mjpeg = scrapers.fetch_custom_region_cameras({
            "folder": "Test2",
            "title": "Cam MJPEG",
            "apiUrl": "https://example.com/mjpeg",
            "format": "MJPEG",
        })
        self.assertEqual(len(mjpeg), 1)
        self.assertEqual(mjpeg[0]["type"], "mjpeg")

    def test_parse_solo_cameras_from_html(self) -> None:
        html_sample = '''
        <div class="cctv-thumb" data-id="1" data-name="AGAS" data-stream-url="https://surakarta.atcsindonesia.info:8086/camera/Agas.flv"></div>
        <div class="cctv-thumb" data-id="2" data-name="Balai Kota" data-stream-url="https://surakarta.atcsindonesia.info:8086/camera/BalaiKota.flv"></div>
        <div class="cctv-thumb" data-id="3" data-name="AGAS Dup" data-stream-url="https://surakarta.atcsindonesia.info:8086/camera/Agas.flv"></div>
        '''
        cams = scrapers.parse_solo_cameras_from_html(html_sample)
        self.assertEqual(len(cams), 2)
        self.assertEqual(cams[0]["folder"], "Solo")
        self.assertEqual(cams[0]["type"], "flv")
        self.assertEqual(cams[1]["name"], "Balai Kota")



# ---------------------------------------------------------------------------
# v6.1 — perbaikan proxy stream, sumber→scraper, MJPEG hub, token kedaluwarsa
# ---------------------------------------------------------------------------
import http.server
import socketserver
import threading
import time
import urllib.request

import mjpeg_hub

JPEG_A = b"\xff\xd8" + b"A" * 200 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 300 + b"\xff\xd9"


class _FakeStream:
    """Respons palsu dengan read1(), mengalirkan frame lalu berhenti."""

    def __init__(self, chunks, delay=0.0):
        self.chunks = list(chunks)
        self.delay = delay

    def read1(self, _n=65536):
        if self.delay:
            time.sleep(self.delay)
        return self.chunks.pop(0) if self.chunks else b""


class TestMjpegHub(unittest.TestCase):
    def test_extract_frames_handles_multipart_and_partial_data(self) -> None:
        buf = bytearray(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG_A + b"\r\n--frame\r\n\r\n" + JPEG_B[:50])
        frames = mjpeg_hub.extract_frames(buf)
        self.assertEqual(frames, [JPEG_A])
        buf += JPEG_B[50:]
        self.assertEqual(mjpeg_hub.extract_frames(buf), [JPEG_B])

    def test_hub_shares_one_reader_per_url_and_returns_latest_frame(self) -> None:
        opened = []

        def opener(url):
            opened.append(url)
            body = b"".join(b"--f\r\n\r\n" + JPEG_A + b"\r\n" for _ in range(3)) + b"--f\r\n\r\n" + JPEG_B
            return _FakeStream([body[i:i + 97] for i in range(0, len(body), 97)], delay=0.01), lambda: None

        hub = mjpeg_hub.MjpegHub(opener, idle_timeout=1.0, snapshot_interval=5.0)
        frame1, seq1, err1 = hub.get_frame("http://cam/1", wait=2.0)
        self.assertIn(frame1, (JPEG_A, JPEG_B))
        self.assertEqual(err1, "")
        frame2, seq2, _ = hub.get_frame("http://cam/1", after_seq=0, wait=2.0)
        self.assertEqual(len(opened), 1, "penonton kedua harus memakai pembaca yang sama")
        time.sleep(0.5)
        frame3, _, _ = hub.get_frame("http://cam/1", after_seq=seq2, wait=0.5)
        self.assertEqual(frame3, JPEG_B, "frame terakhir yang dikirim sumber")
        hub.close()

    def test_hub_reports_error_for_non_jpeg_source(self) -> None:
        def opener(_url):
            return _FakeStream([b"<html>bukan gambar</html>"]), lambda: None

        hub = mjpeg_hub.MjpegHub(opener, idle_timeout=1.0)
        start = time.time()
        frame, _, err = hub.get_frame("http://cam/x", first_wait=6.0)
        self.assertIsNone(frame)
        self.assertTrue(err)
        self.assertLess(time.time() - start, 6.0, "gagal jelas tidak boleh menahan klien sampai batas penuh")
        hub.close()

    def test_hub_reader_stops_when_idle(self) -> None:
        def opener(_url):
            return _FakeStream([b"\r\n" + JPEG_A] * 1000, delay=0.02), lambda: None

        hub = mjpeg_hub.MjpegHub(opener, idle_timeout=0.3)
        hub.get_frame("http://cam/idle", wait=1.0)
        time.sleep(1.5)
        self.assertEqual(hub.stats()["active_readers"], 0)


class TestProxyStreamClassification(unittest.TestCase):
    def test_is_finite_segment(self) -> None:
        f = server._is_finite_segment
        self.assertTrue(f("video/mp2t", "https://a/seg001.ts"))
        self.assertTrue(f("application/octet-stream", "https://a/chunk.m4s"))
        self.assertTrue(f("", "https://a/seg.ts"))
        self.assertFalse(f("multipart/x-mixed-replace; boundary=x", "https://a/stream"))
        self.assertFalse(f("video/x-motion-jpeg", "https://a/stream"))
        self.assertFalse(f("image/jpeg", "https://a/zm/cgi-bin/nph-zms?monitor=1"))
        self.assertFalse(f("application/octet-stream", "https://a/video.mjpg"))

    def test_read_capped(self) -> None:
        data, done = server._read_capped(_FakeStream([b"x" * 100, b"y" * 100]), 1000)
        self.assertTrue(done)
        self.assertEqual(len(data), 200)
        data, done = server._read_capped(_FakeStream([b"x" * 600] * 10), 1000)
        self.assertFalse(done)


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/plain") or self.path.startswith("/multi"):
            ct = "multipart/x-mixed-replace; boundary=f" if self.path.startswith("/multi") else "video/x-motion-jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(200):
                    self._chunk(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG_A + b"\r\n")
                    time.sleep(0.02)
            except Exception:
                pass
            return
        if self.path.startswith("/seg.ts"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(5):
                self._chunk(b"\x47" * 188)
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestProxyAndMjpegEndToEnd(unittest.TestCase):
    """Server CCTipi sungguhan + sumber palsu lokal (tanpa internet)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.up = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Upstream)
        cls.up.daemon_threads = True
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.patches = [
            mock.patch.object(server.CctipiHandler, "require_auth", return_value=True),
            mock.patch("server.allowed_stream_hosts", return_value=frozenset({"127.0.0.1"})),
            mock.patch("net_guard._TEST_ALLOW_LOOPBACK", True),  # upstream palsu di 127.0.0.1
        ]
        for p in cls.patches:
            p.start()
        cls.app = server.FastThreadingHTTPServer(("127.0.0.1", 0), server.CctipiHandler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app.server_address[1]}"
        cls.src = f"http://127.0.0.1:{cls.up.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.shutdown()
        cls.app.server_close()
        cls.up.shutdown()
        cls.up.server_close()
        for p in cls.patches:
            p.stop()

    def _proxy(self, path):
        q = urllib.parse.quote(self.src + path, safe="")
        return urllib.request.urlopen(f"{self.base}/api/proxy?url={q}", timeout=5)

    def test_proxy_streams_mjpeg_with_any_content_type(self) -> None:
        # Regresi: video/x-motion-jpeg dulu dibaca sampai habis -> klien macet.
        for path in ("/plain", "/multi"):
            with self._proxy(path) as resp:
                self.assertEqual(resp.status, 200)
                start = time.time()
                data = resp.read(len(JPEG_A) * 3)
                self.assertLess(time.time() - start, 3.0)
                self.assertIn(JPEG_A, data)

    def test_proxy_buffers_finite_chunked_segment(self) -> None:
        with self._proxy("/seg.ts") as resp:
            self.assertEqual(resp.headers["Content-Length"], str(188 * 5))
            self.assertEqual(len(resp.read()), 188 * 5)

    def test_mjpeg_frame_endpoint_short_requests(self) -> None:
        q = urllib.parse.quote(self.src + "/plain?cam=e2e", safe="")
        seq = 0
        for _ in range(3):
            with urllib.request.urlopen(f"{self.base}/api/mjpeg-frame?url={q}&seq={seq}", timeout=15) as resp:
                self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
                self.assertEqual(resp.read(), JPEG_A)
                new_seq = int(resp.headers["X-Frame-Seq"])
                self.assertGreaterEqual(new_seq, seq)
                seq = new_seq

    def test_mjpeg_frame_rejects_disallowed_url(self) -> None:
        q = urllib.parse.quote("http://contoh-tidak-diizinkan.local/x", safe="")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/api/mjpeg-frame?url={q}", timeout=5)
        try:
            self.assertEqual(ctx.exception.code, 400)
        finally:
            ctx.exception.close()


class TestSourcesAndTokens(unittest.TestCase):
    def test_source_urls_reach_scrapers_and_reset(self) -> None:
        sources = server.load_sources_config()
        try:
            changed = [dict(s) for s in sources]
            for s in changed:
                if s["folder"] == "Pati":
                    s["apiUrl"] = "https://contoh-baru.patikab.go.id/"
            server.apply_sources_config(changed)
            self.assertEqual(scrapers.PATI_CCTV_URL, "https://contoh-baru.patikab.go.id/")
            for s in changed:
                if s["folder"] == "Pati":
                    s["apiUrl"] = ""
            server.apply_sources_config(changed)
            self.assertEqual(scrapers.PATI_CCTV_URL, server._SCRAPER_URL_DEFAULTS["PATI_CCTV_URL"])
        finally:
            server.apply_sources_config(sources)

    def test_get_cameras_marks_expired_tokens_on_first_request(self) -> None:
        _expired_url = "https://example.invalid/zm/cgi-bin/nph-zms?monitor=1&token=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.dummy"
        fallback_sample = [{"id": 1, "name": "Alun Jombang", "url": _expired_url, "provider": "jombangkab", "folder": "Jombang"}]
        with server._cache_lock:
            saved = dict(server._cameras_cache)
            server._cameras_cache["cameras"] = []
        try:
            with mock.patch("server._trigger_bg_refresh"), \
                    mock.patch("server.load_region_cache", return_value={}), \
                    mock.patch("server.load_fallback_cameras", return_value=fallback_sample):
                data = server.get_cameras()
            jombang = [c for c in data["cameras"] if c.get("folder") == "Jombang"]
            self.assertTrue(jombang)
            self.assertTrue(all(c.get("tokenExpired") for c in jombang))
        finally:
            with server._cache_lock:
                server._cameras_cache.clear()
                server._cameras_cache.update(saved)


class TestJombangTokens(unittest.TestCase):
    """Diag 25-11-2026: ZoneMinder Jombang tetap melayani token 'kedaluwarsa'."""

    PAGE = (
        '<div class="cam-card" data-cam-name="Alun-alun Jombang" '
        'data-url="https://cctv.jombangkab.go.id/zm/cgi-bin/nph-zms?monitor=120&amp;token=AAA"></div>'
        '<script>cctvData = [{"id":"3","nama":"Alun-alun Jombang","lat":"-7.557116","lng":"112.233742",'
        '"url":"https:\\/\\/cctv.jombangkab.go.id\\/zm\\/cgi-bin\\/nph-zms?monitor=120&token=AAA","status":"online"}];</script>'
    )

    def _fetch(self, page: str):
        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("scrapers._urlopen", return_value=_R(page.encode())):
            return scrapers.fetch_jombang_cameras()

    def test_jombang_coordinates_from_cctvdata(self) -> None:
        cams = self._fetch(self.PAGE)
        self.assertEqual(len(cams), 1)
        self.assertAlmostEqual(cams[0]["lat"], -7.557116)
        self.assertAlmostEqual(cams[0]["lng"], 112.233742)

    def test_jombang_falls_back_to_cctvdata_without_cards(self) -> None:
        cams = self._fetch(self.PAGE.split("</div>", 1)[1])
        self.assertEqual([c["name"] for c in cams], ["Alun-alun Jombang"])

    def test_expired_token_is_informational_not_offline(self) -> None:
        # Contoh sintetis — edisi publik: cameras.json hanya berisi data Semarang,
        # jadi token kedaluwarsa dibuat di tempat (JWT dengan exp=1, tanpa verifikasi).
        _expired_url = "https://example.invalid/zm/cgi-bin/nph-zms?monitor=1&token=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.dummy"
        expired = [{"id": i, "name": f"Contoh {i}", "url": _expired_url,
                    "provider": "jombangkab", "folder": "Jombang"} for i in range(1, 4)]
        sources = [{"folder": "Jombang", "title": "Jombang"}]
        with mock.patch("server.load_sources_config", return_value=sources), \
                mock.patch.dict("server.BUILTIN_FETCHERS", {"Jombang": lambda: [dict(c) for c in expired]}), \
                mock.patch("server.save_region_cache"), mock.patch("server.load_manual_cameras", return_value=[]):
            result = server._refresh_cameras_sync()
        self.assertTrue(result["apiStatus"]["Jombang"]["online"])
        data = server.get_cameras()
        jombang = [c for c in data["cameras"] if c.get("folder") == "Jombang"]
        self.assertTrue(jombang and all(c.get("tokenExpired") for c in jombang))

if __name__ == "__main__":
    unittest.main()


class TestAiClassSelection(unittest.TestCase):
    """v6.5: kategori Barang bawaan + pilihan per objek."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(ai, "DATA_DIR", d),
            mock.patch.object(ai, "SETTINGS_FILE", d / "ai_settings.json"),
            mock.patch.object(ai, "JEPRETAN_DIR", d / "jepretan"),
        ]
        for name in ("DB_FILE", "DB_PATH"):
            if hasattr(ai, name):
                self.patches.append(mock.patch.object(ai, name, d / "jepretan.db"))
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_catalog_lists_barang_with_labels(self) -> None:
        cat = {c["key"]: c for c in ai.class_catalog()}
        self.assertEqual(set(cat), {"orang", "kendaraan", "hewan", "barang"})
        self.assertFalse(cat["barang"]["default"])
        names = [o["name"] for o in cat["barang"]["objects"]]
        self.assertEqual(names, ["backpack", "handbag", "suitcase"])
        self.assertEqual(ai.CLASS_CATEGORY[28], "barang")

    def test_detection_filter_by_category_and_object(self) -> None:
        car = {"category": "kendaraan", "class_name": "car"}
        moto = {"category": "kendaraan", "class_name": "motorcycle"}
        bag = {"category": "barang", "class_name": "backpack"}
        classes = {"orang": True, "kendaraan": True, "hewan": True}
        self.assertTrue(ai.detection_enabled(car, classes, {"motorcycle": False}))
        self.assertFalse(ai.detection_enabled(moto, classes, {"motorcycle": False}))
        # pengaturan lama tanpa kunci "barang" -> tetap mati (bawaan)
        self.assertFalse(ai.detection_enabled(bag, classes, {}))
        self.assertTrue(ai.detection_enabled(bag, {**classes, "barang": True}, {}))
        self.assertFalse(ai.detection_enabled({"category": "", "class_name": "cup"}, classes, {}))

    def test_settings_roundtrip_objects(self) -> None:
        svc = ai.AiService(lambda: [])
        pub = svc.public_settings()
        self.assertFalse(pub["classes"]["barang"])
        self.assertTrue(pub["objects"]["motorcycle"])
        self.assertEqual(len(pub["catalog"]), 4)
        out = svc.update_settings({
            "classes": {"barang": True},
            "objects": {"motorcycle": False, "car": True, "unknown_thing": False},
        })
        self.assertTrue(out["classes"]["barang"])
        self.assertFalse(out["objects"]["motorcycle"])
        self.assertNotIn("unknown_thing", out["objects"])
        # tersimpan ke disk hanya objek yang dimatikan
        saved = json.loads(ai.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["objects"], {"motorcycle": False})
        # dinyalakan lagi -> hilang dari daftar mati
        svc.update_settings({"objects": {"motorcycle": True}})
        self.assertEqual(json.loads(ai.SETTINGS_FILE.read_text(encoding="utf-8"))["objects"], {})

    def test_old_settings_file_keeps_behavior(self) -> None:
        ai.SETTINGS_FILE.write_text(json.dumps({"classes": {"orang": True, "kendaraan": False, "hewan": True}}), encoding="utf-8")
        pub = ai.AiService(lambda: []).public_settings()
        self.assertEqual(pub["classes"], {"orang": True, "kendaraan": False, "hewan": True, "barang": False})
        self.assertTrue(all(pub["objects"].values()))


import http.cookiejar
import users as users_mod


class TestUserStoreRules(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.revoked: list[str] = []
        self.store = users_mod.UserStore(Path(self.tmp.name) / "auth.json", on_revoke=self.revoked.append)
        self.store.reload()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_admin_and_must_change(self) -> None:
        ctx = self.store.authenticate("admin", users_mod.DEFAULT_PASSWORD)
        self.assertEqual(ctx["role"], "admin")
        self.assertTrue(ctx["mustChange"])  # password bawaan -> diminta ganti
        self.assertIsNone(self.store.authenticate("admin", "salah-password"))
        self.assertIsNone(self.store.authenticate("tidakada", "apapun123"))

    def test_create_user_roles_and_regions(self) -> None:
        role = self.store.save_role({"name": "Satpam Pos", "permissions": ["jepretan.view", "ptz.control", "bogus"]})
        self.assertEqual(role["id"], "satpam-pos")
        self.assertEqual(role["permissions"], ["jepretan.view", "ptz.control"])
        self.store.create_user({"username": "budi", "password": "budi12345", "role": "satpam-pos", "regions": ["Lokal", "Lokal"]})
        ctx = self.store.authenticate("BUDI", "budi12345")  # username tidak peka huruf besar
        self.assertEqual(ctx["regions"], ["Lokal"])
        self.assertTrue(ctx["mustChange"])  # dibuat admin -> wajib ganti saat login pertama
        self.assertEqual(ctx["permissions"], {"jepretan.view", "ptz.control"})
        with self.assertRaises(users_mod.UserError):
            self.store.create_user({"username": "budi", "password": "lainlain123", "role": "viewer"})
        with self.assertRaises(users_mod.UserError):
            self.store.delete_role("satpam-pos")  # masih dipakai
        with self.assertRaises(users_mod.UserError):
            self.store.save_role({"id": "admin", "name": "Admin", "permissions": []})

    def test_admin_guards_and_revocation(self) -> None:
        with self.assertRaises(users_mod.UserError):
            self.store.update_user("admin", {"disabled": True}, "admin")
        with self.assertRaises(users_mod.UserError):
            self.store.delete_user("admin", "admin")
        self.store.create_user({"username": "kedua", "password": "kedua12345", "role": "admin"})
        with self.assertRaises(users_mod.UserError):
            self.store.update_user("admin", {"role": "viewer"}, "admin")  # role sendiri
        self.store.update_user("admin", {"role": "viewer"}, "kedua")      # masih ada admin lain
        self.assertIn("admin", self.revoked)
        with self.assertRaises(users_mod.UserError):
            self.store.update_user("kedua", {"role": "viewer"}, "admin")  # admin terakhir
        self.store.update_user("admin", {"password": "resetbaru123"}, "kedua")
        self.assertTrue(self.store.authenticate("admin", "resetbaru123")["mustChange"])
        name = self.store.change_own("admin", "resetbaru123", "andy", "milikku12345")
        self.assertEqual(name, "andy")
        ctx = self.store.authenticate("andy", "milikku12345")
        self.assertFalse(ctx["mustChange"])
        with self.assertRaises(PermissionError):
            self.store.change_own("andy", "salah", "andy", "apapun12345")


class TestPermissionsEndToEnd(unittest.TestCase):
    CAMS = [
        {"id": 1, "name": "Gerbang", "folder": "Lokal", "type": "mjpeg", "url": "http://127.0.0.1:1/a.mjpg"},
        {"id": 2, "name": "Alun", "folder": "Jepara", "type": "mjpeg", "url": "http://127.0.0.2:1/b.mjpg"},
        {"id": 3, "name": "PTZ", "folder": "Lokal", "type": "rtsp", "manualId": "m-1", "url": "rtsp://x/1",
         "onvif": {"deviceUrl": "http://x/onvif/device_service", "password": "rahasia"}},
    ]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        sessions = server.SessionStore(d / "sessions.json")
        store = users_mod.UserStore(d / "auth.json", on_revoke=sessions.revoke_user)
        store.reload()
        cls.patches = [
            mock.patch.object(server, "SESSIONS", sessions),
            mock.patch.object(server, "USERS", store),
            mock.patch("server.get_cameras", return_value={"cameras": cls.CAMS, "apiStatus": {"Lokal": {}, "Jepara": {}}}),
            mock.patch("server.load_manual_cameras", return_value=[cls.CAMS[2]]),
            mock.patch("server.is_allowed_stream_url", return_value=True),
            mock.patch.object(server.LOGIN_LIMITER, "retry_after", return_value=0),
            mock.patch.object(server.ACTION_LIMITER, "check_and_record", return_value=0),
        ]
        for p in cls.patches:
            p.start()
        cls.app = server.FastThreadingHTTPServer(("127.0.0.1", 0), server.CctipiHandler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.shutdown()
        cls.app.server_close()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    def _client(self, username: str, password: str, *, unlock_pw: str | None = None):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._call(opener, "POST", "/api/login", {"username": username, "password": password})
        if unlock_pw:
            self._call(opener, "POST", "/api/auth/password", {
                "username": username, "currentPassword": password, "password": unlock_pw,
            })
        return opener

    def _call(self, opener, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_roles_permissions_and_regions(self) -> None:
        admin = self._client("admin", users_mod.DEFAULT_PASSWORD)
        st, me = self._call(admin, "GET", "/api/auth/me")
        self.assertEqual(me["role"], "admin")
        self.assertTrue(me["mustChange"])
        # Selama password bawaan aktif, endpoint operasional diblokir 403
        self.assertEqual(self._call(admin, "GET", "/api/users")[0], 403)
        self.assertEqual(self._call(admin, "POST", "/api/auth/password", {
            "username": "admin", "currentPassword": users_mod.DEFAULT_PASSWORD, "password": "adminBaru12345",
        })[0], 200)
        st, data = self._call(admin, "GET", "/api/users")
        self.assertEqual(st, 200)
        self.assertEqual(data["regions"], ["Lokal", "Jepara"])
        st, _ = self._call(admin, "POST", "/api/roles", {"name": "Satpam", "permissions": ["jepretan.view"]})
        self.assertEqual(st, 200)
        st, _ = self._call(admin, "POST", "/api/users",
                           {"username": "satpam1", "password": "satpam12345", "role": "satpam", "regions": ["Lokal"]})
        self.assertEqual(st, 200)

        sat = self._client("satpam1", "satpam12345")
        st, me = self._call(sat, "GET", "/api/auth/me")
        self.assertEqual((me["role"], me["regions"], me["defaultPassword"]), ("satpam", ["Lokal"], True))
        self.assertEqual(self._call(sat, "GET", "/api/cameras")[0], 403)  # terkunci sebelum ganti password awal
        self.assertEqual(self._call(sat, "POST", "/api/auth/password", {
            "username": "satpam1", "currentPassword": "satpam12345", "password": "satpamBaru12345",
        })[0], 200)
        st, cams = self._call(sat, "GET", "/api/cameras")
        self.assertEqual([c["name"] for c in cams["cameras"]], ["Gerbang", "PTZ"])
        self.assertNotIn("onvif", cams["cameras"][1])           # tanpa izin PTZ
        self.assertEqual(list(cams["apiStatus"]), ["Lokal"])
        self.assertEqual(self._call(sat, "GET", "/api/users")[0], 403)
        self.assertEqual(self._call(sat, "GET", "/api/sources")[0], 403)
        self.assertEqual(self._call(sat, "POST", "/api/ai/settings", {"settings": {}})[0], 403)
        self.assertEqual(self._call(sat, "GET", "/api/rekaman")[0], 403)
        self.assertEqual(self._call(sat, "POST", "/api/live-ai/x/start")[0], 403)
        q = urllib.parse.quote(self.CAMS[1]["url"], safe="")
        self.assertEqual(self._call(sat, "GET", f"/api/mjpeg-frame?url={q}")[0], 403)  # wilayah lain
        st, mc = self._call(sat, "GET", "/api/manual-cameras")
        self.assertEqual(len(mc["cameras"]), 1)

        # admin menonaktifkan -> sesi satpam langsung tidak berlaku
        st, _ = self._call(admin, "POST", "/api/users/satpam1", {"disabled": True})
        self.assertEqual(st, 200)
        self.assertEqual(self._call(sat, "GET", "/api/cameras")[0], 401)
        self.assertEqual(self._call(admin, "DELETE", "/api/users/admin")[0], 400)  # akun sendiri
        st, _ = self._call(admin, "DELETE", "/api/users/satpam1")
        self.assertEqual(st, 200)
        self.assertEqual(self._call(admin, "DELETE", "/api/roles/satpam")[0], 200)
        # admin tetap melihat semua + kredensial ONVIF
        st, cams = self._call(admin, "GET", "/api/cameras")
        self.assertEqual(len(cams["cameras"]), 3)
        self.assertIn("onvif", cams["cameras"][2])


import audit as audit_mod
import monitor as monitor_mod


class TestZonesAndSchedule(unittest.TestCase):
    def test_point_in_polygon_and_filter(self) -> None:
        sq = [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]]
        self.assertTrue(ai.point_in_polygon(0.75, 0.75, sq))
        self.assertFalse(ai.point_in_polygon(0.25, 0.75, sq))
        dets = [
            {"box": [700, 300, 760, 700], "class_name": "person", "category": "orang"},  # kaki (730,700) di zona
            {"box": [100, 300, 160, 700], "class_name": "person", "category": "orang"},  # di luar
            {"box": [700, 100, 760, 300], "class_name": "person", "category": "orang"},  # kepala di zona? kaki y=300 -> luar
        ]
        out = ai.filter_by_zone(dets, {"polygons": [sq]}, 1000, 800)
        self.assertEqual(out, [dets[0]])
        self.assertEqual(ai.filter_by_zone(dets, None, 1000, 800), dets)

    def test_schedule_overnight_and_days(self) -> None:
        def at(day, hh, mm=0):  # 2026-11-23 adalah Senin
            return time.mktime((2026, 11, 23 + day, hh, mm, 0, 0, 0, -1))
        sch = {"enabled": True, "start": "18:00", "end": "06:00", "days": [0]}  # hanya malam Senin
        self.assertTrue(ai.schedule_active(sch, at(0, 19)))
        self.assertTrue(ai.schedule_active(sch, at(1, 5)))      # Selasa dini hari = sisa malam Senin
        self.assertFalse(ai.schedule_active(sch, at(1, 19)))    # Selasa malam tidak dipilih
        self.assertFalse(ai.schedule_active(sch, at(0, 12)))
        day = {"enabled": True, "start": "08:00", "end": "17:00", "days": [0, 1, 2, 3, 4]}
        self.assertTrue(ai.schedule_active(day, at(2, 9)))
        self.assertFalse(ai.schedule_active(day, at(5, 9)))     # Sabtu
        self.assertTrue(ai.schedule_active({"enabled": False}, at(5, 3)))
        self.assertTrue(ai.schedule_active(None))

    def test_clean_zone_validation(self) -> None:
        z = ai.clean_zone({"polygons": [[[0, 0], [2, 0], [1, -1]], [[0, 0], [1, 1]]],
                           "schedule": {"enabled": True, "start": "18:00", "end": "06:00", "days": [9, 1, "2"]}})
        self.assertEqual(z["polygons"], [[[0.0, 0.0], [1.0, 0.0], [1.0, 0.0]]])  # dijepit 0..1, poligon < 3 titik dibuang
        self.assertEqual(z["schedule"]["days"], [1, 2])
        self.assertIsNone(ai.clean_zone({"polygons": [], "schedule": {"enabled": False}}))
        with self.assertRaises(ValueError):
            ai.clean_zone({"schedule": {"enabled": True, "start": "25:00", "end": "06:00"}})


class TestAuditLog(unittest.TestCase):
    def test_log_query_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = audit_mod.AuditLog(Path(d) / "audit.db")
            log.log("admin", "auth.login", ip="10.0.0.1")
            log.log("admin", "jepretan.delete", "Gerbang · 25/11 08:00", "person 87%")
            log.log("satpam", "auth.login_failed", "satpam", ok=False)
            items = log.query()
            self.assertEqual([i["action"] for i in items], ["auth.login_failed", "jepretan.delete", "auth.login"])
            self.assertEqual(items[1]["label"], "Hapus jepretan")
            self.assertFalse(items[0]["ok"])
            self.assertEqual(len(log.query(action="auth.")), 2)
            self.assertEqual(len(log.query(username="admin")), 2)
            self.assertEqual(len(log.query(q="Gerbang")), 1)
            self.assertEqual(log.users(), ["admin", "satpam"])

    def test_settings_diff(self) -> None:
        d = server.settings_diff({"enabled": False, "interval": 10, "classes": {"orang": True}},
                                 {"enabled": True, "interval": 15, "classes": {"orang": False}})
        self.assertIn("enabled: off → on", d)
        self.assertIn("interval: 10 → 15", d)
        self.assertIn("classes: orang off", d)


class TestMonitor(unittest.TestCase):
    def test_offline_alert_once_then_recovery_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cams = [{"name": "Gerbang", "folder": "Lokal", "manualId": "g"}, {"name": "Gudang", "folder": "Lokal", "manualId": "w"}]
            state = {"g": "offline", "w": "online"}
            sent, logged = [], []
            rec = {"quota_bytes": 100, "total_size_bytes": 10}
            mon = monitor_mod.MonitorService(
                Path(d) / "mon.json", get_cameras=lambda: cams, camera_key=lambda c: "m:" + c["manualId"],
                probe=lambda c: state[c["manualId"]], send_text=lambda t: (sent.append(t), (True, "ok"))[1],
                audit=lambda *a: logged.append(a[1]), data_dir=Path(d), recorder_status=lambda: rec)
            mon.update_settings({"enabled": True, "cameras": ["m:g", "m:w"], "offline_minutes": 5, "disk_alert_percent": 99})
            t0 = 1_000_000.0
            mon.run_once(t0)
            self.assertEqual(sent, [])                       # baru gagal: belum 5 menit
            self.assertEqual(mon.status()["cameras"][0]["status"], "gangguan")
            mon.run_once(t0 + 301)
            mon.run_once(t0 + 420)
            self.assertEqual(len(sent), 1)                   # hanya sekali
            self.assertIn("Gerbang", sent[0])
            self.assertEqual(mon.status()["offline"], 1)
            state["g"] = "online"
            mon.run_once(t0 + 600)
            self.assertEqual(len(sent), 2)
            self.assertIn("kembali online", sent[1])
            self.assertEqual(logged, ["alarm.offline", "alarm.online"])
            # kuota rekaman 99% -> alarm disk sekali, lalu pulih
            rec["total_size_bytes"] = 99.5
            mon.run_once(t0 + 700)
            mon.run_once(t0 + 800)
            self.assertEqual(logged.count("alarm.disk"), 1)
            rec["total_size_bytes"] = 50
            mon.run_once(t0 + 900)
            self.assertEqual(logged[-1], "alarm.disk_ok")


class TestRecordingFind(unittest.TestCase):
    def test_find_at_prefers_covering_clip(self) -> None:
        import recorder
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(recorder, "DATA_DIR", Path(d)), \
                 mock.patch.object(recorder, "REKAMAN_DIR", Path(d) / "rekaman"), \
                 mock.patch.object(recorder, "DB_FILE", Path(d) / "rec.db", create=True):
                svc = recorder.RecorderService(lambda: [])
                base = {"camera_id": "m:g", "camera_name": "Gerbang", "date": "2026-11-25", "size_bytes": 1, "path": "x"}
                svc._insert_recording({**base, "id": "seg1", "started_at": 1000, "duration_s": 300, "mode": "berkelanjutan"})
                svc._insert_recording({**base, "id": "clip1", "started_at": 1100, "duration_s": 30, "mode": "deteksi"})
                r = svc.find_at("m:g", 1110)
                self.assertEqual((r["recording"]["id"], r["offset"]), ("clip1", 7.0))
                r = svc.find_at("m:g", 1250)
                self.assertEqual((r["recording"]["id"], r["offset"]), ("seg1", 247.0))
                self.assertFalse(svc.find_at("m:g", 5000)["found"])
                self.assertFalse(svc.find_at("m:lain", 1110)["found"])


class TestAuditPrefsEndToEnd(TestPermissionsEndToEnd):
    """Login, aksi, dan preferensi per-user tercatat lewat HTTP sungguhan."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        d = Path(cls.tmp.name)
        cls.extra = [
            mock.patch.object(server, "AUDIT", audit_mod.AuditLog(d / "audit.db")),
            mock.patch.object(server, "PREFS_DIR", d / "prefs"),
        ]
        for p in cls.extra:
            p.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for p in reversed(cls.extra):
            p.stop()
        super().tearDownClass()

    def test_roles_permissions_and_regions(self) -> None:  # sudah diuji di kelas induk
        pass

    def test_audit_and_prefs(self) -> None:
        admin = self._client("admin", users_mod.DEFAULT_PASSWORD, unlock_pw="adminAudit12345")
        self._client("admin", "salahsalah")
        self._call(admin, "POST", "/api/roles", {"name": "Tamu", "permissions": []})
        st, data = self._call(admin, "GET", "/api/audit")
        self.assertEqual(st, 200)
        acts = [i["action"] for i in data["items"]]
        self.assertIn("auth.login", acts)
        self.assertIn("auth.login_failed", acts)
        self.assertIn("role.save", acts)
        role_item = next(i for i in data["items"] if i["action"] == "role.save")
        self.assertEqual(role_item["target"], "Tamu")
        # pengguna tanpa audit.view ditolak
        self._call(admin, "POST", "/api/users", {"username": "budi", "password": "budi123456", "role": "viewer"})
        budi = self._client("budi", "budi123456", unlock_pw="budiBaru123456")
        self.assertEqual(self._call(budi, "GET", "/api/audit")[0], 403)
        # preferensi per user terpisah
        self.assertEqual(self._call(admin, "GET", "/api/prefs")[1]["prefs"], None)
        self._call(admin, "POST", "/api/prefs", {"prefs": {"cctipi_layout_v1": {"rows": 3, "cols": 3, "slots": ["u:a"]}}})
        self.assertEqual(self._call(admin, "GET", "/api/prefs")[1]["prefs"]["cctipi_layout_v1"]["rows"], 3)
        self.assertIsNone(self._call(budi, "GET", "/api/prefs")[1]["prefs"])
        self.assertEqual(self._call(budi, "POST", "/api/prefs", {"prefs": "bukan objek"})[0], 400)


class TestSecurityHardening(TestPermissionsEndToEnd):
    """v6.7.2: password kamera tidak bocor, PTZ pakai data server, CSRF ditolak."""

    CAMS = [
        {"id": 1, "name": "Teras", "folder": "Lokal", "type": "rtsp", "manualId": "m-1",
         "url": "rtsp://admin:Rahasia123@192.168.1.5/V_ENC_001",
         "onvif": {"deviceUrl": "http://192.168.1.5/onvif/device_service", "username": "admin",
                   "password": "Rahasia123", "profileToken": "P1"}},
        {"id": 2, "name": "Alun", "folder": "Jepara", "type": "mjpeg", "url": "http://127.0.0.2:1/b.mjpg"},
    ]
    CAMS.append(CAMS[0])  # induk memakai CAMS[2] sebagai kamera manual

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        d = Path(cls.tmp.name)
        cls.extra = [mock.patch.object(server, "AUDIT", audit_mod.AuditLog(d / "audit.db")),
                     mock.patch.object(server, "PREFS_DIR", d / "prefs")]
        for p in cls.extra:
            p.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for p in reversed(cls.extra):
            p.stop()
        super().tearDownClass()

    def test_roles_permissions_and_regions(self) -> None:
        pass

    def test_redaction_ptz_and_csrf(self) -> None:
        self.assertEqual(server.redact_url("rtsp://a:b%40c@1.2.3.4:554/x"), "rtsp://***@1.2.3.4:554/x")
        self.assertEqual(server.redact_url("http://host/x?y=@z"), "http://host/x?y=@z")
        admin = self._client("admin", users_mod.DEFAULT_PASSWORD, unlock_pw="adminSec12345")
        self._call(admin, "POST", "/api/roles", {"name": "Satpam", "permissions": ["ptz.control"]})
        self._call(admin, "POST", "/api/users", {"username": "satpam9", "password": "satpam12345", "role": "satpam"})
        sat = self._client("satpam9", "satpam12345", unlock_pw="satpamSec12345")
        st, cams = self._call(sat, "GET", "/api/cameras")
        teras = cams["cameras"][0]
        self.assertNotIn("Rahasia123", json.dumps(cams))
        self.assertEqual(teras["url"], "rtsp://***@192.168.1.5/V_ENC_001")
        self.assertEqual(teras["onvif"]["profileToken"], "P1")        # pad PTZ tetap tampil
        st, mc = self._call(sat, "GET", "/api/manual-cameras")
        self.assertNotIn("Rahasia123", json.dumps(mc))
        st, cams = self._call(admin, "GET", "/api/cameras")             # pengelola tetap lihat lengkap
        self.assertIn("Rahasia123", json.dumps(cams))
        # kamera LAN 192.168.1.5: di produksi hanya bisa dipakai bila CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS=1
        with mock.patch("server._onvif_soap_call") as soap, \
                mock.patch("camera_store.ALLOW_PRIVATE_CAMERA_HOSTS", True):
            st, _ = self._call(sat, "POST", "/api/onvif/ptz", {"manualId": "m-1", "action": "left"})
            self.assertEqual(st, 200)
            args = soap.call_args[0]
            self.assertEqual((args[2], args[3]), ("admin", "Rahasia123"))  # kredensial dari server
        # CSRF: Origin asing ditolak, Origin sama & tanpa Origin lolos
        req = urllib.request.Request(self.base + "/api/prefs", data=b'{"prefs":{}}', method="POST",
                                     headers={"Content-Type": "text/plain", "Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            sat.open(req, timeout=5)
        try:
            self.assertEqual(ctx.exception.code, 403)
        finally:
            ctx.exception.close()
        host = self.base.split("://", 1)[1]
        req = urllib.request.Request(self.base + "/api/prefs", data=b'{"prefs":{}}', method="POST",
                                     headers={"Content-Type": "application/json", "Origin": f"http://{host}"})
        with sat.open(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

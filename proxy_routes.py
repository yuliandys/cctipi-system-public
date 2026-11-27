#!/usr/bin/env python3
"""Rute & utilitas reverse-proxy stream HLS/MJPEG/FLV dan jembatan RTSP -> HLS."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import queue
import secrets
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from pathlib import Path

import camera_store
import net_guard
from net_guard import SafeHTTPConnection, SafeHTTPSConnection
from scrapers import USER_AGENT, _ssl_ctx
from system_health import CapacityError, log_event

SEGMENT_BUFFER_LIMIT = 16 * 1024 * 1024
_SEGMENT_TYPES = (
    "video/mp2t", "video/mp4", "video/iso.segment", "video/mpeg",
    "audio/aac", "audio/mp4", "audio/mpeg",
    "application/octet-stream", "binary/octet-stream",
)
_SEGMENT_EXTS = (".ts", ".m4s", ".mp4", ".m4v", ".aac", ".m4a", ".cmfv", ".cmfa", ".fmp4")
_STREAM_HINTS = (".mjpg", ".mjpeg", ".cgi", "nph-zms", "mjpeg", "/video")

RTSP_TMP_ROOT = Path(tempfile.gettempdir()) / "cctipi_rtsp_hls"
RTSP_IDLE_SECONDS = 90
FFMPEG_IO_TIMEOUT_US = 15_000_000

_pool_lock = threading.Lock()
_conn_pools: dict[tuple[str, int], queue.LifoQueue] = {}
_rtsp_lock = threading.Lock()
_rtsp_bridges: dict[str, dict] = {}

# Kunci HMAC per-proses untuk menandatangani URL segmen/kunci hasil rewrite M3U8.
_SIGN_KEY = secrets.token_bytes(32)

# Content-Type upstream yang boleh diteruskan apa adanya. Selain ini (mis. text/html,
# image/svg+xml) dipaksa menjadi octet-stream agar tidak bisa dieksekusi sebagai
# dokumen di origin CCTipi (XSS lewat proxy).
_SAFE_CT_PREFIXES = ("video/", "audio/", "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
_SAFE_CT_EXACT = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl",
    "application/octet-stream", "binary/octet-stream", "multipart/x-mixed-replace",
    "application/dash+xml", "application/mp4",
}
PROXY_CSP = "default-src 'none'; sandbox"


def sign_url(url: str) -> str:
    return hmac.new(_SIGN_KEY, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_url_sig(url: str, sig: str) -> bool:
    return bool(sig) and hmac.compare_digest(sign_url(url), sig)


def safe_content_type(content_type: str) -> str:
    raw = (content_type or "").strip()
    base = raw.split(";", 1)[0].strip().lower()
    if base in _SAFE_CT_EXACT or (base.startswith(_SAFE_CT_PREFIXES) and "svg" not in base and "xml" not in base):
        return raw
    return "application/octet-stream"


def _srv():
    return sys.modules.get("server")


def _acquire_conn(host: str, port: int) -> http.client.HTTPSConnection:
    key = (host, port)
    with _pool_lock:
        q = _conn_pools.get(key)
        if q is None:
            q = queue.LifoQueue(maxsize=32)
            _conn_pools[key] = q
    try:
        return q.get_nowait()
    except queue.Empty:
        return SafeHTTPSConnection(host, port, timeout=20, context=_ssl_ctx)


def _release_conn(host: str, port: int, conn: http.client.HTTPSConnection) -> None:
    key = (host, port)
    with _pool_lock:
        q = _conn_pools.get(key)
    if q is not None:
        try:
            q.put_nowait(conn)
            return
        except queue.Full:
            pass
    try:
        conn.close()
    except Exception:
        pass


def fetch_upstream_pooled(target_url: str, headers: dict[str, str], url_validator=None):
    """HTTP/HTTPS GET dengan koneksi keep-alive pooled & validasi maks 3 redirect."""
    srv = _srv()
    acquire = srv._acquire_conn if srv else _acquire_conn
    release = srv._release_conn if srv else _release_conn
    url = target_url
    for _ in range(4):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        conn = acquire(host, port) if parsed.scheme == "https" else SafeHTTPConnection(host, port, timeout=20)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
        except net_guard.UnsafeAddressError:
            try:
                conn.close()
            except Exception:
                pass
            raise
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = (
                SafeHTTPSConnection(host, port, timeout=20, context=_ssl_ctx)
                if parsed.scheme == "https"
                else SafeHTTPConnection(host, port, timeout=20)
            )
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()

        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            if not loc:
                return resp, conn, url, host, port
            next_url = urllib.parse.urljoin(url, loc)
            resp.read()
            if parsed.scheme == "https":
                release(host, port, conn)
            else:
                conn.close()
            if url_validator is not None and not url_validator(next_url):
                raise PermissionError(f"Disallowed redirect target: {next_url}")
            url = next_url
            continue

        return resp, conn, url, host, port

    raise RuntimeError("Too many redirects")


def rewrite_m3u8(content: str, base_url: str) -> str:
    """Tulis ulang URI segmen & kunci di dalam M3U8 agar melewati /api/proxy."""

    def proxy_uri(uri: str) -> str:
        abs_url = urllib.parse.urljoin(base_url, uri.strip())
        return f"/api/proxy?url={urllib.parse.quote(abs_url, safe='')}&sig={sign_url(abs_url)}"

    def replace_attr_uri(match: re.Match) -> str:
        return f'URI="{proxy_uri(match.group(1))}"'

    out_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
        elif stripped.startswith("#"):
            out_lines.append(re.sub(r'URI="([^"]+)"', replace_attr_uri, line))
        else:
            out_lines.append(proxy_uri(stripped))
    return "\n".join(out_lines) + "\n"


def _upstream_origin_referer(target_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(target_url)
    host_lower = (parsed.hostname or "").lower()
    cached = camera_store._host_portals.get(host_lower)
    if cached is not None:
        return cached
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    return f"{scheme}://{netloc}", f"{scheme}://{netloc}/"


def _host_is_mjpeg(content_type: str) -> bool:
    return "multipart/x-mixed-replace" in content_type.lower()


def _is_finite_segment(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    path = urllib.parse.urlsplit(url).path.lower()
    if _host_is_mjpeg(ct) or "jpeg" in ct or "mjpg" in ct:
        return False
    if any(h in path for h in _STREAM_HINTS):
        return False
    return ct.split(";")[0].strip() in _SEGMENT_TYPES or path.endswith(_SEGMENT_EXTS)


def _read_capped(resp, limit: int) -> tuple[bytes, bool]:
    parts: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = resp.read1(65536)
        if not chunk:
            return b"".join(parts), True
        parts.append(chunk)
        total += len(chunk)
    return b"".join(parts), False


def is_allowed_stream_url(target_url: str) -> bool:
    srv = _srv()
    hosts_fn = srv.allowed_stream_hosts if srv else camera_store.allowed_stream_hosts
    try:
        parsed = urllib.parse.urlparse(target_url)
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and (parsed.hostname or "").lower() in hosts_fn()
    )


def _open_mjpeg_upstream(url: str):
    srv = _srv()
    validator = srv.is_allowed_stream_url if srv else is_allowed_stream_url
    origin, referer = _upstream_origin_referer(url)
    headers = {"User-Agent": USER_AGENT, "Origin": origin, "Referer": referer, "Connection": "close"}
    resp, conn, _final, _h, _p = fetch_upstream_pooled(url, headers, url_validator=validator)
    if resp.status >= 400:
        conn.close()
        raise RuntimeError(f"Sumber membalas HTTP {resp.status}")
    return resp, conn.close


# ---------- RTSP -> HLS Bridge ----------

def _stop_bridge(manual_id: str, bridge: dict) -> None:
    srv = _srv()
    proc = bridge.get("proc")
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass
    if bridge.pop("slot_acquired", False) and srv is not None:
        srv.LOAD_GUARD.release_ffmpeg("rtsp")
    out_dir = RTSP_TMP_ROOT / re.sub(r"[^a-zA-Z0-9_-]", "", manual_id)
    shutil.rmtree(out_dir, ignore_errors=True)


def active_rtsp_bridges_count() -> int:
    with _rtsp_lock:
        return sum(1 for b in _rtsp_bridges.values() if b["proc"].poll() is None)


def reap_idle_rtsp_bridges(force: bool = False) -> int:
    now = time.time()
    victims: list[tuple[str, dict]] = []
    with _rtsp_lock:
        for mid, bridge in list(_rtsp_bridges.items()):
            dead = bridge["proc"].poll() is not None
            idle = now - bridge.get("last_access", 0) > RTSP_IDLE_SECONDS
            if force or dead or idle:
                victims.append((mid, _rtsp_bridges.pop(mid)))
    for mid, bridge in victims:
        _stop_bridge(mid, bridge)
    return len(victims)


def _rtsp_reaper_loop() -> None:
    while True:
        time.sleep(30)
        try:
            reap_idle_rtsp_bridges()
        except Exception as exc:
            log_event("WARNING", "rtsp", "Reaper error", error=str(exc))


def _probe_rtsp_video_codec(rtsp_url: str) -> str:
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return ""
    try:
        res = subprocess.run(
            [
                ffprobe_bin, "-v", "error", "-rtsp_transport", "tcp",
                "-timeout", "8000000", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", rtsp_url,
            ],
            capture_output=True, text=True, timeout=9,
        )
        return (res.stdout or "").strip().lower()
    except Exception:
        return ""


def ensure_rtsp_hls(manual_id: str, rtsp_url: str) -> Path:
    """Mulai jembatan ffmpeg RTSP->HLS sesuai permintaan dengan batas konkurensi."""
    srv = _srv()
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg tidak ditemukan di sistem untuk konversi RTSP -> HLS.")

    reap_idle_rtsp_bridges()
    out_dir = RTSP_TMP_ROOT / re.sub(r"[^a-zA-Z0-9_-]", "", manual_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    m3u8_path = out_dir / "index.m3u8"

    with _rtsp_lock:
        existing = _rtsp_bridges.get(manual_id)
        if existing and existing["proc"].poll() is None and (
            existing["url"] == rtsp_url or manual_id.startswith("tuya-")
        ):
            existing["last_access"] = time.time()
            return out_dir

    # ffprobe/ffmpeg tersambung sendiri (bukan lewat net_guard): periksa dulu
    # bahwa host RTSP tidak me-resolve ke alamat internal terlarang.
    net_guard.check_url_host(rtsp_url)
    vcodec = _probe_rtsp_video_codec(rtsp_url)

    # Hentikan jembatan lama TANPA memegang _rtsp_lock (proc.wait bisa 5 dtk
    # dan dulu menahan semua permintaan RTSP lain).
    while True:
        with _rtsp_lock:
            existing = _rtsp_bridges.get(manual_id)
            if existing and existing["proc"].poll() is None and existing["url"] == rtsp_url:
                existing["last_access"] = time.time()
                return out_dir
            if existing:
                _rtsp_bridges.pop(manual_id, None)
        if not existing:
            break
        _stop_bridge(manual_id, existing)
        out_dir.mkdir(parents=True, exist_ok=True)

    with _rtsp_lock:
        if manual_id in _rtsp_bridges:
            # Permintaan paralel sudah memulai jembatan baru lebih dulu.
            _rtsp_bridges[manual_id]["last_access"] = time.time()
            return out_dir

        guard = srv.LOAD_GUARD if srv else None
        if guard is not None:
            if len(_rtsp_bridges) >= guard.max_rtsp_bridges:
                raise CapacityError(
                    f"Batas jembatan stream RTSP aktif tercapai (maks {guard.max_rtsp_bridges} kamera)."
                )
            guard.acquire_ffmpeg("rtsp")

        try:
            for old_file in out_dir.glob("*"):
                try:
                    old_file.unlink()
                except Exception:
                    pass

            v_args = (
                ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "30"]
                if (vcodec and vcodec != "h264")
                else ["-c:v", "copy"]
            )
            cmd = [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-rtsp_transport", "tcp",
                "-timeout", str(FFMPEG_IO_TIMEOUT_US), "-i", rtsp_url,
                *v_args, "-c:a", "aac", "-b:a", "64k", "-f", "hls",
                "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments+append_list+independent_segments",
                str(m3u8_path),
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _rtsp_bridges[manual_id] = {
                "proc": proc,
                "url": rtsp_url,
                "last_access": time.time(),
                "slot_acquired": guard is not None,
            }
        except Exception:
            if guard is not None:
                guard.release_ffmpeg("rtsp")
            raise

    for _ in range(75):
        if m3u8_path.exists() and m3u8_path.stat().st_size > 0:
            break
        if proc.poll() is not None:
            reap_idle_rtsp_bridges()
            raise RuntimeError("ffmpeg gagal terhubung ke stream RTSP.")
        time.sleep(0.2)

    return out_dir


# ---------- HTTP Route Handlers ----------

def handle_rtsp_hls(handler, path: str) -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    if len(parts) != 4:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Path RTSP tidak valid.")
        return
    manual_id = urllib.parse.unquote(parts[2])
    filename = urllib.parse.unquote(parts[3])
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", filename) or ".." in filename:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Nama file tidak valid.")
        return
    if manual_id.startswith("tuya-") and srv.tuya_service is not None:
        if not handler._key_ok(f"m:{manual_id}"):
            handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
            return
        dev_id = manual_id[5:]
        if not any(c.get("tuyaDeviceId") == dev_id for c in srv.tuya_service.get_cameras()):
            handler.send_json_error(HTTPStatus.NOT_FOUND, "Kamera Tuya tidak ditemukan.")
            return
        try:
            with _rtsp_lock:
                running = _rtsp_bridges.get(manual_id)
                running_url = running["url"] if running and running["proc"].poll() is None else None
            rtsp_url = running_url or srv.tuya_service.allocate_stream(dev_id, "rtsp")[0]
        except Exception as exc:
            handler.send_json_error(HTTPStatus.BAD_GATEWAY, str(exc))
            return
    else:
        cam = next((c for c in srv.load_manual_cameras() if c.get("manualId") == manual_id), None)
        if not cam or not cam.get("url", "").startswith("rtsp://") or not handler._cam_ok(cam):
            handler.send_json_error(HTTPStatus.NOT_FOUND, "Kamera RTSP tidak ditemukan.")
            return
        rtsp_url = cam["url"]
    try:
        out_dir = ensure_rtsp_hls(manual_id, rtsp_url)
        file_path = (out_dir / filename).resolve()
        if not file_path.is_relative_to(out_dir.resolve()) or not file_path.is_file():
            handler.send_json_error(HTTPStatus.NOT_FOUND, "Segmen belum siap.")
            return
        data = file_path.read_bytes()
        ctype = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
        handler.send_response(HTTPStatus.OK)
        handler.send_cors_headers()
        handler.send_header("Content-Type", ctype)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except CapacityError as exc:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, str(exc))


def handle_mjpeg_frame(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    target_url = params.get("url", [""])[0].strip()
    if not target_url or not srv.is_allowed_stream_url(target_url):
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Invalid or disallowed stream URL.")
        return
    if not handler._url_ok(target_url):
        handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
        return
    try:
        after = int(params.get("seq", ["0"])[0] or 0)
    except ValueError:
        after = 0
    try:
        with srv.LOAD_GUARD.viewer_slot(f"mjpeg:{target_url.split('?', 1)[0]}"):
            frame, seq, error = srv.mjpeg_hub.get_frame(target_url, after_seq=after)
    except (OverflowError, CapacityError) as exc:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        return
    if frame is None:
        handler.send_json_error(
            HTTPStatus.GATEWAY_TIMEOUT if not error else HTTPStatus.BAD_GATEWAY,
            error or "Kamera belum mengirim gambar.",
        )
        return
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(frame)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Frame-Seq", str(seq))
    handler.end_headers()
    handler.wfile.write(frame)


def _pump_stream(handler, resp, first: bytes = b"") -> None:
    handler.close_connection = True
    handler.send_header("Connection", "close")
    handler.end_headers()
    if first:
        handler.wfile.write(first)
        handler.wfile.flush()
    while True:
        chunk = resp.read1(16384)
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()


def handle_proxy(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    target_url = params.get("url", [""])[0].strip()

    if not target_url or not srv.is_allowed_stream_url(target_url):
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Invalid or disallowed stream URL.")
        return
    # Segmen/kunci hasil rewrite M3U8 membawa tanda tangan HMAC: playlist induknya
    # sudah lolos cek wilayah, jadi segmen di host CDN lain tetap boleh diputar.
    signed = verify_url_sig(target_url, params.get("sig", [""])[0].strip())
    if not signed and not handler._url_ok(target_url, host_only=True):
        handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
        return

    origin, referer = _upstream_origin_referer(target_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": origin,
        "Referer": referer,
        "Connection": "keep-alive",
    }
    conn = None
    headers_started = False
    used_host = ""
    used_port = 443
    release = srv._release_conn if srv else _release_conn
    slot_key = f"stream:{target_url.split('?', 1)[0]}"
    try:
        with srv.LOAD_GUARD.viewer_slot(slot_key):
            resp, conn, final_url, used_host, used_port = srv.fetch_upstream_pooled(
                target_url, headers, url_validator=srv.is_allowed_stream_url
            )
            status = resp.status
            raw_content_type = resp.getheader("Content-Type") or "application/octet-stream"
            is_playlist = "mpegurl" in raw_content_type.lower() or final_url.split("?")[0].lower().endswith(".m3u8")
            content_type = safe_content_type(raw_content_type)

            if is_playlist:
                raw_text = resp.read().decode("utf-8", errors="replace")
                if final_url.startswith("https://") and status < 400:
                    release(used_host, used_port, conn)
                else:
                    conn.close()
                conn = None
                rewritten = rewrite_m3u8(raw_text, final_url).encode("utf-8")
                handler.send_response(status)
                headers_started = True
                handler.send_cors_headers()
                handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
                handler.send_header("Cache-Control", "no-store, no-cache")
                handler.send_header("Content-Length", str(len(rewritten)))
                handler.end_headers()
                handler.wfile.write(rewritten)
                return

            content_len = resp.getheader("Content-Length")
            if content_len:
                handler.send_response(status)
                headers_started = True
                handler.send_cors_headers()
                handler.send_header("Content-Type", content_type)
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Content-Length", content_len)
                handler.end_headers()
                while True:
                    chunk = resp.read(32768)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                if final_url.startswith("https://") and status < 400:
                    release(used_host, used_port, conn)
                else:
                    conn.close()
                conn = None
                return

            if _is_finite_segment(content_type, final_url):
                body_bytes, complete = _read_capped(resp, SEGMENT_BUFFER_LIMIT)
                if complete:
                    if final_url.startswith("https://") and status < 400:
                        release(used_host, used_port, conn)
                    else:
                        conn.close()
                    conn = None
                    handler.send_response(status)
                    headers_started = True
                    handler.send_cors_headers()
                    handler.send_header("Content-Type", content_type)
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("Content-Length", str(len(body_bytes)))
                    handler.end_headers()
                    handler.wfile.write(body_bytes)
                else:
                    handler.send_response(status)
                    headers_started = True
                    handler.send_cors_headers()
                    handler.send_header("Content-Type", content_type)
                    handler.send_header("Cache-Control", "no-store")
                    _pump_stream(handler, resp, first=body_bytes)
                    conn.close()
                    conn = None
                return

            handler.send_response(status)
            headers_started = True
            handler.send_cors_headers()
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            _pump_stream(handler, resp)
            conn.close()
            conn = None
    except CapacityError as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if not headers_started:
            handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if headers_started:
            handler.close_connection = True
        else:
            handler.send_json_error(HTTPStatus.BAD_GATEWAY, f"Upstream error: {exc}")

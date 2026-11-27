#!/usr/bin/env python3
"""CCTipi System v1.1 — HTTP Adapter, Routing & Service Composition.

Logika domain dipecah ke modul terpisah (< 600 baris per modul):
- auth_routes.py       : Sesi, login/rate-limit, RBAC, kelola user/role, audit, preferensi
- camera_routes.py     : Sumber stream, validasi SSRF allowlist, kamera manual, cache
- proxy_routes.py      : Reverse proxy HLS/MJPEG/FLV, koneksi keep-alive, bridge RTSP->HLS
- onvif_tuya_routes.py : ONVIF auto-discover/PTZ SOAP & Tuya IoT Cloud
- recording_routes.py  : Daftar/putar/hapus rekaman server & HTTP Range 206/416
- ai_routes.py         : AI Jepretan, Zona Deteksi, Live AI realtime, dan Alarm Monitor
- system_health.py     : Validasi config awal, batas beban ffmpeg/viewer, log & /api/health
"""

from __future__ import annotations

import http.cookies
import json
import mimetypes
import os
import ssl
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Modul rute mengakses state server lewat sys.modules["server"]. Saat dijalankan
# sebagai `python server.py` modul ini bernama "__main__", sehingga tanpa alias
# ini semua handler (termasuk login) mendapat None dan gagal (AttributeError).
sys.modules.setdefault("server", sys.modules[__name__])

import ai_routes, audit as _audit, auth_routes, camera_routes, monitor as _monitor
import onvif_tuya_routes, proxy_routes, recording_routes, scrapers, users as _users
from auth_routes import (
    SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, LoginLimiter, SessionStore,
    SlidingWindowLimiter, audit_action_for, permission_for, prefs_path, rate_limit_rule_for, settings_diff,
)
from camera_store import (
    BASE_STREAM_HOSTS, CACHE_TTL_SECONDS, CAMERAS_CACHE_FILE, CUSTOM_SOURCES_FILE,
    DEFAULT_SOURCES, DIRECT_CORS_HOSTS, FALLBACK_CAMERAS_FILE, MANUAL_CAMERAS_FILE,
    _cache_lock, _cameras_cache, _refresh_cameras_sync, _SCRAPER_URL_DEFAULTS,
    _trigger_bg_refresh, allowed_stream_hosts, apply_sources_config, get_cameras,
    load_fallback_cameras, load_manual_cameras, load_region_cache, load_sources_config,
    probe_camera, redact_camera, save_manual_cameras, save_region_cache, validate_camera_url, validate_stream_host,
)
from mjpeg_hub import MjpegHub
from onvif_tuya_routes import _onvif_soap_call
from proxy_routes import (
    RTSP_IDLE_SECONDS, RTSP_TMP_ROOT, SEGMENT_BUFFER_LIMIT, _acquire_conn, _host_is_mjpeg,
    _is_finite_segment, _open_mjpeg_upstream, _read_capped, _release_conn, _rtsp_reaper_loop,
    _upstream_origin_referer, active_rtsp_bridges_count, ensure_rtsp_hls,
    fetch_upstream_pooled, is_allowed_stream_url, reap_idle_rtsp_bridges, rewrite_m3u8,
)
from scrapers import BUILTIN_FETCHERS, USER_AGENT, fetch_custom_region_cameras, mark_token_expiry
from storage_utils import redact_sensitive_text, redact_url
from system_health import LOAD_GUARD, CapacityError, ConfigError, build_health_report, log_event, validate_startup_config

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
STATE_DIR = Path(os.environ.get("CCTIPI_STATE_DIR") or BASE_DIR).resolve()  # data runtime & rahasia
AUTH_CONFIG_FILE = STATE_DIR / "auth.json"
DATA_DIR = STATE_DIR / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
PREFS_DIR = DATA_DIR / "prefs"

DEFAULT_PASSWORD = _users.DEFAULT_PASSWORD
MIN_PASSWORD_LEN = _users.MIN_PASSWORD_LEN
_hash_password = _users.hash_password
_verify_password = _users.verify_password

_SECURE_COOKIE_ENV = os.environ.get("CCTIPI_SECURE_COOKIE", "").strip().lower()
SECURE_COOKIE = _SECURE_COOKIE_ENV in ("1", "true", "yes")
SECURE_COOKIE_DISABLED = _SECURE_COOKIE_ENV in ("0", "false", "no")
PUBLIC_HTTPS = os.environ.get("CCTIPI_PUBLIC_URL", "").strip().lower().startswith("https://")
TRUST_PROXY = os.environ.get("CCTIPI_TRUST_PROXY", "").lower() in ("1", "true", "yes")
try:
    # Jumlah reverse proxy tepercaya di depan CCTipi. IP klien = entri ke-N dari
    # KANAN pada X-Forwarded-For (entri kiri bisa dipalsukan klien).
    TRUST_PROXY_HOPS = max(1, int(os.environ.get("CCTIPI_TRUST_PROXY_HOPS", "1")))
except ValueError:
    TRUST_PROXY_HOPS = 1


def harden_permissions() -> None:
    """Rapikan izin berkas sensitif yang sudah ada."""
    for path in (
        AUTH_CONFIG_FILE, MANUAL_CAMERAS_FILE, SESSIONS_FILE,
        DATA_DIR / "ai_settings.json", DATA_DIR / "recorder_settings.json", DATA_DIR / "tuya_settings.json",
    ):
        try:
            if path.exists():
                os.chmod(str(path), 0o600)
        except OSError:
            pass


harden_permissions()
SESSIONS = SessionStore(SESSIONS_FILE)
USERS = _users.UserStore(AUTH_CONFIG_FILE, on_revoke=SESSIONS.revoke_user)
USERS.reload()
AUDIT = _audit.AuditLog(DATA_DIR / "audit.db")
LOGIN_LIMITER = LoginLimiter()
ACTION_LIMITER = SlidingWindowLimiter()

apply_sources_config(load_sources_config())
load_manual_cameras()
mjpeg_hub = MjpegHub(_open_mjpeg_upstream)

import importlib


def _load_optional(mod_name: str, builder):
    try:
        mod = importlib.import_module(mod_name)
        return mod, builder(mod), ""
    except Exception as exc:
        return None, None, f"Modul {mod_name}.py tidak termuat: {exc}"


_ai_module, ai_service, _ai_import_error = _load_optional("ai", lambda m: m.AiService(lambda: get_cameras()))
_, recorder_service, _recorder_import_error = _load_optional("recorder", lambda m: m.RecorderService(lambda: get_cameras()))
if recorder_service is not None and ai_service is not None:
    ai_service.on_detection = recorder_service.trigger_clip
_, live_ai_service, _live_ai_import_error = _load_optional("live_ai", lambda m: m.LiveAiService(lambda: get_cameras(), ai_service))
_, tuya_service, _tuya_import_error = _load_optional("tuya", lambda m: m.TuyaService())


def _camera_key(cam: dict) -> str:
    if cam.get("manualId"):
        return f"m:{cam['manualId']}"
    return _ai_module.camera_key(cam) if _ai_module is not None else f"u:{cam.get('url', '') or ''}"


def _normalize_key(key: str) -> str:
    return _ai_module.normalize_key(key) if _ai_module is not None else key


monitor_service = _monitor.MonitorService(
    DATA_DIR / "monitor_settings.json",
    get_cameras=lambda: get_cameras().get("cameras", []),
    camera_key=lambda cam: _camera_key(cam),
    probe=lambda cam: probe_camera(cam),
    send_text=ai_service.send_telegram_text if ai_service is not None else None,
    audit=lambda user, action, target, detail: AUDIT.log(user, action, target, detail),
    data_dir=DATA_DIR,
    recorder_status=recorder_service.status if recorder_service is not None else None,
)


def handle_health(handler) -> None:
    """Liveness minimal untuk publik (healthcheck Docker); laporan lengkap hanya untuk user login."""
    ctx = handler.get_ctx()
    if not ctx or ctx.get("mustChange"):
        handler._send_json({"ok": True, "status": "alive"})
        return
    handler._send_json(build_health_report(
        data_dir=DATA_DIR, cameras_payload=get_cameras(),
        rtsp_bridges_active=active_rtsp_bridges_count(), recorder_service=recorder_service,
        ai_service=ai_service, live_ai_service=live_ai_service,
        monitor_service=monitor_service, mjpeg_hub=mjpeg_hub, guard=LOAD_GUARD,
    ))


_GET_EXACT = {
    "/api/users": lambda h, _q: auth_routes.handle_users_list(h),
    "/api/audit": auth_routes.handle_audit_list,
    "/api/prefs": lambda h, _q: auth_routes.handle_prefs_get(h),
    "/api/cameras": camera_routes.handle_cameras,
    "/api/sources": lambda h, _q: camera_routes.handle_get_sources(h),
    "/api/manual-cameras": lambda h, _q: camera_routes.handle_manual_cameras_get(h),
    "/api/rekaman": recording_routes.handle_rekaman_list,
    "/api/rekaman/status": lambda h, _q: recording_routes.handle_rekaman_status(h),
    "/api/rekaman/settings": lambda h, _q: recording_routes.handle_rekaman_settings_get(h),
    "/api/rekaman/find": recording_routes.handle_rekaman_find,
    "/api/jepretan": ai_routes.handle_jepretan_list,
    "/api/ai/status": lambda h, _q: ai_routes.handle_ai_status(h),
    "/api/ai/settings": lambda h, _q: ai_routes.handle_ai_settings_get(h),
    "/api/ai/zones": lambda h, _q: ai_routes.handle_zones_get(h),
    "/api/ai/snapshot": ai_routes.handle_ai_snapshot,
    "/api/live-ai/status": ai_routes.handle_live_ai_status,
    "/api/monitor/status": lambda h, _q: ai_routes.handle_monitor_status(h),
    "/api/monitor/settings": lambda h, _q: ai_routes.handle_monitor_settings_get(h),
    "/api/tuya/settings": lambda h, _q: onvif_tuya_routes.handle_tuya_settings_get(h),
    "/api/proxy": proxy_routes.handle_proxy,
    "/api/mjpeg-frame": proxy_routes.handle_mjpeg_frame,
    "/api/mjpeg-hub": lambda h, _q: h._send_json(mjpeg_hub.stats()),
    "/api/health": lambda h, _q: handle_health(h),
}

_POST_EXACT = {
    "/api/auth/password": auth_routes.handle_change_password,
    "/api/users": auth_routes.handle_user_create,
    "/api/roles": auth_routes.handle_role_save,
    "/api/prefs": auth_routes.handle_prefs_post,
    "/api/rekaman/settings": recording_routes.handle_rekaman_settings_post,
    "/api/ai/settings": ai_routes.handle_ai_settings_post,
    "/api/monitor/settings": ai_routes.handle_monitor_settings_post,
    "/api/sources": camera_routes.handle_save_sources,
    "/api/sources/reset": camera_routes.handle_reset_sources,
    "/api/check-status": camera_routes.handle_check_status,
    "/api/manual-cameras": camera_routes.handle_add_manual_camera,
    "/api/backup/import": camera_routes.handle_backup_import,
    "/api/onvif/discover": onvif_tuya_routes.handle_onvif_discover,
    "/api/onvif/ptz": onvif_tuya_routes.handle_onvif_ptz,
    "/api/tuya/settings": onvif_tuya_routes.handle_tuya_settings_post,
    "/api/tuya/test": onvif_tuya_routes.handle_tuya_test,
    "/api/tuya/ptz": onvif_tuya_routes.handle_tuya_ptz,
}


class CctipiHandler(BaseHTTPRequestHandler):
    """Adaptor HTTP ramping untuk API, proxy stream, dan berkas statis."""

    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    CSP = "; ".join([
        "default-src 'self'",
        # Hanya path pustaka yang dipakai (bukan seluruh CDN) agar CSP tidak bisa
        # dilewati dengan memuat paket npm sembarang dari jsdelivr/unpkg.
        "script-src 'self' https://cdn.jsdelivr.net/npm/hls.js@1.7.4/ https://cdn.jsdelivr.net/npm/flv.js@1.5.0/ https://unpkg.com/leaflet@1.9.4/",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com/leaflet@1.9.4/",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://unpkg.com https:",
        "media-src 'self' blob: https:",
        "connect-src 'self' https:",
        "worker-src 'self' blob:",
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com",
        "object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'",
    ])

    def log_message(self, fmt: str, *args) -> None:
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *(redact_sensitive_text(str(a)) for a in args))

    def is_https_request(self) -> bool:
        """Deteksi apakah permintaan disajikan lewat HTTPS (TLS langsung atau reverse proxy)."""
        if SECURE_COOKIE_DISABLED:
            return False
        if SECURE_COOKIE or PUBLIC_HTTPS or isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return True
        if TRUST_PROXY and (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https":
            return True
        origin = (self.headers.get("Origin") or "").strip().lower()
        return origin.startswith("https://") and not self._cross_origin()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.is_https_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if getattr(self, "_is_html", False):
            self.send_header("Content-Security-Policy", self.CSP)
        else:
            # Respons non-HTML (API, proxy stream, rekaman, gambar) tidak boleh
            # pernah dijalankan sebagai dokumen aktif di origin ini.
            self.send_header("Content-Security-Policy", proxy_routes.PROXY_CSP)
        super().end_headers()

    def client_ip(self) -> str:
        if TRUST_PROXY:
            hops = [h.strip() for h in self.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
            if hops:
                return hops[-TRUST_PROXY_HOPS] if len(hops) >= TRUST_PROXY_HOPS else hops[0]
        return self.client_address[0]

    def session_cookie_header(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.is_https_request() else ""
        return f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    def read_json_body(self, limit: int = 1_000_000) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > limit:
            raise ValueError("payload terlalu besar")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if length and ctype != "application/json":
            # Form lintas-situs (text/plain, x-www-form-urlencoded) tidak bisa
            # mengirim application/json tanpa preflight CORS -> tambahan anti-CSRF.
            raise ValueError("Content-Type harus application/json")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("payload harus objek JSON")
        return body

    def send_cors_headers(self) -> None:
        origin = (self.headers.get("Origin") or "").strip()
        host = ((self.headers.get("X-Forwarded-Host") if TRUST_PROXY else None) or self.headers.get("Host") or "").strip()
        if origin and host:
            try:
                if urllib.parse.urlparse(origin).netloc.lower() == host.lower():
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
            except ValueError:
                pass
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def session_token(self) -> str:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return ""
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def get_ctx(self) -> dict | None:
        return USERS.context(SESSIONS.verify(self.session_token()))

    def get_current_user(self) -> str | None:
        ctx = self.get_ctx()
        return ctx["username"] if ctx else None

    def require_auth(self, method: str = "", path: str = "") -> dict | None:
        """Wajib login + paksa ganti password bawaan/awal + cek izin endpoint."""
        self.ctx = None
        ctx = self.get_ctx()
        if not ctx:
            self.send_json_error(HTTPStatus.UNAUTHORIZED, "Unauthorized")
            return None
        if ctx.get("mustChange") and (method, path) != ("POST", "/api/auth/password"):
            self.send_json_error(
                HTTPStatus.FORBIDDEN,
                "Wajib mengganti password bawaan/awal terlebih dahulu sebelum memakai fitur operasional.",
                extra={"mustChange": True},
            )
            return None
        perm = permission_for(method, path) if method else None
        if perm and perm not in ctx["permissions"]:
            self.send_json_error(HTTPStatus.FORBIDDEN, "Akses ditolak: akun Anda tidak memiliki izin untuk fitur ini.")
            return None
        self.ctx = ctx
        return ctx

    def _check_sensitive_rate_limit(self, method: str, path: str) -> bool:
        rule = rate_limit_rule_for(method, path)
        if not rule:
            return True
        bucket, max_calls, window_s = rule
        user = (getattr(self, "ctx", None) or {}).get("username") or "-"
        wait = ACTION_LIMITER.check_and_record([f"{bucket}:ip:{self.client_ip()}", f"{bucket}:u:{user.lower()}"], max_calls, window_s)
        if wait > 0:
            self.send_rate_limited(wait)
            return False
        return True

    def _regions(self) -> list[str] | None:
        return (getattr(self, "ctx", None) or {}).get("regions")

    def _cam_ok(self, cam: dict) -> bool:
        regions = self._regions()
        return regions is None or (cam.get("folder") or "") in regions

    def _allowed_cams(self) -> list[dict]:
        return [c for c in get_cameras().get("cameras", []) if self._cam_ok(c)]

    def _allowed_keys(self) -> list[str] | None:
        return None if self._regions() is None else sorted({_camera_key(c) for c in self._allowed_cams()})

    def _key_ok(self, key: str | None) -> bool:
        return True if self._regions() is None else (bool(key) and _normalize_key(key) in set(self._allowed_keys() or []))

    def _url_ok(self, url: str, *, host_only: bool = False) -> bool:
        if self._regions() is None:
            return True
        cams = self._allowed_cams()
        if host_only:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            return any((urllib.parse.urlparse(c.get("url", "")).hostname or "").lower() == host for c in cams)
        norm = _normalize_key("u:" + url)
        return any(_camera_key({"url": c.get("url", "")}) == norm for c in cams)

    def send_response(self, code, message=None) -> None:
        self._status = int(code)
        super().send_response(code, message)

    def _audit_reset(self, method: str = "", path: str = "") -> None:
        self._status = 0
        self._audit_target = None
        self._audit_detail = ""
        self._audit_method = method
        self._audit_path = path
        self._audited = False

    def audit(self, action: str, target: str = "", detail="", ok: bool = True, username: str | None = None) -> None:
        user = username if username is not None else ((getattr(self, "ctx", None) or {}).get("username") or "-")
        AUDIT.log(user, action, target, detail, ip=self.client_ip(), ok=ok)

    def _audit_after(self, method: str = "", path: str = "") -> None:
        if getattr(self, "_audited", False):
            return
        m = method or getattr(self, "_audit_method", "")
        p = path or getattr(self, "_audit_path", "")
        if not (200 <= getattr(self, "_status", 0) < 400) or not getattr(self, "ctx", None):
            return
        action = audit_action_for(m, p)
        if not action:
            return
        self._audited = True
        target = self._audit_target
        if target is None:
            target = urllib.parse.unquote(p.rstrip("/").rsplit("/", 1)[-1]) if p.count("/") > 2 else ""
        self.audit(action, target, self._audit_detail)

    def _cross_origin(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            # Tanpa Origin: andalkan Fetch Metadata browser modern bila ada.
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            return site not in ("", "same-origin", "none")
        try:
            o = urllib.parse.urlparse(origin)
        except ValueError:
            return True
        host = (self.headers.get("X-Forwarded-Host") if TRUST_PROXY else None) or self.headers.get("Host") or ""
        return o.netloc.lower() != host.strip().lower()

    def _redirect_login(self) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._is_html = False
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            return self.handle_static("/login.html")
        if path == "/api/auth/me":
            return auth_routes.handle_auth_me(self)
        if path == "/api/health":
            return handle_health(self)
        if path.startswith("/api/"):
            if not self.require_auth("GET", path):
                return
            fn = _GET_EXACT.get(path)
            if fn is not None:
                return fn(self, parsed.query)
            if path.startswith("/api/rekaman/"):
                return recording_routes.handle_rekaman_file(self, path, parsed.query)
            if path.startswith("/api/jepretan/"):
                return ai_routes.handle_jepretan_image(self, path, parsed.query)
            if path.startswith("/api/live-ai/") and path.endswith(".mjpg"):
                return ai_routes.handle_live_ai_stream(self, path)
            if path.startswith("/api/rtsp/"):
                return proxy_routes.handle_rtsp_hls(self, path)
            if path.startswith("/api/tuya/hls/"):
                return onvif_tuya_routes.handle_tuya_hls(self, path)
            return self.send_json_error(HTTPStatus.NOT_FOUND, "Not found")
        if path in ("", "/", "/index.html") and not self.get_current_user():
            return self._redirect_login()
        self.handle_static(path)

    def do_POST(self) -> None:
        self._is_html = False
        parsed = urllib.parse.urlparse(self.path)
        self._audit_reset("POST", parsed.path)
        if self._cross_origin():
            self.send_json_error(HTTPStatus.FORBIDDEN, "Permintaan lintas situs ditolak.")
            return
        try:
            self._dispatch_post(parsed.path)
        finally:
            self._audit_after("POST", parsed.path)

    def _dispatch_post(self, path: str) -> None:
        if path == "/api/login":
            auth_routes.handle_login(self)
            return
        if path == "/api/logout":
            auth_routes.handle_logout(self)
            return
        if not self.require_auth("POST", path) or not self._check_sensitive_rate_limit("POST", path):
            return
        fn = _POST_EXACT.get(path)
        if fn is not None:
            fn(self)
        elif path.startswith("/api/users/"):
            auth_routes.handle_user_update(self, path)
        elif path.startswith("/api/ai/zones/"):
            ai_routes.handle_zone_post(self, path)
        elif path.startswith("/api/live-ai/"):
            ai_routes.handle_live_ai_control(self, path)
        else:
            self.send_json_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        self._is_html = False
        parsed = urllib.parse.urlparse(self.path)
        self._audit_reset("DELETE", parsed.path)
        if self._cross_origin():
            self.send_json_error(HTTPStatus.FORBIDDEN, "Permintaan lintas situs ditolak.")
            return
        try:
            if not self.require_auth("DELETE", parsed.path) or not self._check_sensitive_rate_limit("DELETE", parsed.path):
                return
            if parsed.path.startswith("/api/users/"):
                auth_routes.handle_user_delete(self, parsed.path)
            elif parsed.path.startswith("/api/roles/"):
                auth_routes.handle_role_delete(self, parsed.path)
            elif parsed.path == "/api/manual-cameras":
                camera_routes.handle_delete_manual_camera(self, parsed.query)
            elif parsed.path.startswith("/api/rekaman/"):
                recording_routes.handle_rekaman_delete(self, parsed.path)
            elif parsed.path.startswith("/api/jepretan/"):
                ai_routes.handle_jepretan_delete(self, parsed.path)
            else:
                self.send_json_error(HTTPStatus.NOT_FOUND, "Not found")
        finally:
            self._audit_after("DELETE", parsed.path)

    def _send_json(self, payload, status=HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._status = int(status)
        self._audit_after()
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_rate_limited(self, retry_after: int) -> None:
        body = json.dumps({"error": f"Terlalu banyak permintaan/percobaan. Coba lagi dalam {retry_after} detik.", "retryAfter": retry_after}).encode("utf-8")
        self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
        self.send_cors_headers()
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_error(self, status: int, message: str, *, extra: dict | None = None) -> None:
        payload = {"error": redact_sensitive_text(message), **(extra or {})}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def handle_static(self, req_path: str) -> None:
        rel_path = "index.html" if req_path in ("", "/") else req_path.lstrip("/")
        safe_path = (PUBLIC_DIR / rel_path).resolve()
        if not safe_path.is_relative_to(PUBLIC_DIR.resolve()) or not safe_path.is_file():
            safe_path = PUBLIC_DIR / "index.html"
        if not safe_path.is_file():
            self.send_json_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        mime_type, _ = mimetypes.guess_type(str(safe_path))
        if safe_path.name == "index.html" and not self.get_current_user():
            return self._redirect_login()
        self._is_html = (mime_type or "").startswith("text/html")
        content = safe_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class FastThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads, request_queue_size, allow_reuse_address = True, 128, True

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)


def main() -> None:
    try:
        cfg = validate_startup_config(
            host=os.environ.get("HOST", "127.0.0.1"), port_raw=os.environ.get("PORT", "9999"),
            users_store=USERS, data_dir=DATA_DIR, sources=load_sources_config(), ai_service=ai_service,
        )
    except ConfigError as exc:
        log_event("ERROR", "startup", str(exc))
        sys.exit(2)
    host, port = cfg["host"], cfg["port"]
    for warn in cfg["warnings"]:
        log_event("WARNING", "startup", warn)
    server = FastThreadingHTTPServer((host, port), CctipiHandler)
    log_event("INFO", "startup", f"CCTipi System berjalan di http://{host}:{port}", host=host, port=port)
    threading.Thread(target=_rtsp_reaper_loop, name="cctipi-rtsp-reaper", daemon=True).start()
    for name, svc, err in (("ai", ai_service, _ai_import_error), ("recorder", recorder_service, _recorder_import_error)):
        if svc is not None:
            svc.start()
            log_event("INFO", name, f"Modul {name} siaga (opt-in).")
        else:
            log_event("INFO", name, f"Modul {name} tidak aktif: {err}")
    monitor_service.start()
    if live_ai_service is not None:
        ok_live, reason = live_ai_service.available()
        log_event("INFO", "live_ai", "Modul Live AI siaga." if ok_live else f"Live AI tidak tersedia: {reason}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("INFO", "shutdown", "Menghentikan server...")
    finally:
        for svc, m in ((live_ai_service, "shutdown"), (recorder_service, "shutdown"), (ai_service, "stop"), (monitor_service, "stop")):
            if svc is not None:
                getattr(svc, m)()
        reap_idle_rtsp_bridges(force=True)
        server.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Penyimpanan sumber stream, validasi SSRF allowlist, kamera manual, dan cache kamera."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import net_guard
import scrapers
from scrapers import USER_AGENT, _ssl_ctx, mark_token_expiry
from storage_utils import redact_url, write_private
from system_health import log_event

BASE_DIR = Path(__file__).resolve().parent
FALLBACK_CAMERAS_FILE = BASE_DIR / "cameras.json"
STATE_DIR = Path(os.environ.get("CCTIPI_STATE_DIR") or BASE_DIR).resolve()  # data runtime & rahasia
CUSTOM_SOURCES_FILE = STATE_DIR / "sources.json"
MANUAL_CAMERAS_FILE = STATE_DIR / "manual_cameras.json"
DATA_DIR = STATE_DIR / "data"
CAMERAS_CACHE_FILE = DATA_DIR / "cameras_cache.json"
CACHE_TTL_SECONDS = 600

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_LOCAL_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def _srv():
    return sys.modules.get("server")


DEFAULT_SOURCES = [
    {"folder": "Jepara", "title": "Kabupaten Jepara (SAMUDRA)", "portalUrl": "https://samudra.jepara.go.id/cctv", "apiUrl": "https://samudra.jepara.go.id/cctv", "streamHosts": "cctv-jepara.lifemedia.id, stream-cctv.jepara.go.id", "format": "HLS (.m3u8)"},
    {"folder": "Kudus", "title": "Kabupaten Kudus (Diskominfo)", "portalUrl": "https://streaming.kuduskab.go.id/", "apiUrl": "https://kudussehat.kuduskab.go.id/api/get-cctv", "streamHosts": "stream.kuduskab.go.id", "format": "HLS (.m3u8)"},
    {"folder": "Demak", "title": "Kabupaten Demak (Dishub)", "portalUrl": "https://www.youtube.com/@cctvdishubdemak/streams", "apiUrl": "https://www.youtube.com/@cctvdishubdemak/streams", "streamHosts": "www.youtube.com", "format": "YouTube Live Embed"},
    {"folder": "Jombang", "title": "Kabupaten Jombang (Smart Service)", "portalUrl": "https://cctv.jombangkab.go.id/v2/", "apiUrl": "https://cctv.jombangkab.go.id/v2/", "streamHosts": "cctv.jombangkab.go.id", "format": "MJPEG (multipart/x-mixed-replace)"},
    {"folder": "Tol", "title": "Jalan Tol Kertosono–Mojokerto (Bina Marga)", "portalUrl": "https://binamarga.pu.go.id/contents/cctv_tol/?id_ruas=kertosono-mojokerto", "apiUrl": "https://binamarga.pu.go.id/contents/cctv_tol/?id_ruas=kertosono-mojokerto", "streamHosts": "api-cctv.margaharjaya.co.id", "format": "HLS (.m3u8)"},
    {"folder": "Jogja", "title": "Kota Yogyakarta (Pemkot Jogja)", "portalUrl": "https://cctv.jogjakota.go.id/", "apiUrl": "https://cctv.jogjakota.go.id/home/getdata", "streamHosts": "cctvjss.jogjakota.go.id", "format": "HLS (.m3u8)"},
    {"folder": "Semarang", "title": "Kota Semarang (Pantau Semar)", "portalUrl": "https://pantausemar.semarangkota.go.id/", "apiUrl": "https://pantausemar.semarangkota.go.id/", "streamHosts": "livepantau.semarangkota.go.id", "format": "HLS (.m3u8)"},
    {"folder": "Pati", "title": "Kabupaten Pati (Dishub & PA Pati)", "portalUrl": "https://cctvkabpati.biz.id/", "apiUrl": "https://cctvkabpati.biz.id/", "streamHosts": "streaming.patikab.go.id, papati.cctvbadilag2.my.id", "format": "HLS (.m3u8)"},
    {"folder": "Rembang", "title": "Kabupaten Rembang (Dinkominfo)", "portalUrl": "https://cctv.rembangkab.go.id/", "apiUrl": "https://smartcity.rembangkab.go.id/api/v1/cctv", "streamHosts": "cctv.rembangkab.go.id", "format": "HLS (.m3u8)"},
    {"folder": "Solo", "title": "Kota Surakarta / Solo (Dishub)", "portalUrl": "https://ccroom-dishub.surakarta.go.id/cctv", "apiUrl": "https://ccroom-dishub.surakarta.go.id/cctv", "streamHosts": "surakarta.atcsindonesia.info", "format": "HTTP-FLV (.flv)"},
]

_DIRECT_CORS_DEFAULT = [
    "api-cctv.margaharjaya.co.id", "cctvjss.jogjakota.go.id",
    "livepantau.semarangkota.go.id", "cctv.rembangkab.go.id", "surakarta.atcsindonesia.info",
]
_extra_cors = [h.strip().lower() for h in os.environ.get("CCTIPI_DIRECT_CORS_HOSTS", "").split(",") if h.strip()]
DIRECT_CORS_HOSTS = sorted(dict.fromkeys(_DIRECT_CORS_DEFAULT + _extra_cors))

BASE_STREAM_HOSTS = frozenset({
    "cctv-jepara.lifemedia.id", "stream-cctv.jepara.go.id", "stream.kuduskab.go.id",
    "cctv.jombangkab.go.id", "api-cctv.margaharjaya.co.id", "cctvjss.jogjakota.go.id",
    "livepantau.semarangkota.go.id", "streaming.patikab.go.id", "papati.cctvbadilag2.my.id",
    "cctv.rembangkab.go.id", "surakarta.atcsindonesia.info",
})

_SCRAPER_URL_ATTR = {
    "Jepara": "SAMUDRA_CCTV_URL", "Kudus": "KUDUS_CCTV_API_URL", "Demak": "DEMAK_YOUTUBE_URL",
    "Jombang": "JOMBANG_CCTV_URL", "Tol": "TOLL_CCTV_URL", "Jogja": "JOGJA_CCTV_API_URL",
    "Semarang": "SEMARANG_CCTV_URL", "Pati": "PATI_CCTV_URL", "Rembang": "REMBANG_CCTV_API_URL",
}
_SCRAPER_URL_DEFAULTS = {attr: getattr(scrapers, attr) for attr in _SCRAPER_URL_ATTR.values()}

_allowlist_lock = threading.Lock()
_source_hosts: frozenset[str] = frozenset()
_manual_hosts: frozenset[str] = frozenset()
_host_portals: dict[str, tuple[str, str]] = {}
_known_allowed_hosts: set[str] = set(BASE_STREAM_HOSTS)
ALLOW_PRIVATE_CAMERA_HOSTS = os.environ.get("CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS", "").lower() in ("1", "true", "yes")


def validate_stream_host(host: str, *, allow_private: bool | None = None) -> str:
    """Validasi hostname/IP kamera untuk mencegah pelebaran allowlist SSRF."""
    h = (host or "").strip().lower().strip("[]")
    if not h or len(h) > 253 or any(ch in h for ch in ("/", "\\", "@", " ", "\t", "\n", "?")):
        raise ValueError("Hostname kamera tidak valid.")
    if allow_private is None:
        allow_private = ALLOW_PRIVATE_CAMERA_HOSTS or (
            os.environ.get("CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS", "").lower() in ("1", "true", "yes")
        )
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".invalid"):
        raise ValueError(f"Host loopback/lokal '{h}' ditolak demi keamanan SSRF.")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        if re.fullmatch(r"[0-9xXa-fA-F.]+", h) or ":" in h:
            raise ValueError(f"Alamat IP '{h}' tidak valid.")
        if h.endswith((".local", ".lan", ".internal", ".home")):
            if not allow_private or not _LOCAL_HOSTNAME_RE.match(h):
                raise ValueError(f"Hostname jaringan lokal '{h}' ditolak (aktifkan CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS=1 bila diperlukan).")
            return h
        if not _HOSTNAME_RE.match(h):
            raise ValueError(f"Format hostname '{h}' tidak valid.")
        return h

    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise ValueError(f"Alamat IP '{h}' (loopback/link-local/reserved) ditolak demi keamanan SSRF.")
    base_ip = getattr(ip, "ipv4_mapped", None) or ip
    is_cgnat = base_ip.version == 4 and base_ip in net_guard._CGNAT
    if (ip.is_private or is_cgnat) and not allow_private:
        raise ValueError(
            f"Alamat IP privat '{h}' ditolak secara bawaan untuk mencegah SSRF "
            "(set CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS=1 bila kamera berada di LAN tepercaya)."
        )
    return h


def is_safe_stream_host(host: str) -> bool:
    try:
        validate_stream_host(host)
        return True
    except ValueError:
        return False


def validate_camera_url(url: str, *, allow_private: bool | None = None, resolve: bool = False) -> tuple[str, str]:
    clean = (url or "").strip()
    if not clean.startswith(("http://", "https://", "rtsp://")):
        raise ValueError("URL kamera wajib diawali http://, https://, atau rtsp://.")
    parsed = urllib.parse.urlparse(clean)
    host = validate_stream_host(parsed.hostname or "", allow_private=allow_private)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Port URL kamera tidak valid.")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Port URL kamera tidak valid.")
    if resolve:
        check_resolved_host(host, port or 80, allow_private=allow_private)
    return clean, host


def check_resolved_host(host: str, port: int = 80, *, allow_private: bool | None = None) -> None:
    """Tolak host yang me-resolve ke IP terlarang (mis. 127.0.0.1.nip.io).

    Gagal resolve (kamera offline/DNS lokal) tidak ditolak di sini; pemeriksaan
    final tetap dilakukan setiap kali tersambung (net_guard)."""
    if allow_private is None:
        allow_private = ALLOW_PRIVATE_CAMERA_HOSTS or net_guard.private_allowed()
    try:
        net_guard.resolve_safe(host, port, allow_private=allow_private)
    except net_guard.UnsafeAddressError as exc:
        raise ValueError(str(exc)) from exc
    except OSError:
        pass


def _record_allowlist_changes(source_label: str) -> None:
    srv = _srv()
    tuya_svc = getattr(srv, "tuya_service", None) if srv else None
    tuya_hosts = frozenset(h for h in (tuya_svc.cached_stream_hosts() if tuya_svc is not None else ()) if is_safe_stream_host(h))
    current = set(BASE_STREAM_HOSTS | _source_hosts | _manual_hosts | tuya_hosts)
    with _allowlist_lock:
        added = sorted(current - _known_allowed_hosts)
        _known_allowed_hosts.clear()
        _known_allowed_hosts.update(current)
    for h in added:
        log_event("INFO", "allowlist", f"Host baru ditambahkan ke allowlist stream: {h}", host=h, source=source_label)
        audit_obj = getattr(srv, "AUDIT", None) if srv else None
        if audit_obj is not None:
            audit_obj.log("sistem", "allowlist.host", h, f"sumber: {source_label}")


def allowed_stream_hosts() -> frozenset[str]:
    srv = _srv()
    tuya_svc = getattr(srv, "tuya_service", None) if srv else None
    raw_tuya = tuya_svc.cached_stream_hosts() if tuya_svc is not None else frozenset()
    tuya_hosts = frozenset(h for h in raw_tuya if is_safe_stream_host(h))
    if tuya_hosts - _known_allowed_hosts:
        _record_allowlist_changes("tuya")
    return BASE_STREAM_HOSTS | _source_hosts | _manual_hosts | tuya_hosts


def redact_camera(cam: dict) -> dict:
    out = dict(cam)
    if "@" in (out.get("url") or ""):
        out["url"] = redact_url(out["url"])
    if isinstance(out.get("onvif"), dict):
        out["onvif"] = {k: v for k, v in out["onvif"].items() if k not in ("password", "username")}
    return out


def load_sources_config() -> list[dict]:
    if CUSTOM_SOURCES_FILE.exists():
        try:
            saved = json.loads(CUSTOM_SOURCES_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                return [
                    {
                        "folder": (it.get("folder") or "").strip(),
                        "title": (it.get("title") or it.get("folder") or "").strip(),
                        "portalUrl": (it.get("portalUrl") or "").strip(),
                        "apiUrl": (it.get("apiUrl") or "").strip(),
                        "streamHosts": (it.get("streamHosts") or "").strip(),
                        "format": (it.get("format") or "HLS (.m3u8)").strip(),
                    }
                    for it in saved if isinstance(it, dict) and (it.get("folder") or "").strip()
                ]
        except Exception:
            pass
    return [dict(item) for item in DEFAULT_SOURCES]


def apply_sources_config(sources: list[dict]) -> None:
    global _source_hosts, _host_portals
    hosts: set[str] = set()
    portals: dict[str, tuple[str, str]] = {}
    urls = dict(_SCRAPER_URL_DEFAULTS)
    for item in sources:
        folder = item.get("folder")
        api_url = (item.get("apiUrl") or "").strip()
        portal = (item.get("portalUrl") or "").strip()
        portal_pair = None
        if portal.startswith(("http://", "https://")):
            p = urllib.parse.urlparse(portal)
            portal_pair = (f"{p.scheme}://{p.netloc}", portal)
        attr = _SCRAPER_URL_ATTR.get(folder or "")
        if attr and api_url.startswith(("http://", "https://")):
            urls[attr] = api_url
        for raw_host in (item.get("streamHosts") or "").split(","):
            host = raw_host.strip().split(":")[0].lower()
            if host and host != "www.youtube.com" and is_safe_stream_host(host):
                hosts.add(host)
                if portal_pair is not None:
                    portals[host] = portal_pair
    for attr, value in urls.items():
        setattr(scrapers, attr, value)
    _source_hosts = frozenset(hosts)
    _host_portals = portals
    _record_allowlist_changes("sources")


def load_manual_cameras() -> list[dict]:
    global _manual_hosts
    if not MANUAL_CAMERAS_FILE.exists():
        _manual_hosts = frozenset()
        return []
    try:
        items = json.loads(MANUAL_CAMERAS_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            _manual_hosts = frozenset()
            return []
        valid: list[dict] = []
        hosts: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            name = (item.get("name") or "").strip()
            if not url.startswith(("http://", "https://", "rtsp://")) or not name:
                continue
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if host and url.startswith(("http://", "https://")) and is_safe_stream_host(host):
                hosts.add(host)
            entry = {
                "manualId": item.get("manualId") or f"m-{len(valid) + 1}",
                "name": name, "url": url,
                "type": item.get("type") or ("rtsp" if url.startswith("rtsp://") else "hls"),
                "provider": "manual",
                "folder": (item.get("folder") or "Lokal").strip() or "Lokal",
            }
            if isinstance(item.get("onvif"), dict):
                entry["onvif"] = item["onvif"]
            if item.get("lat") is not None and item.get("lng") is not None:
                entry["lat"], entry["lng"] = item["lat"], item["lng"]
            valid.append(entry)
        _manual_hosts = frozenset(hosts)
        _record_allowlist_changes("manual_camera")
        return valid
    except Exception:
        return []


def save_manual_cameras(items: list[dict]) -> None:
    write_private(MANUAL_CAMERAS_FILE, json.dumps(items, indent=2, ensure_ascii=False))
    load_manual_cameras()


def load_fallback_cameras() -> list[dict]:
    if FALLBACK_CAMERAS_FILE.exists():
        with FALLBACK_CAMERAS_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    return []


def load_region_cache() -> dict[str, list[dict]]:
    try:
        data = json.loads(CAMERAS_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_region_cache(live: dict[str, list[dict]]) -> None:
    if not live:
        return
    cache = load_region_cache()
    for folder, cams in live.items():
        cache[folder] = [{k: v for k, v in c.items() if k != "id"} for c in cams]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CAMERAS_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CAMERAS_CACHE_FILE)
    except OSError as exc:
        log_event("WARNING", "cameras", "Gagal menyimpan cache wilayah", error=str(exc))


_cache_lock = threading.Lock()
_cameras_cache: dict = {"cameras": [], "updated_at": 0.0, "source": "uninitialized", "apiStatus": {}}
_bg_refreshing = False


def _refresh_cameras_sync() -> dict:
    srv = _srv()
    now = time.time()
    fallback = (srv.load_fallback_cameras() if srv else load_fallback_cameras())
    region_cache = (srv.load_region_cache() if srv else load_region_cache())
    sources = (srv.load_sources_config() if srv else load_sources_config())
    fetchers = (srv.BUILTIN_FETCHERS if srv else scrapers.BUILTIN_FETCHERS)

    def fallback_for(folder: str) -> list[dict]:
        cams = region_cache.get(folder) or [c for c in fallback if c.get("folder") == folder]
        return mark_token_expiry(cams, now)

    def run_fetch(src: dict):
        folder = src["folder"]
        fn = fetchers.get(folder)
        try:
            res = fn() if fn else scrapers.fetch_custom_region_cameras(src)
            if res:
                return folder, res, True, ""
            fb = fallback_for(folder)
            log_event("WARNING", "scraper", f"Sumber '{folder}' kosong (struktur HTML/API mungkin berubah); memakai fallback ({len(fb)} kamera).", region=folder)
            return folder, fb, False, "Empty response (struktur berubah / kosong)"
        except Exception as exc:
            fb = fallback_for(folder)
            log_event("WARNING", "scraper", f"Gagal mengambil sumber '{folder}': {exc}; memakai fallback ({len(fb)} kamera).", region=folder)
            return folder, fb, False, str(exc)

    results = []
    if sources:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(12, len(sources)))) as pool:
            results = [f.result() for f in [pool.submit(run_fetch, src) for src in sources]]

    (srv.save_region_cache if srv else save_region_cache)({f: c for f, c, ok, _ in results if ok})
    combined: list[dict] = []
    live_sources = 0
    api_status: dict[str, dict] = {}
    for folder, cams, is_live, err_msg in results:
        api_status[folder] = {"online": is_live, "error": err_msg[:120] if err_msg else "", "checkedAt": int(now)}
        if is_live:
            live_sources += 1
        for cam in cams:
            combined.append({**cam, "id": len(combined) + 1})
    for cam in (srv.load_manual_cameras() if srv else load_manual_cameras()):
        combined.append({**cam, "id": len(combined) + 1})
    tuya_svc = getattr(srv, "tuya_service", None) if srv else None
    if tuya_svc is not None:
        for cam in tuya_svc.get_cameras():
            combined.append({**cam, "id": len(combined) + 1})

    cache_lock = srv._cache_lock if srv else _cache_lock
    cache_obj = srv._cameras_cache if srv else _cameras_cache
    if combined or not sources:
        source_label = "live" if sources and live_sources == len(sources) else ("partial" if live_sources > 0 else "fallback")
        with cache_lock:
            cache_obj.update(cameras=combined, updated_at=now, source=source_label, apiStatus=api_status)
        return {"source": source_label, "updated_at": int(now), "count": len(combined), "cameras": combined, "apiStatus": api_status, "directCorsHosts": DIRECT_CORS_HOSTS}
    return {"source": "empty", "updated_at": int(now), "count": 0, "cameras": [], "apiStatus": api_status, "directCorsHosts": DIRECT_CORS_HOSTS}


_bg_refresh_lock = threading.Lock()


def _trigger_bg_refresh() -> None:
    global _bg_refreshing
    with _bg_refresh_lock:
        if _bg_refreshing:
            return
        _bg_refreshing = True

    def _worker():
        global _bg_refreshing
        try:
            _refresh_cameras_sync()
        finally:
            with _bg_refresh_lock:
                _bg_refreshing = False

    threading.Thread(target=_worker, daemon=True).start()


def get_cameras(force_refresh: bool = False) -> dict:
    srv = _srv()
    if force_refresh:
        return (srv._refresh_cameras_sync() if srv else _refresh_cameras_sync())
    now = time.time()
    cache_lock = srv._cache_lock if srv else _cache_lock
    cache_obj = srv._cameras_cache if srv else _cameras_cache
    with cache_lock:
        if not cache_obj["cameras"]:
            region_cache = (srv.load_region_cache() if srv else load_region_cache())
            fallback, seen_folders = [], set()
            for cam in (srv.load_fallback_cameras() if srv else load_fallback_cameras()):
                folder = cam.get("folder")
                if folder in region_cache:
                    if folder not in seen_folders:
                        fallback.extend(region_cache[folder])
                        seen_folders.add(folder)
                else:
                    fallback.append(cam)
            manual = (srv.load_manual_cameras() if srv else load_manual_cameras())
            tuya_svc = getattr(srv, "tuya_service", None) if srv else None
            tuya_cams = tuya_svc.get_cameras() if tuya_svc is not None else []
            combined_fb = [{**cam, "id": idx + 1} for idx, cam in enumerate(fallback + manual + tuya_cams)]
            if combined_fb:
                cache_obj.update(cameras=combined_fb, updated_at=0.0, source="fallback")
        cams = cache_obj["cameras"]
        updated_at = cache_obj["updated_at"]
        source = cache_obj["source"]
        api_status = cache_obj.get("apiStatus", {})

    if not cams:
        return (srv._refresh_cameras_sync() if srv else _refresh_cameras_sync())
    if now - updated_at >= CACHE_TTL_SECONDS:
        (srv._trigger_bg_refresh() if srv else _trigger_bg_refresh())
    cams = mark_token_expiry(cams, now)
    return {"source": source, "updated_at": int(updated_at or now), "count": len(cams), "cameras": cams, "apiStatus": api_status, "directCorsHosts": DIRECT_CORS_HOSTS}


def probe_camera(cam: dict) -> str:
    url = cam.get("url", "") or ""
    ctype = cam.get("type", "hls")
    try:
        if url.startswith(("rtsp://", "rtsps://")):
            parsed = urllib.parse.urlparse(url)
            port = parsed.port or 554
            net_guard.resolve_safe(parsed.hostname or "", port)
            with socket.create_connection((parsed.hostname, port), timeout=4):
                return "online"
        if ctype == "youtube":
            vid = cam.get("videoId", "")
            req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}", headers={"User-Agent": USER_AGENT})
            with net_guard.safe_urlopen(req, timeout=6, context=_ssl_ctx) as r:
                ps = re.search(r'"playabilityStatus":\{"status":"([^"]+)"', r.read().decode("utf-8", errors="ignore"))
                return "online" if (ps and ps.group(1) == "OK") else "offline"
        host_lower = (urllib.parse.urlparse(url).hostname or "").lower()
        headers = {"User-Agent": USER_AGENT}
        if host_lower == "stream.kuduskab.go.id":
            headers["Origin"] = "https://streaming.kuduskab.go.id"
        elif host_lower == "cctv.jombangkab.go.id":
            headers["Referer"] = "https://cctv.jombangkab.go.id/v2/"
        else:
            headers["Origin"] = "https://samudra.jepara.go.id"
        req = urllib.request.Request(url, headers=headers)
        with net_guard.safe_urlopen(req, timeout=5, context=_ssl_ctx) as r:
            if r.status >= 400:
                return "offline"
            if ctype == "http":
                return "online"
            if ctype in ("mjpeg", "flv"):
                return "online" if len(r.read(128)) > 0 else "offline"
            return "online" if "#EXTM3U" in r.read(512).decode("utf-8", errors="ignore") else "offline"
    except Exception:
        return "offline"

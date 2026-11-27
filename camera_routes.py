#!/usr/bin/env python3
"""Handler HTTP domain kamera, sumber stream, cek status & backup."""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import urllib.parse
from http import HTTPStatus

import camera_store as _store
from camera_store import (
    BASE_STREAM_HOSTS, CACHE_TTL_SECONDS, CAMERAS_CACHE_FILE, CUSTOM_SOURCES_FILE,
    DEFAULT_SOURCES, DIRECT_CORS_HOSTS, FALLBACK_CAMERAS_FILE, MANUAL_CAMERAS_FILE,
    _cache_lock, _cameras_cache, _host_portals, _refresh_cameras_sync,
    _SCRAPER_URL_DEFAULTS, _trigger_bg_refresh, allowed_stream_hosts,
    apply_sources_config, get_cameras, is_safe_stream_host, load_fallback_cameras,
    load_manual_cameras, load_region_cache, load_sources_config, probe_camera,
    redact_camera, save_manual_cameras, save_region_cache, validate_camera_url,
    validate_stream_host,
)
from storage_utils import write_private

__all__ = [
    "BASE_STREAM_HOSTS", "CACHE_TTL_SECONDS", "CAMERAS_CACHE_FILE", "CUSTOM_SOURCES_FILE",
    "DEFAULT_SOURCES", "DIRECT_CORS_HOSTS", "FALLBACK_CAMERAS_FILE", "MANUAL_CAMERAS_FILE",
    "_cache_lock", "_cameras_cache", "_host_portals", "_refresh_cameras_sync",
    "_SCRAPER_URL_DEFAULTS", "_trigger_bg_refresh", "allowed_stream_hosts",
    "apply_sources_config", "get_cameras", "is_safe_stream_host", "load_fallback_cameras",
    "load_manual_cameras", "load_region_cache", "load_sources_config", "probe_camera",
    "redact_camera", "save_manual_cameras", "save_region_cache", "validate_camera_url",
    "validate_stream_host",
]


def _srv():
    return sys.modules.get("server")


def handle_cameras(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    force = params.get("refresh", ["0"])[0] in ("1", "true", "yes")
    if force:
        # Refresh paksa menjalankan seluruh scraper secara sinkron: batasi agar
        # tidak bisa dipakai untuk membebani server / sumber publik.
        user = ((handler.ctx or {}).get("username") or "-").lower()
        wait = srv.ACTION_LIMITER.check_and_record(
            [f"cam_refresh:ip:{handler.client_ip()}", f"cam_refresh:u:{user}", "cam_refresh:global"], 4, 60,
        )
        force = wait == 0
    data = srv.get_cameras(force_refresh=force)
    regions = handler._regions()
    perms = (handler.ctx or {}).get("permissions", set())
    can_ptz = "ptz.control" in perms
    can_manage = "cameras.manage" in perms
    if regions is not None or not can_ptz or not can_manage:
        data = dict(data)
        cams = [c for c in data.get("cameras", []) if regions is None or (c.get("folder") or "") in regions]
        if not can_ptz:
            cams = [{k: v for k, v in c.items() if k != "onvif"} if "onvif" in c else c for c in cams]
        if not can_manage:
            cams = [redact_camera(c) for c in cams]
        data["cameras"] = cams
        data["count"] = len(cams)
        if regions is not None and isinstance(data.get("apiStatus"), dict):
            data["apiStatus"] = {k: v for k, v in data["apiStatus"].items() if k in regions}
        data["regionsLimited"] = regions is not None
    handler._send_json(data)


def handle_get_sources(handler) -> None:
    srv = _srv()
    handler._send_json({"sources": srv.load_sources_config()})


def _validate_sources_list(incoming: list) -> None:
    for item in incoming:
        if not isinstance(item, dict):
            continue
        for field in ("apiUrl", "portalUrl"):
            u = (item.get(field) or "").strip()
            if u.startswith(("http://", "https://")):
                h = (urllib.parse.urlparse(u).hostname or "").lower()
                if h and h != "www.youtube.com":
                    validate_stream_host(h)
        for raw_host in (item.get("streamHosts") or "").split(","):
            h = raw_host.strip().split(":")[0].lower()
            if h and h != "www.youtube.com":
                validate_stream_host(h)


def handle_save_sources(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        incoming = body.get("sources")
        if not isinstance(incoming, list):
            handler.send_json_error(HTTPStatus.BAD_REQUEST, "Invalid sources payload")
            return
        _validate_sources_list(incoming)
        write_private(_store.CUSTOM_SOURCES_FILE, json.dumps(incoming, indent=2, ensure_ascii=False))
        sources = srv.load_sources_config()
        srv.apply_sources_config(sources)
        cams = srv.get_cameras(force_refresh=True)
        handler._send_json({"sources": sources, "cameras": cams})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Failed to save sources: {exc}")


def handle_reset_sources(handler) -> None:
    srv = _srv()
    try:
        _store.CUSTOM_SOURCES_FILE.unlink(missing_ok=True)
        sources = srv.load_sources_config()
        srv.apply_sources_config(sources)
        cams = srv.get_cameras(force_refresh=True)
    except Exception as exc:
        handler.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Gagal mereset sumber: {exc}")
        return
    handler._send_json({"sources": sources, "cameras": cams})


def handle_check_status(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        ids = set(body.get("ids") or [])
        folder = body.get("folder")
        all_cams = srv.get_cameras().get("cameras", [])
        targets = [
            c for c in all_cams
            if ((ids and c["id"] in ids) or (folder and c.get("folder") == folder)) and handler._cam_ok(c)
        ]
        statuses: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for cam, st in zip(targets, pool.map(srv.probe_camera, targets)):
                statuses[str(cam["id"])] = st
        handler._send_json({"statuses": statuses})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Status check failed: {exc}")


_ONVIF_FIELDS = ("deviceUrl", "username", "password", "profileToken", "mediaUrl", "snapshotUri", "profileName")


def _clean_onvif(value) -> dict | None:
    """Validasi blok ONVIF kamera manual: hanya field dikenal, string pendek, deviceUrl aman."""
    if not isinstance(value, dict):
        return None
    out = {k: str(value.get(k) or "").strip()[:500] for k in _ONVIF_FIELDS if value.get(k) not in (None, "")}
    dev_url = out.get("deviceUrl", "")
    if dev_url:
        full = dev_url if dev_url.startswith(("http://", "https://")) else f"http://{dev_url}"
        validate_camera_url(full, resolve=True)
    return out or None


def handle_manual_cameras_get(handler) -> None:
    srv = _srv()
    can_manage = "cameras.manage" in handler.ctx["permissions"]
    items = [c if can_manage else redact_camera(c) for c in srv.load_manual_cameras() if handler._cam_ok(c)]
    handler._send_json({"cameras": items})


def handle_add_manual_camera(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        name = (body.get("name") or "").strip()
        url = (body.get("url") or "").strip()
        ctype = (body.get("type") or "hls").strip().lower()
        folder = (body.get("folder") or "Lokal").strip() or "Lokal"
        if handler._regions() is not None and folder not in handler._regions():
            handler.send_json_error(HTTPStatus.FORBIDDEN, f"Akun Anda tidak boleh menambah kamera di wilayah “{folder}”.")
            return
        if not name or not url.startswith(("http://", "https://", "rtsp://")):
            handler.send_json_error(HTTPStatus.BAD_REQUEST, "Nama dan URL (http://, https://, atau rtsp://) wajib diisi.")
            return
        url, _host = validate_camera_url(url, resolve=True)
        onvif = body.get("onvif")
        if onvif is not None:
            body["onvif"] = _clean_onvif(onvif)
        if ctype not in ("hls", "mjpeg", "http", "rtsp"):
            ctype = "rtsp" if url.startswith("rtsp://") else "hls"

        items = srv.load_manual_cameras()
        manual_id = f"m-{int(time.time() * 1000)}"
        handler._audit_target = f"{name} ({folder})"
        new_item = {"manualId": manual_id, "name": name, "url": url, "type": ctype, "provider": "manual", "folder": folder}
        if isinstance(body.get("onvif"), dict):
            new_item["onvif"] = body["onvif"]
        if body.get("lat") is not None and body.get("lng") is not None:
            new_item["lat"], new_item["lng"] = body["lat"], body["lng"]
        items.append(new_item)
        srv.save_manual_cameras(items)
        with srv._cache_lock:
            srv._cameras_cache["cameras"] = []
        handler._send_json({"manualId": manual_id, "cameras": srv.get_cameras()})
    except ValueError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Gagal menambah kamera: {exc}")


def handle_delete_manual_camera(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    manual_id = params.get("manualId", [""])[0].strip()
    target = next((c for c in srv.load_manual_cameras() if c.get("manualId") == manual_id), None)
    if target is not None and not handler._cam_ok(target):
        handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
        return
    handler._audit_target = (target or {}).get("name") or manual_id
    if srv.live_ai_service is not None and manual_id:
        try:
            srv.live_ai_service.stop(f"m:{manual_id}", reason="Kamera dihapus.")
        except Exception:
            pass
    items = [c for c in srv.load_manual_cameras() if c.get("manualId") != manual_id]
    srv.save_manual_cameras(items)
    with srv._cache_lock:
        srv._cameras_cache["cameras"] = []
    handler._send_json({"cameras": srv.get_cameras()})


def handle_backup_import(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        if isinstance(body.get("sources"), list):
            _validate_sources_list(body["sources"])
        if isinstance(body.get("manualCameras"), list):
            for mc in body["manualCameras"]:
                if not isinstance(mc, dict):
                    continue
                if mc.get("url"):
                    validate_camera_url(str(mc["url"]), resolve=True)
                if mc.get("onvif") is not None:
                    mc["onvif"] = _clean_onvif(mc["onvif"])
        if isinstance(body.get("sources"), list):
            write_private(_store.CUSTOM_SOURCES_FILE, json.dumps(body["sources"], indent=2, ensure_ascii=False))
            srv.apply_sources_config(srv.load_sources_config())
        if isinstance(body.get("manualCameras"), list):
            srv.save_manual_cameras(body["manualCameras"])
        handler._send_json({"ok": True, "cameras": srv.get_cameras(force_refresh=True)})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Import backup gagal: {exc}")

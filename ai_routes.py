#!/usr/bin/env python3
"""Rute domain AI Jepretan, Zona Deteksi, Live AI realtime, dan Alarm Monitor."""

from __future__ import annotations

import sys
import time
import urllib.parse
from http import HTTPStatus

from auth_routes import settings_diff
from system_health import CapacityError


def _srv():
    return sys.modules.get("server")


def _cam_by_key(handler, key: str) -> dict | None:
    srv = _srv()
    norm = srv._normalize_key(key or "")
    return next((c for c in srv.get_cameras().get("cameras", []) if srv._camera_key(c) == norm), None)


def handle_jepretan_list(handler, query: str) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._ai_import_error)
        return
    params = urllib.parse.parse_qs(query)
    try:
        limit = int(params.get("limit", ["200"])[0])
    except ValueError:
        limit = 200
    items = srv.ai_service.list_jepretan(
        date=params.get("date", [""])[0],
        camera=params.get("camera", [""])[0],
        category=params.get("category", [""])[0],
        limit=limit,
        allowed_keys=handler._allowed_keys(),
    )
    handler._send_json({"jepretan": items, "count": len(items)})


def handle_jepretan_image(handler, path: str, query: str = "") -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    params = urllib.parse.parse_qs(query or "")
    want_enhanced = params.get("enhanced", ["0"])[0] in ("1", "true", "yes")
    img_path = None
    if (
        srv.ai_service is not None
        and len(parts) == 4
        and parts[3] == "image"
        and handler._key_ok(srv.ai_service.jepretan_camera_key(parts[2]))
    ):
        img_path = (
            srv.ai_service.enhanced_image_path(parts[2])
            if want_enhanced
            else srv.ai_service.get_image_path(parts[2])
        )
    if img_path is None:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Jepretan tidak ditemukan")
        return
    try:
        data = img_path.read_bytes()
    except Exception:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Berkas jepretan hilang")
        return
    handler.send_response(HTTPStatus.OK)
    handler.send_cors_headers()
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def handle_jepretan_delete(handler, path: str) -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    ok = False
    if srv.ai_service is not None and len(parts) == 3 and handler._key_ok(srv.ai_service.jepretan_camera_key(parts[2])):
        row = srv.ai_service.get_jepretan(parts[2]) or {}
        handler._audit_target = (
            f"{row.get('camera_name', parts[2])} · "
            f"{time.strftime('%d/%m %H:%M', time.localtime(row.get('ts') or 0))}"
        )
        handler._audit_detail = f"{row.get('class_name', '')} {round(float(row.get('confidence') or 0) * 100)}%"
        ok = srv.ai_service.delete_jepretan(parts[2])
    if not ok:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Jepretan tidak ditemukan")
        return
    handler._send_json({"ok": True})


def handle_ai_status(handler) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler._send_json({
            "available": False,
            "unavailable_reason": srv._ai_import_error,
            "running": False,
            "enabled": False,
            "monitored_cameras": 0,
            "today_count": 0,
            "telegram_configured": False,
        })
        return
    st = srv.ai_service.status()
    allowed = handler._allowed_keys()
    if allowed is not None:
        st["today_count"] = len(srv.ai_service.list_jepretan(
            date=time.strftime("%Y-%m-%d"), limit=1000, allowed_keys=allowed,
        ))
    handler._send_json(st)


def handle_ai_settings_get(handler) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._ai_import_error)
        return
    settings = srv.ai_service.public_settings()
    if "settings.ai" not in handler.ctx["permissions"]:
        settings["telegram_chat_id"] = ""
    allowed = handler._allowed_keys()
    if allowed is not None:
        ok = set(allowed)
        settings["cameras"] = [k for k in settings.get("cameras", []) if k in ok]
        settings["zones"] = {k: v for k, v in (settings.get("zones") or {}).items() if k in ok}
    handler._send_json({"settings": settings})


def handle_ai_settings_post(handler) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._ai_import_error)
        return
    try:
        body = handler.read_json_body()
        payload = body.get("settings", body)
        before = srv.ai_service.public_settings()
        updated = srv.ai_service.update_settings(payload)
        handler._audit_detail = settings_diff(before, updated)
        if payload.get("telegram_bot_token"):
            handler._audit_detail += "; token Telegram diganti"
        handler._send_json({"settings": updated})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Pengaturan AI tidak valid: {exc}")


def handle_zones_get(handler) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._ai_import_error)
        return
    zones = srv.ai_service.public_settings().get("zones") or {}
    handler._send_json({"zones": {k: v for k, v in zones.items() if handler._key_ok(k)}})


def handle_zone_post(handler, path: str) -> None:
    srv = _srv()
    if srv.ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._ai_import_error)
        return
    key = srv._normalize_key(urllib.parse.unquote(path[len("/api/ai/zones/"):]))
    cam = _cam_by_key(handler, key)
    if not cam or not handler._cam_ok(cam):
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Kamera tidak ditemukan.")
        return
    try:
        zone = srv.ai_service.set_zone(key, handler.read_json_body(limit=200_000))
    except ValueError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
        return
    handler._audit_target = cam.get("name") or key
    if zone is None:
        handler._audit_detail = "zona & jadwal dihapus"
    else:
        sch = zone["schedule"]
        handler._audit_detail = (
            f"{len(zone['polygons'])} zona · "
            + (f"jadwal {sch['start']}–{sch['end']}" if sch["enabled"] else "aktif 24 jam")
        )
    handler._send_json({"ok": True, "zone": zone})


def handle_ai_snapshot(handler, query: str) -> None:
    srv = _srv()
    key = urllib.parse.parse_qs(query).get("key", [""])[0]
    cam = _cam_by_key(handler, key)
    ai_mod = getattr(srv, "_ai_module", None)
    if not cam or not handler._cam_ok(cam) or ai_mod is None:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Kamera tidak ditemukan.")
        return
    data = ai_mod.grab_frame(cam)
    if not data:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, "Gagal mengambil gambar dari kamera.")
        return
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def handle_live_ai_status(handler, query: str = "") -> None:
    srv = _srv()
    if srv.live_ai_service is None:
        handler._send_json({
            "available": False,
            "unavailable_reason": srv._live_ai_import_error,
            "active": False,
            "max_cameras": 1,
        })
        return
    params = urllib.parse.parse_qs(query or "")
    key = params.get("camera", [""])[0] or None
    st = srv.live_ai_service.status(key)
    if st.get("camera_key") and not handler._key_ok(st["camera_key"]):
        st = {k: v for k, v in st.items() if k not in ("camera_key", "camera_name", "detection_count", "detections")}
        st["camera_name"] = "kamera wilayah lain"
    handler._send_json(st)


def handle_live_ai_stream(handler, path: str) -> None:
    srv = _srv()
    raw = path[len("/api/live-ai/"):-len(".mjpg")]
    key = urllib.parse.unquote(raw)
    if not handler._key_ok(key):
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Live AI tidak aktif untuk kamera ini")
        return
    if srv.live_ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._live_ai_import_error)
        return
    try:
        with srv.LOAD_GUARD.viewer_slot(f"liveai:{key}"):
            frames = srv.live_ai_service.mjpeg_frames(key)
            probe = next(frames, None)
            if probe is None:
                handler.send_json_error(HTTPStatus.NOT_FOUND, "Live AI tidak aktif untuk kamera ini")
                return
            handler.send_response(HTTPStatus.OK)
            handler.send_cors_headers()
            handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            handler.send_header("Pragma", "no-cache")
            handler.end_headers()
            try:
                handler.wfile.write(probe)
                for chunk in frames:
                    handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass
            finally:
                frames.close()
    except CapacityError as exc:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))


def handle_live_ai_control(handler, path: str) -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[3] not in ("start", "stop"):
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Not found")
        return
    key = urllib.parse.unquote(parts[2])
    if not handler._key_ok(key):
        handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
        return
    handler._audit_target = next(
        (c.get("name") for c in srv.get_cameras().get("cameras", []) if srv._camera_key(c) == srv._normalize_key(key)),
        key,
    )
    if srv.live_ai_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._live_ai_import_error)
        return
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length:
            handler.rfile.read(length)
    except Exception:
        pass

    if parts[3] == "start":
        try:
            ok, message = srv.live_ai_service.start(key)
        except CapacityError as exc:
            handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        if not ok:
            status_code = HTTPStatus.SERVICE_UNAVAILABLE if "batas proses ffmpeg" in message.lower() else HTTPStatus.BAD_REQUEST
            handler.send_json_error(status_code, message)
            return
        handler._send_json({"ok": True, "message": message, "status": srv.live_ai_service.status(key)})
    else:
        stopped = srv.live_ai_service.stop(key)
        handler._send_json({"ok": True, "stopped": stopped, "status": srv.live_ai_service.status(key)})


def handle_monitor_status(handler) -> None:
    srv = _srv()
    allowed = handler._allowed_keys()
    handler._send_json(srv.monitor_service.status(set(allowed) if allowed is not None else None))


def handle_monitor_settings_get(handler) -> None:
    srv = _srv()
    ms = srv.monitor_service.settings()
    allowed = handler._allowed_keys()
    if allowed is not None:
        ms["cameras"] = [k for k in ms["cameras"] if k in set(allowed)]
    handler._send_json({"settings": ms})


def handle_monitor_settings_post(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        before = srv.monitor_service.settings()
        updated = srv.monitor_service.update_settings(body.get("settings", body))
        handler._audit_detail = settings_diff(before, updated)
        handler._send_json({"settings": updated})
    except (ValueError, TypeError) as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Pengaturan alarm tidak valid: {exc}")

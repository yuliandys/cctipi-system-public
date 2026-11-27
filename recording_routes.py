#!/usr/bin/env python3
"""Rute & fungsi murni domain Rekaman Server (termasuk HTTP Range 206/416)."""

from __future__ import annotations

import sys
import time
import urllib.parse
from http import HTTPStatus

from auth_routes import settings_diff


def _srv():
    return sys.modules.get("server")


def parse_byte_range(range_header: str, total: int) -> tuple[int, int, bool] | None:
    """Parse header HTTP Range: 'bytes=...'.

    Mengembalikan (start, end, is_partial) bila valid, atau None bila rentang
    tidak dapat dipenuhi (416 Requested Range Not Satisfiable).
    """
    if total <= 0:
        return (0, -1, False) if not (range_header or "").startswith("bytes=") else None
    if not (range_header or "").startswith("bytes="):
        return 0, total - 1, False
    spec = range_header[6:].split(",")[0].strip()
    try:
        if spec.startswith("-"):
            suffix_len = int(spec[1:])
            if suffix_len <= 0:
                return None
            start = max(0, total - suffix_len)
            end = total - 1
        else:
            first, _, last = spec.partition("-")
            start = int(first) if first else 0
            end = int(last) if last else total - 1
        end = min(end, total - 1)
        if start < 0 or start > end or start >= total:
            return None
        return start, end, True
    except ValueError:
        return 0, total - 1, False


def handle_rekaman_list(handler, query: str) -> None:
    srv = _srv()
    if srv.recorder_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._recorder_import_error)
        return
    params = urllib.parse.parse_qs(query)
    try:
        limit = int(params.get("limit", ["200"])[0])
    except ValueError:
        limit = 200
    items = srv.recorder_service.list_recordings(
        date=params.get("date", [""])[0],
        camera=params.get("camera", [""])[0],
        mode=params.get("mode", [""])[0],
        limit=limit,
        allowed_keys=handler._allowed_keys(),
    )
    handler._send_json({"rekaman": items, "count": len(items)})


def handle_rekaman_find(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    key = srv._normalize_key(params.get("camera", [""])[0])
    try:
        ts = float(params.get("ts", ["0"])[0])
    except ValueError:
        ts = 0
    if srv.recorder_service is None or not key or not ts or not handler._key_ok(key):
        handler._send_json({"found": False, "message": "Rekaman server tidak tersedia."})
        return
    handler._send_json(srv.recorder_service.find_at(key, ts))


def handle_rekaman_file(handler, path: str, query: str = "") -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    file_path = None
    if srv.recorder_service is not None and len(parts) == 4 and parts[3] == "file":
        rec = srv.recorder_service.get_recording(parts[2])
        if rec and handler._key_ok(rec.get("camera_id")):
            file_path = srv.recorder_service.get_file_path(parts[2])
    if file_path is None:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Rekaman tidak ditemukan")
        return

    params = urllib.parse.parse_qs(query or "")
    as_download = params.get("download", ["0"])[0] in ("1", "true", "yes")
    try:
        total = file_path.stat().st_size
    except Exception:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Berkas rekaman hilang")
        return

    parsed_range = parse_byte_range(handler.headers.get("Range", ""), total)
    if parsed_range is None:
        handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        handler.send_cors_headers()
        handler.send_header("Content-Range", f"bytes */{total}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return

    start, end, partial = parsed_range
    length = max(0, end - start + 1)
    handler.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
    handler.send_cors_headers()
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total}")
    if as_download:
        handler.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.send_header("Content-Length", str(length))
    handler.end_headers()
    try:
        with open(file_path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


def handle_rekaman_delete(handler, path: str) -> None:
    srv = _srv()
    parts = path.strip("/").split("/")
    ok = False
    if srv.recorder_service is not None and len(parts) == 3:
        rec = srv.recorder_service.get_recording(parts[2])
        if rec and handler._key_ok(rec.get("camera_id")):
            handler._audit_target = (
                f"{rec.get('camera_name')} · "
                f"{time.strftime('%d/%m %H:%M', time.localtime(rec.get('started_at') or 0))}"
            )
            handler._audit_detail = f"{rec.get('mode')} · {round((rec.get('size_bytes') or 0) / 1048576, 1)} MB"
            ok = srv.recorder_service.delete_recording(parts[2])
    if not ok:
        handler.send_json_error(HTTPStatus.NOT_FOUND, "Rekaman tidak ditemukan")
        return
    handler._send_json({"ok": True})


def handle_rekaman_status(handler) -> None:
    srv = _srv()
    if srv.recorder_service is None:
        handler._send_json({
            "available": False,
            "unavailable_reason": srv._recorder_import_error,
            "enabled": False,
            "running": False,
            "recording": [],
            "recordings_count": 0,
            "total_size_bytes": 0,
        })
        return
    st = srv.recorder_service.status()
    allowed = handler._allowed_keys()
    if allowed is not None:
        ok = set(allowed)
        st["recording"] = [r for r in st.get("recording", []) if r.get("camera_id") in ok]
        mine = srv.recorder_service.list_recordings(limit=1000, allowed_keys=allowed)
        st["recordings_count"] = len(mine)
        st["total_size_bytes"] = sum(int(r.get("size_bytes") or 0) for r in mine)
    handler._send_json(st)


def handle_rekaman_settings_get(handler) -> None:
    srv = _srv()
    if srv.recorder_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._recorder_import_error)
        return
    handler._send_json({"settings": srv.recorder_service.public_settings()})


def handle_rekaman_settings_post(handler) -> None:
    srv = _srv()
    if srv.recorder_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._recorder_import_error)
        return
    try:
        body = handler.read_json_body()
        payload = body.get("settings", body)
        before = dict(srv.recorder_service.public_settings()) if hasattr(srv.recorder_service, "public_settings") else {}
        updated = srv.recorder_service.update_settings(payload)
        handler._audit_detail = settings_diff(before, updated) if before else ""
        handler._send_json({"settings": updated})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Pengaturan rekaman tidak valid: {exc}")

#!/usr/bin/env python3
"""Rute & logika domain ONVIF (penemuan profil & PTZ SOAP) serta Tuya IoT Cloud."""

from __future__ import annotations

import base64
import hashlib
import html
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http import HTTPStatus

import net_guard
from auth_routes import settings_diff
from camera_routes import validate_stream_host
from camera_store import check_resolved_host
from proxy_routes import rewrite_m3u8
from scrapers import USER_AGENT, _ssl_ctx


def _srv():
    return sys.modules.get("server")


def _wsse_header(username: str, password: str) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f'<UsernameToken><Username>{html.escape(username)}</Username>'
        f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>'
        f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security></s:Header>"
    )


def _onvif_soap_call(endpoint: str, inner_xml: str, username: str = "", password: str = "") -> ET.Element:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"{_wsse_header(username, password)}<s:Body>{inner_xml}</s:Body></s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    with net_guard.safe_urlopen(req, timeout=8, context=_ssl_ctx) as r:
        return ET.fromstring(r.read(2_000_000))


def build_onvif_ptz_xml(action: str, token: str) -> str:
    """Bangun body SOAP ONVIF ContinuousMove atau Stop (fungsi murni)."""
    act = (action or "stop").strip().lower()
    vectors = {
        "left": (-0.5, 0.0, 0.0),
        "right": (0.5, 0.0, 0.0),
        "up": (0.0, 0.5, 0.0),
        "down": (0.0, -0.5, 0.0),
        "zoom_in": (0.0, 0.0, 0.5),
        "zoom_out": (0.0, 0.0, -0.5),
    }
    if act == "stop":
        return (
            '<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            f"<ProfileToken>{html.escape(token)}</ProfileToken>"
            "<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>"
        )
    vx, vy, vz = vectors.get(act, (0.0, 0.0, 0.0))
    return (
        '<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
        f"<ProfileToken>{html.escape(token)}</ProfileToken>"
        "<Velocity>"
        f'<PanTilt x="{vx}" y="{vy}" xmlns="http://www.onvif.org/ver10/schema"/>'
        f'<Zoom x="{vz}" xmlns="http://www.onvif.org/ver10/schema"/>'
        "</Velocity></ContinuousMove>"
    )


def discover_onvif_profiles(device_url: str, username: str = "", password: str = "") -> dict:
    """Tanya layanan Media ONVIF perangkat untuk daftar profil & URI stream/snapshot."""
    url = (device_url or "").strip()
    if not url:
        raise ValueError("URL / IP perangkat ONVIF wajib diisi.")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("URL ONVIF tidak valid.")
    validate_stream_host(parsed.hostname)
    check_resolved_host(parsed.hostname, parsed.port or 80)
    if not parsed.path or parsed.path == "/":
        url = f"{parsed.scheme}://{parsed.netloc}/onvif/device_service"

    handlers: list = []
    if username:
        pw_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pw_mgr.add_password(None, f"{parsed.scheme}://{parsed.netloc}", username, password)
        handlers.extend([
            urllib.request.HTTPDigestAuthHandler(pw_mgr),
            urllib.request.HTTPBasicAuthHandler(pw_mgr),
        ])
    opener = net_guard.build_safe_opener(*handlers, context=_ssl_ctx)

    def soap_call(endpoint: str, inner_xml: str) -> ET.Element:
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"{_wsse_header(username, password)}<s:Body>{inner_xml}</s:Body></s:Envelope>"
        ).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"}, method="POST",
        )
        with opener.open(req, timeout=8) as r:
            return ET.fromstring(r.read(2_000_000))

    media_url = url.replace("device_service", "media_service")
    try:
        cap_xml = soap_call(
            url,
            '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"><Category>Media</Category></GetCapabilities>',
        )
        for el in cap_xml.iter():
            if el.tag.endswith("Media"):
                for child in el:
                    if child.tag.endswith("XAddr") and child.text:
                        cand = child.text.strip()
                        cand_host = (urllib.parse.urlparse(cand).hostname or "")
                        if cand_host:
                            validate_stream_host(cand_host)
                            check_resolved_host(cand_host, urllib.parse.urlparse(cand).port or 80)
                            media_url = cand
                        break
    except ValueError:
        raise
    except Exception:
        pass

    prof_xml = soap_call(media_url, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>')
    profiles: list[dict] = []
    for el in prof_xml.iter():
        if not (el.tag.endswith("Profiles") and "token" in el.attrib):
            continue
        token = el.attrib["token"]
        pname = next((ch.text.strip() for ch in el if ch.tag.endswith("Name") and ch.text), token)
        stream_uri = ""
        snapshot_uri = ""
        for proto in ("RTSP", "UDP", "HTTP"):
            try:
                s_xml = soap_call(
                    media_url,
                    '<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                    '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                    f'<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>{proto}</Protocol></Transport></StreamSetup>'
                    f"<ProfileToken>{html.escape(token)}</ProfileToken></GetStreamUri>",
                )
                stream_uri = next((sel.text.strip() for sel in s_xml.iter() if sel.tag.endswith("Uri") and sel.text), "")
                if stream_uri:
                    break
            except Exception:
                continue
        if stream_uri.startswith("rtsp://") and username and "@" not in stream_uri.split("://", 1)[1].split("/", 1)[0]:
            auth_part = urllib.parse.quote(username, safe="")
            if password:
                auth_part = f"{auth_part}:{urllib.parse.quote(password, safe='')}"
            stream_uri = f"rtsp://{auth_part}@{stream_uri[7:]}"
        try:
            snap_xml = soap_call(
                media_url,
                '<GetSnapshotUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                f"<ProfileToken>{html.escape(token)}</ProfileToken></GetSnapshotUri>",
            )
            snapshot_uri = next((sel.text.strip() for sel in snap_xml.iter() if sel.tag.endswith("Uri") and sel.text), "")
        except Exception:
            pass
        profiles.append({"token": token, "name": pname, "streamUri": stream_uri, "snapshotUri": snapshot_uri})

    return {"mediaUrl": media_url, "profiles": profiles}


# ---------- HTTP Route Handlers ----------

def handle_onvif_discover(handler) -> None:
    try:
        body = handler.read_json_body()
        res = discover_onvif_profiles(
            device_url=(body.get("url") or "").strip(),
            username=(body.get("username") or "").strip(),
            password=(body.get("password") or "").strip(),
        )
        handler._send_json(res)
    except ValueError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, f"ONVIF gagal: {exc}")


def handle_onvif_ptz(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body()
        action = (body.get("action") or "stop").strip().lower()
        manual_id = str(body.get("manualId") or "").strip()
        # PTZ hanya untuk kamera ONVIF yang sudah terdaftar: deviceUrl bebas dari
        # klien dulu bisa dipakai untuk mengirim POST ke host internal (SSRF).
        cam = next((c for c in srv.load_manual_cameras() if c.get("manualId") == manual_id), None) if manual_id else None
        if not cam or not isinstance(cam.get("onvif"), dict) or not handler._cam_ok(cam):
            handler.send_json_error(HTTPStatus.NOT_FOUND, "Kamera PTZ tidak ditemukan.")
            return
        onvif = cam["onvif"]
        device_url = str(onvif.get("deviceUrl") or "").strip()
        username = str(onvif.get("username") or "").strip()
        password = str(onvif.get("password") or "").strip()
        token = str(onvif.get("profileToken") or "").strip()
        if device_url:
            parsed = urllib.parse.urlparse(device_url if device_url.startswith(("http://", "https://")) else f"http://{device_url}")
            validate_stream_host(parsed.hostname or "")
        if not device_url or not token:
            handler.send_json_error(HTTPStatus.BAD_REQUEST, "Konfigurasi ONVIF PTZ tidak lengkap.")
            return

        ptz_url = device_url.replace("device_service", "ptz_service").replace("media_service", "ptz_service")
        inner = build_onvif_ptz_xml(action, token)
        soap_fn = srv._onvif_soap_call if srv else _onvif_soap_call
        soap_fn(ptz_url, inner, username, password)
        handler._send_json({"ok": True})
    except ValueError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, f"PTZ gagal: {exc}")


def handle_tuya_settings_get(handler) -> None:
    srv = _srv()
    if srv.tuya_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._tuya_import_error)
        return
    handler._send_json({"settings": srv.tuya_service.public_settings()})


def handle_tuya_settings_post(handler) -> None:
    srv = _srv()
    if srv.tuya_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._tuya_import_error)
        return
    try:
        body = handler.read_json_body()
        payload = body.get("settings", body)
        before = srv.tuya_service.public_settings()
        updated = srv.tuya_service.update_settings(payload)
        handler._audit_detail = settings_diff(before, updated) + (
            "; Access Secret diganti" if str(payload.get("access_secret") or "").strip() else ""
        )
        cams = srv.get_cameras(force_refresh=True)
        handler._send_json({"settings": updated, "cameras": cams})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Pengaturan Tuya tidak valid: {exc}")


def handle_tuya_test(handler) -> None:
    srv = _srv()
    if srv.tuya_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._tuya_import_error)
        return
    try:
        devices = srv.tuya_service.fetch_devices(force=True)
        cams = srv.get_cameras(force_refresh=True)
        handler._send_json({"ok": True, "devices": devices, "count": len(devices), "cameras": cams})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, str(exc))


def handle_tuya_ptz(handler) -> None:
    srv = _srv()
    if srv.tuya_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._tuya_import_error)
        return
    try:
        body = handler.read_json_body()
        dev_id = (body.get("deviceId") or "").strip()
        action = (body.get("action") or "stop").strip().lower()
        if not dev_id:
            handler.send_json_error(HTTPStatus.BAD_REQUEST, "deviceId wajib diisi.")
            return
        if not handler._key_ok(f"m:tuya-{dev_id}") or not any(
            c.get("tuyaDeviceId") == dev_id for c in srv.tuya_service.get_cameras()
        ):
            handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
            return
        srv.tuya_service.send_ptz(dev_id, action)
        handler._send_json({"ok": True})
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, f"PTZ Tuya gagal: {exc}")


def handle_tuya_hls(handler, path: str) -> None:
    srv = _srv()
    if srv.tuya_service is None:
        handler.send_json_error(HTTPStatus.SERVICE_UNAVAILABLE, srv._tuya_import_error)
        return
    parts = path.strip("/").split("/")
    if len(parts) != 4:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Path Tuya HLS tidak valid.")
        return
    dev_id = urllib.parse.unquote(parts[3].split(".", 1)[0])
    if not handler._key_ok(f"m:tuya-{dev_id}") or not any(
        c.get("tuyaDeviceId") == dev_id for c in srv.tuya_service.get_cameras()
    ):
        handler.send_json_error(HTTPStatus.FORBIDDEN, "Kamera ini di luar wilayah akun Anda.")
        return
    try:
        hls_url, _ = srv.tuya_service.allocate_stream(dev_id, "hls")
        req = urllib.request.Request(hls_url, headers={"User-Agent": USER_AGENT})
        with net_guard.safe_urlopen(req, timeout=12, context=_ssl_ctx) as resp:
            raw_text = resp.read(2_000_000).decode("utf-8", errors="replace")
        rewritten = rewrite_m3u8(raw_text, hls_url).encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_cors_headers()
        handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
        handler.send_header("Cache-Control", "no-store, no-cache")
        handler.send_header("Content-Length", str(len(rewritten)))
        handler.end_headers()
        handler.wfile.write(rewritten)
    except Exception as exc:
        handler.send_json_error(HTTPStatus.BAD_GATEWAY, f"Tuya HLS gagal: {exc}")

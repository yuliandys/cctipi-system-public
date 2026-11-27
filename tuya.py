#!/usr/bin/env python3
"""CCTipi System — Integrasi Tuya IoT Cloud OpenAPI (Smart Camera RTSP/HLS & PTZ).

Memakai pustaka standar Python saja (hmac, hashlib, urllib.request).
Kredensial disimpan di data/tuya_settings.json dengan izin 0o600;
access_secret tidak pernah dikirim kembali ke UI/API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from scrapers import _ssl_ctx
from storage_utils import redact_sensitive_text, write_private

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("CCTIPI_STATE_DIR") or BASE_DIR).resolve()  # data runtime & rahasia
DATA_DIR = STATE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "tuya_settings.json"

TUYA_ENDPOINTS: dict[str, tuple[str, str]] = {
    "sg": ("Singapore", "https://openapi-sg.iotbing.com"),
    "eu": ("Central Europe", "https://openapi.tuyaeu.com"),
    "eu-w": ("Western Europe", "https://openapi-weaz.tuyaeu.com"),
    "us": ("Western America", "https://openapi.tuyaus.com"),
    "us-e": ("Eastern America", "https://openapi-ueaz.tuyaus.com"),
    "in": ("India", "https://openapi.tuyain.com"),
    "cn": ("China", "https://openapi.tuyacn.com"),
}

DEFAULT_SETTINGS: dict = {
    "enabled": False,
    "region": "sg",
    "access_id": "",
    "access_secret": "",
    "uid": "",
    "device_ids": "",
    "stream_type": "rtsp",  # "rtsp" atau "hls"
}

_PTZ_CODES: dict[str, tuple[str, str]] = {
    "up": ("ptz_control", "0"),
    "right": ("ptz_control", "2"),
    "down": ("ptz_control", "4"),
    "left": ("ptz_control", "6"),
    "zoom_in": ("zoom_control", "0"),
    "zoom_out": ("zoom_control", "1"),
    "stop": ("ptz_stop", True),  # type: ignore[dict-item]
}

_STREAM_CACHE_TTL = 180  # 3 menit (URL stream Tuya bersifat sementara)
_DEVICES_CACHE_TTL = 300  # 5 menit


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TuyaService:
    """Klien Tuya OpenAPI v1.0 untuk kamera CCTV Smart Life / Tuya."""

    def __init__(self, settings_path: Path = SETTINGS_FILE):
        self._path = settings_path
        self._lock = threading.Lock()
        self._settings = self._load_settings()
        self._token: str = ""
        self._token_exp: float = 0.0
        self._devices_cache: list[dict] = []
        self._devices_exp: float = 0.0
        self._stream_cache: dict[tuple[str, str], tuple[str, float]] = {}

    def _load_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k in DEFAULT_SETTINGS:
                        if k in data:
                            out[k] = data[k]
                os.chmod(str(self._path), 0o600)
            except Exception:
                pass
        return out

    def _save_settings(self) -> None:
        write_private(self._path, json.dumps(self._settings, indent=2, ensure_ascii=False))

    def public_settings(self) -> dict:
        with self._lock:
            s = dict(self._settings)
        return {
            "enabled": bool(s.get("enabled")),
            "region": s.get("region") if s.get("region") in TUYA_ENDPOINTS else "sg",
            "regions": [{"id": k, "label": v[0]} for k, v in TUYA_ENDPOINTS.items()],
            "access_id": s.get("access_id", ""),
            "has_secret": bool(s.get("access_secret")),
            "uid": s.get("uid", ""),
            "device_ids": s.get("device_ids", ""),
            "stream_type": s.get("stream_type") if s.get("stream_type") in ("rtsp", "hls") else "rtsp",
            "configured": bool(s.get("access_id") and s.get("access_secret") and (s.get("uid") or s.get("device_ids"))),
        }

    def update_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Payload Tuya tidak valid")
        with self._lock:
            s = self._settings
            if "enabled" in payload:
                s["enabled"] = bool(payload["enabled"])
            if "region" in payload:
                reg = str(payload["region"]).strip().lower()
                if reg not in TUYA_ENDPOINTS:
                    raise ValueError(f"Region Tuya tidak dikenal: {reg}")
                s["region"] = reg
            if "access_id" in payload:
                s["access_id"] = str(payload["access_id"]).strip()
            secret = payload.get("access_secret")
            if isinstance(secret, str) and secret.strip():
                s["access_secret"] = secret.strip()
            if "uid" in payload:
                s["uid"] = str(payload["uid"]).strip()
            if "device_ids" in payload:
                s["device_ids"] = str(payload["device_ids"]).strip()
            if "stream_type" in payload:
                st = str(payload["stream_type"]).strip().lower()
                if st not in ("rtsp", "hls"):
                    raise ValueError("stream_type harus 'rtsp' atau 'hls'")
                s["stream_type"] = st
            self._token = ""
            self._token_exp = 0.0
            self._devices_exp = 0.0
            self._stream_cache.clear()
            self._save_settings()
        return self.public_settings()

    def _base_url(self, region: str) -> str:
        return TUYA_ENDPOINTS.get(region, TUYA_ENDPOINTS["sg"])[1]

    @staticmethod
    def sign_request(
        access_id: str,
        access_secret: str,
        method: str,
        path_with_query: str,
        body_bytes: bytes,
        timestamp_ms: str,
        access_token: str = "",
    ) -> str:
        """Hitung signature HMAC-SHA256 Tuya OpenAPI v1.0."""
        content_hash = _sha256_hex(body_bytes)
        string_to_sign = f"{method.upper()}\n{content_hash}\n\n{path_with_query}"
        raw = f"{access_id}{access_token}{timestamp_ms}{string_to_sign}"
        return hmac.new(
            access_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()

    def _raw_call(
        self,
        method: str,
        path_with_query: str,
        body_obj: dict | None = None,
        access_token: str = "",
    ) -> dict:
        with self._lock:
            region = self._settings.get("region", "sg")
            access_id = self._settings.get("access_id", "")
            access_secret = self._settings.get("access_secret", "")
        if not access_id or not access_secret:
            raise RuntimeError("Access ID dan Access Secret Tuya belum diisi.")

        body_bytes = json.dumps(body_obj).encode("utf-8") if body_obj is not None else b""
        t_ms = str(int(time.time() * 1000))
        sig = self.sign_request(
            access_id, access_secret, method, path_with_query, body_bytes, t_ms, access_token
        )
        headers = {
            "client_id": access_id,
            "sign": sig,
            "t": t_ms,
            "sign_method": "HMAC-SHA256",
        }
        if access_token:
            headers["access_token"] = access_token
        if body_obj is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self._base_url(region)}{path_with_query}"
        req = urllib.request.Request(url, data=body_bytes or None, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=12, context=_ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = redact_sensitive_text(exc.read().decode("utf-8", errors="replace")[:200])
            raise RuntimeError(f"Tuya HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(redact_sensitive_text(f"Gagal menghubungi Tuya Cloud: {exc}")) from exc

        if not data.get("success"):
            msg = data.get("msg") or data.get("code") or "Permintaan Tuya ditolak"
            raise RuntimeError(f"Tuya API error: {msg}")
        return data

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_exp - 30:
                return self._token
        data = self._raw_call("GET", "/v1.0/token?grant_type=1")
        res = data.get("result") or {}
        token = res.get("access_token") or ""
        if not token:
            raise RuntimeError("Token akses Tuya kosong.")
        expire = int(res.get("expire_time") or 3600)
        with self._lock:
            self._token = token
            self._token_exp = time.time() + expire
        return token

    def _api(self, method: str, path_with_query: str, body_obj: dict | None = None) -> dict:
        token = self._get_token()
        try:
            return self._raw_call(method, path_with_query, body_obj, access_token=token)
        except RuntimeError as exc:
            if "token" in str(exc).lower() or "1010" in str(exc):
                token = self._get_token(force=True)
                return self._raw_call(method, path_with_query, body_obj, access_token=token)
            raise

    def fetch_devices(self, force: bool = False) -> list[dict]:
        """Ambil daftar kamera Tuya berdasarkan UID atau daftar device_ids."""
        with self._lock:
            enabled = bool(self._settings.get("enabled"))
            uid = self._settings.get("uid", "").strip()
            dev_ids_raw = self._settings.get("device_ids", "").strip()
            if not force and self._devices_cache and time.time() < self._devices_exp:
                return list(self._devices_cache)

        if not enabled and not force:
            return []

        devices: list[dict] = []
        seen: set[str] = set()

        if dev_ids_raw:
            for raw_id in dev_ids_raw.split(","):
                did = raw_id.strip()
                if not did or did in seen:
                    continue
                seen.add(did)
                info = self._api("GET", f"/v1.0/devices/{urllib.parse.quote(did, safe='')}").get("result") or {}
                devices.append({
                    "id": info.get("id") or did,
                    "name": info.get("name") or f"Tuya {did[:8]}",
                    "category": info.get("category") or "sp",
                    "online": bool(info.get("online", True)),
                })
        elif uid:
            res = self._api("GET", f"/v1.0/users/{urllib.parse.quote(uid, safe='')}/devices").get("result") or []
            cam_cats = {"sp", "ipc", "camera", "sxj"}
            cams = [d for d in res if str(d.get("category", "")).lower() in cam_cats]
            for d in cams or res:
                did = str(d.get("id") or "").strip()
                if not did or did in seen:
                    continue
                seen.add(did)
                devices.append({
                    "id": did,
                    "name": d.get("name") or f"Tuya {did[:8]}",
                    "category": d.get("category") or "sp",
                    "online": bool(d.get("online", True)),
                })
        else:
            res_obj = self._api("GET", "/v1.0/iot-01/associated-users/devices").get("result") or {}
            res = res_obj.get("list") or []
            cam_cats = {"sp", "ipc", "camera", "sxj"}
            cams = [d for d in res if str(d.get("category", "")).lower() in cam_cats]
            for d in cams or res:
                did = str(d.get("id") or "").strip()
                if not did or did in seen:
                    continue
                seen.add(did)
                devices.append({
                    "id": did,
                    "name": d.get("name") or f"Tuya {did[:8]}",
                    "category": d.get("category") or "sp",
                    "online": bool(d.get("online", True)),
                })
            if not devices and force:
                raise RuntimeError(
                    "Token Tuya valid, tetapi 0 perangkat ditemukan. "
                    "Pastikan aplikasi Smart Life sudah di-link di iot.tuya.com (tab Devices → Link App Account) "
                    "sesuai Region, dan isi User UID atau Device IDs."
                )

        with self._lock:
            self._devices_cache = list(devices)
            self._devices_exp = time.time() + _DEVICES_CACHE_TTL
        return devices

    def get_cameras(self) -> list[dict]:
        """Daftar kamera format CCTipi (non-blocking terhadap error jaringan)."""
        with self._lock:
            if not self._settings.get("enabled"):
                return []
            stream_type = self._settings.get("stream_type", "rtsp")
        try:
            devs = self.fetch_devices()
        except Exception:
            with self._lock:
                devs = list(self._devices_cache)
        out: list[dict] = []
        for d in devs:
            did = d["id"]
            out.append({
                "name": d.get("name") or f"Tuya {did[:8]}",
                "url": f"tuya://{did}",
                "type": stream_type,
                "provider": "tuya",
                "folder": "Tuya",
                "manualId": f"tuya-{did}",
                "tuyaDeviceId": did,
                "tuyaOnline": bool(d.get("online", True)),
            })
        return out

    def allocate_stream(self, device_id: str, stream_type: str | None = None) -> tuple[str, str]:
        """Minta URL stream RTSP/HLS sementara dari Tuya Cloud."""
        with self._lock:
            st = (stream_type or self._settings.get("stream_type") or "rtsp").lower()
            cached = self._stream_cache.get((device_id, st))
            if cached and time.time() < cached[1]:
                return cached[0], st

        data = self._api(
            "POST",
            f"/v1.0/devices/{urllib.parse.quote(device_id, safe='')}/stream/actions/allocate",
            {"type": st},
        )
        url = ((data.get("result") or {}).get("url") or "").strip()
        if not url:
            raise RuntimeError(f"Tuya tidak mengembalikan URL {st.upper()} untuk perangkat ini.")
        with self._lock:
            self._stream_cache[(device_id, st)] = (url, time.time() + _STREAM_CACHE_TTL)
        return url, st

    def cached_stream_hosts(self) -> frozenset[str]:
        """Host dari URL stream Tuya yang sedang aktif di cache (untuk allowlist proxy)."""
        hosts: set[str] = set()
        now = time.time()
        with self._lock:
            for (url, exp) in self._stream_cache.values():
                if exp > now:
                    h = (urllib.parse.urlparse(url).hostname or "").lower()
                    if h:
                        hosts.add(h)
        return frozenset(hosts)

    def send_ptz(self, device_id: str, action: str) -> None:
        """Kirim perintah PTZ ke kamera Tuya."""
        act = (action or "stop").strip().lower()
        mapping = _PTZ_CODES.get(act, _PTZ_CODES["stop"])
        code, value = mapping
        self._api(
            "POST",
            f"/v1.0/devices/{urllib.parse.quote(device_id, safe='')}/commands",
            {"commands": [{"code": code, "value": value}]},
        )

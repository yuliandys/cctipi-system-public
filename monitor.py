"""Alarm kamera offline & penyimpanan hampir penuh — v6.7.

Worker latar memeriksa kamera yang dipilih (opt-in) secara berkala. Kamera
yang gagal dicek terus-menerus selama >= `offline_minutes` dianggap offline:
kirim 1 notifikasi Telegram, lalu 1 notifikasi lagi saat kembali online.
Penyimpanan (folder data/ & kuota rekaman) dicek dengan cara yang sama.
Semua kejadian juga masuk log aktivitas sebagai pengguna "sistem".
"""
from __future__ import annotations

import concurrent.futures
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from storage_utils import write_private

DEFAULTS = {
    "enabled": False,
    "cameras": [],            # kunci kamera yang dipantau
    "interval": 120,          # detik antar pemeriksaan
    "offline_minutes": 5,     # lama gagal sebelum dianggap offline
    "notify_recovery": True,  # kirim notifikasi saat kembali online
    "disk_alert_percent": 90, # ambang disk/kuota rekaman
    "telegram_enabled": True,
}


class MonitorService:
    def __init__(self, path: Path, *, get_cameras: Callable[[], list[dict]],
                 camera_key: Callable[[dict], str], probe: Callable[[dict], str],
                 send_text: Callable[[str], tuple[bool, str]] | None,
                 audit: Callable[..., None] | None, data_dir: Path,
                 recorder_status: Callable[[], dict] | None = None):
        self._path = path
        self._get_cameras = get_cameras
        self._camera_key = camera_key
        self._probe = probe
        self._send_text = send_text
        self._audit = audit
        self._data_dir = data_dir
        self._recorder_status = recorder_status
        self._lock = threading.Lock()
        self._settings = self._load()
        # kunci -> {"name", "folder", "status", "fail_since", "alerted", "checked"}
        self._state: dict[str, dict] = {}
        self._disk: dict = {"alerted": False}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""
        self._last_run = 0.0

    # ---------- pengaturan ----------
    def _load(self) -> dict:
        s = dict(DEFAULTS)
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                s.update({k: saved[k] for k in DEFAULTS if k in saved})
        except Exception:
            pass
        return s

    def settings(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def update_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Payload pengaturan tidak valid")
        with self._lock:
            s = self._settings
            if "enabled" in payload:
                s["enabled"] = bool(payload["enabled"])
            if "cameras" in payload and isinstance(payload["cameras"], list):
                s["cameras"] = [str(k) for k in payload["cameras"]][:500]
            if "interval" in payload:
                s["interval"] = min(3600, max(30, int(payload["interval"])))
            if "offline_minutes" in payload:
                s["offline_minutes"] = min(1440, max(1, int(payload["offline_minutes"])))
            if "notify_recovery" in payload:
                s["notify_recovery"] = bool(payload["notify_recovery"])
            if "disk_alert_percent" in payload:
                s["disk_alert_percent"] = min(99, max(50, int(payload["disk_alert_percent"])))
            if "telegram_enabled" in payload:
                s["telegram_enabled"] = bool(payload["telegram_enabled"])
            write_private(self._path, json.dumps(s, indent=2))
        self._wake.set()  # periksa ulang segera dengan pengaturan baru
        return self.settings()

    # ---------- status ----------
    def disk_info(self) -> dict:
        info: dict = {}
        try:
            du = shutil.disk_usage(str(self._data_dir))
            info.update({"disk_total": du.total, "disk_used": du.used, "disk_free": du.free,
                         "disk_percent": round(du.used * 100 / du.total, 1) if du.total else 0})
        except OSError:
            pass
        if self._recorder_status:
            try:
                rs = self._recorder_status() or {}
                quota, used = int(rs.get("quota_bytes") or 0), int(rs.get("total_size_bytes") or 0)
                if quota:
                    info.update({"quota_bytes": quota, "quota_used": used,
                                 "quota_percent": round(used * 100 / quota, 1)})
            except Exception:
                pass
        return info

    def status(self, allowed: set[str] | None = None) -> dict:
        with self._lock:
            s = dict(self._settings)
            rows = [dict(v, key=k) for k, v in self._state.items()
                    if k in set(s.get("cameras") or []) and (allowed is None or k in allowed)]
        rows.sort(key=lambda r: (r.get("status") != "offline", r.get("name") or ""))
        return {
            "enabled": bool(s.get("enabled")),
            "running": bool(s.get("enabled")) and bool(self._thread and self._thread.is_alive()),
            "monitored": len(s.get("cameras") or []),
            "offline": sum(1 for r in rows if r.get("status") == "offline"),
            "cameras": rows,
            "disk": {**self.disk_info(), "alerted": bool(self._disk.get("alerted"))},
            "last_run": int(self._last_run),
            "last_error": self._last_error,
        }

    # ---------- worker ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="cctipi-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _notify(self, text: str) -> None:
        with self._lock:
            tg = bool(self._settings.get("telegram_enabled"))
        if tg and self._send_text:
            ok, msg = self._send_text(text)
            if not ok:
                self._last_error = f"Telegram: {msg}"

    def _log(self, action: str, target: str, detail: str = "") -> None:
        if self._audit:
            try:
                self._audit("sistem", action, target, detail)
            except Exception:
                pass

    def run_once(self, now: float | None = None) -> None:
        """Satu putaran pemeriksaan (dipanggil worker; juga dipakai tes)."""
        now = time.time() if now is None else now
        with self._lock:
            s = dict(self._settings)
        keys = set(s.get("cameras") or [])
        cams = [c for c in self._get_cameras() if self._camera_key(c) in keys]
        limit = int(s.get("offline_minutes") or 5) * 60
        results: dict[str, str] = {}
        if cams:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                for cam, st in zip(cams, pool.map(self._safe_probe, cams)):
                    results[self._camera_key(cam)] = st
        events: list[tuple[str, str, str, str]] = []
        with self._lock:
            for cam in cams:
                key = self._camera_key(cam)
                st = self._state.setdefault(key, {"status": "unknown", "fail_since": 0, "alerted": False})
                st.update(name=cam.get("name") or key, folder=cam.get("folder") or "", checked=int(now))
                if results.get(key) == "online":
                    if st["alerted"]:
                        down = int((now - st["fail_since"]) / 60)
                        events.append(("alarm.online", st["name"], f"offline ±{down} menit",
                                       f"✅ Kamera kembali online\n{st['name']} ({st['folder']})\nSempat offline ±{down} menit."))
                    st.update(status="online", fail_since=0, alerted=False)
                else:
                    if not st["fail_since"]:
                        st["fail_since"] = now
                    st["status"] = "offline" if now - st["fail_since"] >= limit else "gangguan"
                    if st["status"] == "offline" and not st["alerted"]:
                        st["alerted"] = True
                        since = time.strftime("%H:%M", time.localtime(st["fail_since"]))
                        events.append(("alarm.offline", st["name"], f"sejak {since}",
                                       f"🔴 Kamera offline\n{st['name']} ({st['folder']})\nTidak merespons sejak {since} (≥ {limit // 60} menit)."))
            # buang status kamera yang tidak dipantau lagi
            for k in list(self._state):
                if k not in keys:
                    self._state.pop(k)
        # disk / kuota
        disk = self.disk_info()
        pct = max(disk.get("disk_percent") or 0, disk.get("quota_percent") or 0)
        thr = int(s.get("disk_alert_percent") or 90)
        if pct >= thr and not self._disk.get("alerted"):
            self._disk["alerted"] = True
            which = "kuota rekaman" if (disk.get("quota_percent") or 0) >= (disk.get("disk_percent") or 0) else "disk server"
            events.append(("alarm.disk", which, f"{pct}% terpakai",
                           f"🟠 Penyimpanan hampir penuh\n{which.capitalize()} {pct}% terpakai (ambang {thr}%).\nHapus rekaman lama atau tambah kapasitas."))
        elif pct < thr - 5 and self._disk.get("alerted"):
            self._disk["alerted"] = False
            events.append(("alarm.disk_ok", "penyimpanan", f"{pct}% terpakai", ""))
        for action, target, detail, text in events:
            self._log(action, target, detail)
            if text and (action != "alarm.online" or s.get("notify_recovery")):
                self._notify(text)
        self._last_run = now

    def _safe_probe(self, cam: dict) -> str:
        try:
            return self._probe(cam)
        except Exception:
            return "offline"

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled = bool(self._settings.get("enabled"))
                interval = int(self._settings.get("interval") or 120)
            if enabled:
                try:
                    self.run_once()
                    self._last_error = "" if not self._last_error.startswith("Telegram") else self._last_error
                except Exception as exc:
                    self._last_error = f"Monitor: {exc.__class__.__name__}: {exc}"
            self._wake.wait(interval if enabled else 15)
            self._wake.clear()

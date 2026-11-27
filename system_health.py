#!/usr/bin/env python3
"""Observabilitas, validasi konfigurasi awal, log terstruktur & batas beban proses."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

from storage_utils import redact_sensitive_text

SERVER_STARTED_AT = time.time()


class ConfigError(ValueError):
    """Kesalahan konfigurasi saat startup yang wajib menghentikan server."""


class CapacityError(RuntimeError):
    """Batas konkurensi server penuh -> HTTP 503 Service Unavailable."""


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Variabel lingkungan {name}={raw!r} harus berupa bilangan bulat.") from exc
    if not (minimum <= val <= maximum):
        raise ConfigError(f"Variabel lingkungan {name}={val} harus di antara {minimum} dan {maximum}.")
    return val


def log_event(level: str, component: str, message: str, **fields) -> None:
    """Tulis log terstruktur ke stderr dengan redaksi rahasia otomatis."""
    safe_msg = redact_sensitive_text(str(message))
    safe_fields = {k: redact_sensitive_text(str(v)) for k, v in fields.items() if v is not None and v != ""}
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fmt = os.environ.get("CCTIPI_LOG_FORMAT", "").strip().lower()
    if fmt == "json":
        payload = {"ts": ts, "level": level.upper(), "component": component, "msg": safe_msg, **safe_fields}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
        return
    extra = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in safe_fields.items())
    line = f"{ts} [{level.upper()}] [{component}] {safe_msg}" + (f" {extra}" if extra else "")
    print(line, file=sys.stderr, flush=True)


class ConcurrencyGuard:
    """Batas beban proses ffmpeg, jembatan RTSP, rekaman aktif, dan penonton stream."""

    def __init__(
        self,
        *,
        max_ffmpeg: int | None = None,
        max_rtsp_bridges: int | None = None,
        max_recordings: int | None = None,
        max_viewers_per_camera: int | None = None,
        max_total_viewers: int | None = None,
    ) -> None:
        self.max_ffmpeg = max_ffmpeg if max_ffmpeg is not None else _env_int("CCTIPI_MAX_FFMPEG_PROCS", 12, minimum=1, maximum=256)
        self.max_rtsp_bridges = max_rtsp_bridges if max_rtsp_bridges is not None else _env_int("CCTIPI_MAX_RTSP_BRIDGES", 8, minimum=1, maximum=128)
        self.max_recordings = max_recordings if max_recordings is not None else _env_int("CCTIPI_MAX_CONCURRENT_RECORDINGS", 4, minimum=1, maximum=64)
        self.max_viewers_per_camera = max_viewers_per_camera if max_viewers_per_camera is not None else _env_int("CCTIPI_MAX_VIEWERS_PER_CAMERA", 8, minimum=1, maximum=256)
        self.max_total_viewers = max_total_viewers if max_total_viewers is not None else _env_int("CCTIPI_MAX_STREAM_VIEWERS", 32, minimum=1, maximum=1024)
        self._lock = threading.Lock()
        self._ffmpeg_by_kind: dict[str, int] = {}
        self._viewers_by_cam: dict[str, int] = {}

    def acquire_ffmpeg(self, kind: str) -> None:
        with self._lock:
            total = sum(self._ffmpeg_by_kind.values())
            if total >= self.max_ffmpeg:
                raise CapacityError(
                    f"Server sibuk: batas proses ffmpeg aktif tercapai ({total}/{self.max_ffmpeg}). Coba lagi sebentar."
                )
            if kind == "recorder" and self._ffmpeg_by_kind.get("recorder", 0) >= self.max_recordings:
                raise CapacityError(
                    f"Batas rekaman bersamaan tercapai (maks {self.max_recordings} kamera)."
                )
            if kind == "rtsp" and self._ffmpeg_by_kind.get("rtsp", 0) >= self.max_rtsp_bridges:
                raise CapacityError(
                    f"Batas jembatan stream RTSP aktif tercapai (maks {self.max_rtsp_bridges} kamera)."
                )
            self._ffmpeg_by_kind[kind] = self._ffmpeg_by_kind.get(kind, 0) + 1

    def release_ffmpeg(self, kind: str) -> None:
        with self._lock:
            cur = self._ffmpeg_by_kind.get(kind, 0)
            if cur <= 1:
                self._ffmpeg_by_kind.pop(kind, None)
            else:
                self._ffmpeg_by_kind[kind] = cur - 1

    @contextlib.contextmanager
    def ffmpeg_slot(self, kind: str) -> Iterator[None]:
        self.acquire_ffmpeg(kind)
        try:
            yield
        finally:
            self.release_ffmpeg(kind)

    @contextlib.contextmanager
    def viewer_slot(self, camera_id: str) -> Iterator[None]:
        key = (camera_id or "unknown")[:200]
        with self._lock:
            total = sum(self._viewers_by_cam.values())
            if total >= self.max_total_viewers:
                raise CapacityError(
                    f"Batas total penonton stream bersamaan penuh ({total}/{self.max_total_viewers})."
                )
            cam_cur = self._viewers_by_cam.get(key, 0)
            if cam_cur >= self.max_viewers_per_camera:
                raise CapacityError(
                    f"Batas penonton bersamaan untuk kamera ini penuh ({cam_cur}/{self.max_viewers_per_camera})."
                )
            self._viewers_by_cam[key] = cam_cur + 1
        try:
            yield
        finally:
            with self._lock:
                cur = self._viewers_by_cam.get(key, 0)
                if cur <= 1:
                    self._viewers_by_cam.pop(key, None)
                else:
                    self._viewers_by_cam[key] = cur - 1

    def snapshot(self) -> dict:
        with self._lock:
            by_kind = dict(self._ffmpeg_by_kind)
            total_viewers = sum(self._viewers_by_cam.values())
            busy_cams = len(self._viewers_by_cam)
        return {
            "ffmpeg_active": sum(by_kind.values()),
            "ffmpeg_max": self.max_ffmpeg,
            "ffmpeg_by_kind": by_kind,
            "rtsp_bridges_max": self.max_rtsp_bridges,
            "recordings_max": self.max_recordings,
            "viewers_active": total_viewers,
            "viewers_max": self.max_total_viewers,
            "viewers_per_camera_max": self.max_viewers_per_camera,
            "streamed_cameras": busy_cams,
        }


LOAD_GUARD = ConcurrencyGuard()


def process_memory_info() -> dict:
    """Ambil penggunaan RAM proses ini & sistem (stdlib-only: Linux/Termux, POSIX, Windows)."""
    rss_bytes = 0
    sys_total = 0
    sys_avail = 0

    if Path("/proc/self/status").is_file():
        try:
            for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    rss_bytes = int(line.split()[1]) * 1024
                    break
        except Exception:
            pass
    if Path("/proc/meminfo").is_file():
        try:
            mem: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1]) * 1024
            sys_total = mem.get("MemTotal", 0)
            sys_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        except Exception:
            pass

    if os.name == "nt":
        try:
            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                rss_bytes = int(pmc.WorkingSetSize)

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = _MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(ms)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                sys_total = int(ms.ullTotalPhys)
                sys_avail = int(ms.ullAvailPhys)
        except Exception:
            pass
    elif not rss_bytes:
        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_bytes = int(ru if sys.platform == "darwin" else ru * 1024)
        except Exception:
            pass

    out: dict = {
        "rss_bytes": rss_bytes,
        "rss_mb": round(rss_bytes / 1_048_576, 1) if rss_bytes else 0.0,
    }
    if sys_total > 0:
        used = max(0, sys_total - sys_avail)
        out.update({
            "system_total_bytes": sys_total,
            "system_avail_bytes": sys_avail,
            "system_percent": round(used * 100.0 / sys_total, 1),
        })
    return out


def is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


def validate_startup_config(
    *,
    host: str,
    port_raw: str | int,
    users_store,
    data_dir: Path,
    sources: list[dict] | None = None,
    ai_service=None,
) -> dict:
    """Validasi konfigurasi awal saat server dinyalakan. Raise ConfigError bila fatal."""
    host_clean = (host or "").strip()
    if not host_clean:
        raise ConfigError("HOST tidak boleh kosong (gunakan 127.0.0.1 atau 0.0.0.0).")
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"PORT={port_raw!r} tidak valid; isi angka port 1–65535.") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"PORT={port} di luar jangkauan 1–65535.")

    # Validasi variabel lingkungan numerik opsional sejak awal
    _env_int("CCTIPI_AUDIT_DAYS", 180, minimum=1, maximum=3650)
    _env_int("CCTIPI_LIVEAI_AUTOSTOP_SECONDS", 600, minimum=10, maximum=86400)
    _env_int("CCTIPI_LIVEAI_NO_VIEWER_SECONDS", 45, minimum=5, maximum=3600)
    _env_int("CCTIPI_MAX_FFMPEG_PROCS", 12, minimum=1, maximum=256)
    _env_int("CCTIPI_MAX_RTSP_BRIDGES", 8, minimum=1, maximum=128)
    _env_int("CCTIPI_MAX_CONCURRENT_RECORDINGS", 4, minimum=1, maximum=64)
    _env_int("CCTIPI_MAX_VIEWERS_PER_CAMERA", 8, minimum=1, maximum=256)

    pw_env = os.environ.get("CCTIPI_PASS")
    if pw_env is not None and len(pw_env) < 8:
        raise ConfigError("CCTIPI_PASS minimal 8 karakter.")

    # P0 #1: Tolak start bila diekspos ke jaringan (HOST != loopback) dengan password bawaan
    has_default = bool(users_store and users_store.has_default_credentials())
    if not is_loopback_host(host_clean) and has_default:
        raise ConfigError(
            f"Menolak berjalan di HOST={host_clean} selama akun masih memakai password bawaan "
            "(admin/cctipi123). Jalankan dulu di HOST=127.0.0.1 untuk mengganti password, "
            "atau set variabel lingkungan CCTIPI_PASS dengan password kuat (minimal 8 karakter)."
        )

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Folder data tidak dapat dibuat ({data_dir}): {exc}") from exc

    warnings: list[str] = []
    if has_default:
        warnings.append("Akun masih memakai password bawaan — login pertama wajib mengganti password.")
    if sources and any(s.get("folder") == "Kudus" for s in sources) and not os.environ.get("CCTIPI_KUDUS_API_KEY", "").strip():
        warnings.append("CCTIPI_KUDUS_API_KEY belum diisi — sumber daerah Kudus akan memakai fallback bila ada.")
    if ai_service is not None:
        pub = ai_service.public_settings()
        if pub.get("telegram_enabled") and not pub.get("telegram_configured"):
            warnings.append("Notifikasi Telegram diaktifkan tetapi token/chat ID belum lengkap.")
    if shutil.which("ffmpeg") is None:
        warnings.append("ffmpeg tidak ditemukan di PATH — RTSP, Rekaman Server, dan Live AI nonaktif.")

    return {"host": host_clean, "port": port, "warnings": warnings}


def build_health_report(
    *,
    data_dir: Path,
    cameras_payload: dict,
    rtsp_bridges_active: int,
    recorder_service=None,
    ai_service=None,
    live_ai_service=None,
    monitor_service=None,
    mjpeg_hub=None,
    guard: ConcurrencyGuard = LOAD_GUARD,
) -> dict:
    """Bangun laporan kesehatan jujur untuk /api/health & Health Monitor."""
    reasons: list[str] = []
    disk: dict = {}
    try:
        du = shutil.disk_usage(str(data_dir))
        pct = round(du.used * 100.0 / du.total, 1) if du.total else 0.0
        disk = {
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "percent": pct,
        }
        if pct >= 95.0 or du.free < 50 * 1_048_576:
            reasons.append(f"Penyimpanan disk hampir penuh ({pct}%).")
    except OSError as exc:
        reasons.append(f"Gagal membaca status disk: {exc}")

    rec_st = recorder_service.status() if recorder_service is not None else None
    if rec_st and rec_st.get("quota_bytes"):
        q_used = int(rec_st.get("total_size_bytes") or 0)
        q_max = int(rec_st["quota_bytes"])
        disk["quota_bytes"] = q_max
        disk["quota_used_bytes"] = q_used
        disk["quota_percent"] = round(q_used * 100.0 / q_max, 1) if q_max else 0.0

    api_status = cameras_payload.get("apiStatus") or {}
    src_online = sum(1 for v in api_status.values() if isinstance(v, dict) and v.get("online"))
    src_offline = sum(1 for v in api_status.values() if isinstance(v, dict) and v.get("online") is False)
    if api_status and src_online == 0 and src_offline > 0:
        reasons.append("Semua sumber API publik sedang offline (memakai cache).")

    mon_st = monitor_service.status() if monitor_service is not None else None
    monitored_cams = (mon_st or {}).get("cameras") or []
    cam_online = sum(1 for c in monitored_cams if c.get("status") == "online")
    cam_offline = sum(1 for c in monitored_cams if c.get("status") == "offline")
    if cam_offline > 0:
        reasons.append(f"{cam_offline} kamera terpantau sedang offline.")

    load = guard.snapshot()
    rec_procs = int((rec_st or {}).get("ffmpeg_processes") or 0)
    live_st = live_ai_service.status() if live_ai_service is not None else None
    live_active = bool((live_st or {}).get("active"))
    ffmpeg_avail = shutil.which("ffmpeg") is not None
    if not ffmpeg_avail:
        reasons.append("ffmpeg tidak tersedia di server.")

    observed_ffmpeg = max(load["ffmpeg_active"], rtsp_bridges_active + rec_procs + (1 if live_active else 0))
    if observed_ffmpeg >= load["ffmpeg_max"]:
        reasons.append(f"Kapasitas proses ffmpeg penuh ({observed_ffmpeg}/{load['ffmpeg_max']}).")

    mem = process_memory_info()
    mjpeg_stats = mjpeg_hub.stats() if mjpeg_hub is not None else {"active_readers": 0}
    ai_st = ai_service.status() if ai_service is not None else None

    status_str = "degraded" if reasons else "ok"
    return {
        "ok": status_str == "ok",
        "status": status_str,
        "reasons": reasons,
        "uptime_seconds": max(0, int(time.time() - SERVER_STARTED_AT)),
        "ffmpeg": {
            "available": ffmpeg_avail,
            "active_processes": observed_ffmpeg,
            "max_processes": load["ffmpeg_max"],
            "rtsp_bridges": rtsp_bridges_active,
            "rtsp_bridges_max": load["rtsp_bridges_max"],
            "recorder_processes": rec_procs,
            "recorder_max": load["recordings_max"],
            "live_ai_active": live_active,
        },
        "viewers": {
            "active": load["viewers_active"],
            "max": load["viewers_max"],
            "per_camera_max": load["viewers_per_camera_max"],
            "mjpeg_readers": mjpeg_stats.get("active_readers", 0),
        },
        "memory": mem,
        "disk": disk,
        "cameras": {
            "total": int(cameras_payload.get("count") or len(cameras_payload.get("cameras") or [])),
            "source": cameras_payload.get("source", ""),
            "sources_online": src_online,
            "sources_offline": src_offline,
            "monitored_online": cam_online,
            "monitored_offline": cam_offline,
        },
        "services": {
            "ai_running": bool((ai_st or {}).get("running")),
            "recorder_running": bool((rec_st or {}).get("running")),
            "monitor_running": bool((mon_st or {}).get("running")),
            "live_ai_active": live_active,
        },
    }

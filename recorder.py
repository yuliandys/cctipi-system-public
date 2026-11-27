#!/usr/bin/env python3
"""CCTipi System v1.1 — Modul Rekaman Server (dengan batas konkurensi & pemulihan disk penuh/ffmpeg)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ai import _http_headers_for, cam_type, camera_key, ffmpeg_input_allowed, ffmpeg_timeout_args, normalize_key
from storage_utils import closing_sqlite, redact_sensitive_text, write_private
from system_health import LOAD_GUARD, CapacityError, log_event

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("CCTIPI_STATE_DIR") or BASE_DIR).resolve()  # data runtime & rahasia
DATA_DIR = STATE_DIR / "data"
REKAMAN_DIR = DATA_DIR / "rekaman"
DB_FILE = DATA_DIR / "cctipi.db"
SETTINGS_FILE = DATA_DIR / "recorder_settings.json"

MODE_CONTINUOUS = "berkelanjutan"
MODE_DETECTION = "deteksi"

DEFAULT_SETTINGS: dict = {
    "record_enabled": False,
    "continuous_cameras": [],
    "segment_minutes": 5,
    "clip_on_detect": True,
    "clip_seconds": 30,
    "retention_days": 7,
    "quota_gb": 20,
}

_CLEANUP_INTERVAL = 600
_MIN_SEGMENT_BYTES = 8192
_SIZE_CACHE_TTL = 60
_WATCHDOG_GRACE = 60
MIN_FREE_DISK_BYTES = int(os.environ.get("CCTIPI_MIN_FREE_DISK_MB", "50")) * 1_048_576


def _cam_dir_name(camera_id: str) -> str:
    digest = hashlib.sha1(camera_id.encode("utf-8")).hexdigest()[:12]
    return f"cam-{digest}"


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=15,
        )
        return max(0.0, float((proc.stdout or b"").decode("utf-8", "replace").strip()))
    except Exception:
        return 0.0


def _is_disk_full_error(exc_or_msg) -> bool:
    if isinstance(exc_or_msg, OSError) and getattr(exc_or_msg, "errno", None) == errno.ENOSPC:
        return True
    txt = str(exc_or_msg).lower()
    return "no space left on device" in txt or "database or disk is full" in txt or "disk full" in txt


class RecorderService:
    def __init__(self, get_cameras_fn):
        self._get_cameras = get_cameras_fn
        self._lock = threading.Lock()
        self._settings = self._load_settings()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cam_threads: dict[str, threading.Thread] = {}
        self._active: dict[str, dict] = {}
        self._size_cache: tuple[float, int] = (0.0, 0)
        self._clip_active: set[str] = set()
        self._last_cleanup = 0.0
        self._last_error = ""
        self._segments_done = 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        REKAMAN_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _set_error(self, msg: str) -> None:
        self._last_error = redact_sensitive_text(msg)[:300]

    # ---------- pengaturan ----------
    def _load_settings(self) -> dict:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if SETTINGS_FILE.exists():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for key in DEFAULT_SETTINGS:
                        if key in saved:
                            settings[key] = saved[key]
            except Exception:
                pass
        cams = settings.get("continuous_cameras") or []
        migrated = list(dict.fromkeys(normalize_key(k) for k in cams))
        if migrated != cams:
            settings["continuous_cameras"] = migrated
            try:
                write_private(SETTINGS_FILE, json.dumps(settings, indent=2, ensure_ascii=False))
            except OSError:
                pass
        elif SETTINGS_FILE.exists():
            try:
                os.chmod(str(SETTINGS_FILE), 0o600)
            except OSError:
                pass
        return settings

    def _save_settings(self) -> None:
        write_private(SETTINGS_FILE, json.dumps(self._settings, indent=2, ensure_ascii=False))

    def public_settings(self) -> dict:
        with self._lock:
            s = dict(self._settings)
        return {
            "record_enabled": bool(s.get("record_enabled")),
            "continuous_cameras": list(s.get("continuous_cameras") or []),
            "segment_minutes": s.get("segment_minutes"),
            "clip_on_detect": bool(s.get("clip_on_detect")),
            "clip_seconds": s.get("clip_seconds"),
            "retention_days": s.get("retention_days"),
            "quota_gb": s.get("quota_gb"),
            "max_concurrent_recordings": LOAD_GUARD.max_recordings,
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
        }

    def update_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Payload pengaturan tidak valid")
        with self._lock:
            s = self._settings
            if "continuous_cameras" in payload and isinstance(payload["continuous_cameras"], list):
                cams = list(dict.fromkeys(normalize_key(str(k)) for k in payload["continuous_cameras"] if str(k).strip()))
                if len(cams) > LOAD_GUARD.max_recordings:
                    raise ValueError(
                        f"Maksimal {LOAD_GUARD.max_recordings} kamera rekaman berkelanjutan bersamaan "
                        "(sesuaikan CCTIPI_MAX_CONCURRENT_RECORDINGS bila diperlukan)."
                    )
                s["continuous_cameras"] = cams
            if "record_enabled" in payload:
                s["record_enabled"] = bool(payload["record_enabled"])
            if "segment_minutes" in payload:
                s["segment_minutes"] = min(60, max(1, int(payload["segment_minutes"])))
            if "clip_on_detect" in payload:
                s["clip_on_detect"] = bool(payload["clip_on_detect"])
            if "clip_seconds" in payload:
                s["clip_seconds"] = min(600, max(5, int(payload["clip_seconds"])))
            if "retention_days" in payload:
                s["retention_days"] = min(365, max(1, int(payload["retention_days"])))
            if "quota_gb" in payload:
                s["quota_gb"] = min(10000, max(1, int(payload["quota_gb"])))
            self._save_settings()
        return self.public_settings()

    # ---------- basis data rekaman ----------
    def _db(self):
        return closing_sqlite(DB_FILE, timeout=10)

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    duration_s REAL DEFAULT 0,
                    size_bytes INTEGER DEFAULT 0,
                    path TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'berkelanjutan'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recordings_date ON recordings(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recordings_ts ON recordings(started_at)")
            for (old,) in conn.execute(
                "SELECT DISTINCT camera_id FROM recordings WHERE camera_id LIKE 'u:%?%'"
            ).fetchall():
                new = normalize_key(old)
                if new != old:
                    conn.execute("UPDATE recordings SET camera_id=? WHERE camera_id=?", (new, old))

    def _insert_recording(self, rec: dict) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO recordings "
                "(id, camera_id, camera_name, started_at, date, duration_s, "
                "size_bytes, path, mode) VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["camera_id"], rec["camera_name"],
                 rec["started_at"], rec["date"], rec["duration_s"],
                 rec["size_bytes"], rec["path"], rec["mode"]),
            )

    def list_recordings(self, date: str = "", camera: str = "",
                        mode: str = "", limit: int = 200,
                        allowed_keys: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM recordings WHERE 1=1"
        args: list = []
        if allowed_keys is not None:
            sql += " AND camera_id IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(list(allowed_keys)))
        if date:
            sql += " AND date = ?"
            args.append(date)
        if camera:
            sql += " AND camera_id = ?"
            args.append(camera)
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(min(1000, max(1, int(limit))))
        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["file_url"] = f"/api/rekaman/{item['id']}/file"
            out.append(item)
        return out

    def get_recording(self, rec_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rec_id or ""):
            return None
        with self._db() as conn:
            row = conn.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        return dict(row) if row else None

    def find_at(self, camera: str, ts: float) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE camera_id = ? AND started_at <= ? "
                "AND started_at + MAX(duration_s, 1) >= ? ORDER BY (mode = 'deteksi') DESC, started_at DESC LIMIT 1",
                (camera, int(ts) + 5, int(ts)),
            ).fetchall()
        if rows:
            rec = dict(rows[0])
            rec["file_url"] = f"/api/rekaman/{rec['id']}/file"
            return {"found": True, "recording": rec, "offset": max(0.0, round(ts - rec["started_at"] - 3, 1))}
        with self._lock:
            active = [a for a in self._active.values() if a.get("camera_id") == camera and a.get("since", 0) <= ts + 5]
        if active:
            return {"found": False, "pending": True,
                    "message": "Segmen rekaman untuk waktu ini masih direkam — bisa diputar setelah segmen selesai."}
        return {"found": False, "pending": False,
                "message": "Tidak ada rekaman server pada waktu ini (kamera tidak direkam atau sudah terhapus retensi)."}

    def get_file_path(self, rec_id: str) -> Path | None:
        rec = self.get_recording(rec_id)
        if rec is None:
            return None
        path = (DATA_DIR / rec["path"]).resolve()
        if REKAMAN_DIR.resolve() not in path.parents or not path.exists():
            return None
        return path

    def delete_recording(self, rec_id: str) -> bool:
        return self._delete_recording(rec_id) is not None

    def _delete_recording(self, rec_id: str) -> int | None:
        rec = self.get_recording(rec_id)
        if rec is None:
            return None
        with self._db() as conn:
            cur = conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
            deleted = cur.rowcount > 0
        if not deleted:
            return None
        freed = 0
        try:
            path = DATA_DIR / rec["path"]
            if path.exists():
                freed = path.stat().st_size
                path.unlink()
        except Exception:
            pass
        with self._lock:
            ts, size = self._size_cache
            if ts:
                self._size_cache = (ts, max(0, size - freed))
        return freed

    # ---------- utilitas disk & kuota ----------
    def _folder_size(self, fresh: bool = False) -> int:
        with self._lock:
            ts, size = self._size_cache
        if not fresh and ts and time.time() - ts < _SIZE_CACHE_TTL:
            return size
        size = self._scan_folder_size()
        with self._lock:
            self._size_cache = (time.time(), size)
        return size

    def _scan_folder_size(self) -> int:
        total = 0
        if REKAMAN_DIR.exists():
            for p in REKAMAN_DIR.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except Exception:
                        pass
        return total

    def _ensure_disk_space(self) -> bool:
        """Pastikan sisa ruang disk mencukupi; jalankan cleanup bila menipis."""
        try:
            free = shutil.disk_usage(str(DATA_DIR)).free
        except OSError:
            return True
        if free >= MIN_FREE_DISK_BYTES:
            return True
        try:
            self.cleanup_now()
            free = shutil.disk_usage(str(DATA_DIR)).free
        except Exception:
            pass
        if free < MIN_FREE_DISK_BYTES:
            self._set_error(
                f"Penyimpanan disk hampir habis (sisa {free // 1_048_576} MB < {MIN_FREE_DISK_BYTES // 1_048_576} MB); perekaman dijeda."
            )
            log_event("ERROR", "recorder", self._last_error)
            return False
        return True

    def cleanup_now(self) -> dict:
        """Terapkan retensi + kuota sekarang. Mengembalikan ringkasan."""
        with self._lock:
            retention = int(self._settings.get("retention_days") or 7)
            quota_bytes = int(float(self._settings.get("quota_gb") or 20) * (1024 ** 3))
        removed_retention = 0
        removed_quota = 0
        cutoff = int(time.time() - retention * 86400)
        with self._db() as conn:
            old = conn.execute("SELECT id, path FROM recordings WHERE started_at < ?", (cutoff,)).fetchall()
        for row in old:
            if self._delete_recording(row["id"]) is not None:
                removed_retention += 1

        total = self._folder_size(fresh=True)
        if total > quota_bytes:
            with self._db() as conn:
                oldest = conn.execute("SELECT id FROM recordings ORDER BY started_at ASC").fetchall()
            for row in oldest:
                if total <= quota_bytes:
                    break
                freed = self._delete_recording(row["id"])
                if freed is None:
                    continue
                total -= freed
                removed_quota += 1

        if REKAMAN_DIR.exists():
            for sub in sorted(REKAMAN_DIR.rglob("*"), reverse=True):
                if sub.is_dir() and not any(sub.iterdir()):
                    try:
                        sub.rmdir()
                    except Exception:
                        pass
        self._last_cleanup = time.time()
        return {
            "retention_removed": removed_retention,
            "quota_removed": removed_quota,
            "total_size_bytes": self._folder_size(fresh=True),
        }

    # ---------- perekaman ffmpeg ----------
    def _input_args(self, cam: dict) -> list[str] | None:
        url = (cam.get("url") or "").strip()
        if not url or cam_type(cam) == "youtube" or not ffmpeg_input_allowed(url):
            return None
        args: list[str] = ffmpeg_timeout_args(url)
        if url.startswith("rtsp://"):
            args += ["-rtsp_transport", "tcp"]
        elif url.startswith(("http://", "https://")):
            hdrs = _http_headers_for(url)
            header_blob = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
            args += ["-headers", header_blob]
        args += ["-i", url]
        return args

    def _run_ffmpeg_record(self, cam: dict, out_path: Path, seconds: int, tag: str) -> tuple[bool, str]:
        """Rekam satu berkas MP4 selama `seconds` dengan batas slot ffmpeg & pemulihan error."""
        if not self._ensure_disk_space():
            return False, "disk penuh"
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return False, "ffmpeg tidak ditemukan di sistem"
        input_args = self._input_args(cam)
        if input_args is None:
            return False, "stream tidak didukung untuk direkam (YouTube / URL kosong)"

        try:
            LOAD_GUARD.acquire_ffmpeg("recorder")
        except CapacityError as exc:
            return False, str(exc)

        try:
            attempts = [
                ["-c", "copy"],
                ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"],
            ]
            last_err = ""
            for idx, codec_args in enumerate(attempts):
                cmd = [
                    ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
                    *input_args, "-t", str(int(seconds)), *codec_args,
                    "-movflags", "+faststart", "-y", str(out_path),
                ]
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except OSError as exc:
                    out_path.unlink(missing_ok=True)
                    if _is_disk_full_error(exc):
                        try:
                            self.cleanup_now()
                        except Exception:
                            pass
                        return False, "disk penuh saat menjalankan ffmpeg"
                    return False, redact_sensitive_text(f"gagal menjalankan ffmpeg: {exc}")

                with self._lock:
                    self._procs[tag] = proc
                try:
                    try:
                        _, stderr = proc.communicate(timeout=int(seconds) + _WATCHDOG_GRACE)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        _, stderr = proc.communicate()
                        stderr = (stderr or b"") + b"\nwatchdog: ffmpeg melewati batas waktu"
                finally:
                    with self._lock:
                        self._procs.pop(tag, None)

                if self._stop.is_set():
                    out_path.unlink(missing_ok=True)
                    return False, "dihentikan"

                size = out_path.stat().st_size if out_path.exists() else 0
                if proc.returncode == 0 and size >= _MIN_SEGMENT_BYTES:
                    return True, "copy" if idx == 0 else "re-encode"

                raw_err = (stderr or b"").decode("utf-8", "replace").strip()
                last_err = redact_sensitive_text(raw_err)[-300:]
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                if _is_disk_full_error(raw_err):
                    try:
                        self.cleanup_now()
                    except Exception:
                        pass
                    return False, "disk penuh saat merekam"
            return False, last_err or "ffmpeg gagal tanpa pesan"
        finally:
            LOAD_GUARD.release_ffmpeg("recorder")

    def _finalize(self, cam: dict, out_path: Path, rel_path: str, started_at: float, mode: str) -> dict | None:
        duration = max(0.0, _probe_duration(out_path))
        rec = {
            "id": f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(started_at))}-{uuid.uuid4().hex[:6]}",
            "camera_id": camera_key(cam),
            "camera_name": cam.get("name") or camera_key(cam),
            "started_at": int(started_at),
            "date": time.strftime("%Y-%m-%d", time.localtime(started_at)),
            "duration_s": round(duration, 2),
            "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
            "path": rel_path,
            "mode": mode,
        }
        try:
            self._insert_recording(rec)
            # Pastikan kuota dijaga setiap kali segmen baru selesai ditulis
            if self._folder_size(fresh=True) > int(float(self._settings.get("quota_gb") or 20) * (1024 ** 3)):
                self.cleanup_now()
            return rec
        except (OSError, sqlite3.Error) as exc:
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            if _is_disk_full_error(exc):
                try:
                    self.cleanup_now()
                except Exception:
                    pass
            self._set_error(f"Gagal menyimpan indeks rekaman: {exc}")
            return None

    def _segment_loop(self, key: str) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    s = dict(self._settings)
                if not s.get("record_enabled") or key not in (s.get("continuous_cameras") or []):
                    break
                cam = self._resolve_camera(key)
                if cam is None:
                    self._stop.wait(10)
                    continue
                if cam_type(cam) == "youtube":
                    self._set_error(f"Kamera {cam.get('name')}: stream YouTube tidak bisa direkam server.")
                    break
                seg_seconds = int(s.get("segment_minutes") or 5) * 60
                started = time.time()
                date_str = time.strftime("%Y-%m-%d", time.localtime(started))
                day_dir = REKAMAN_DIR / _cam_dir_name(key) / date_str
                try:
                    day_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    self._set_error(f"Gagal membuat folder rekaman: {exc}")
                    if _is_disk_full_error(exc):
                        try:
                            self.cleanup_now()
                        except Exception:
                            pass
                    self._stop.wait(10)
                    continue

                fname = time.strftime("%H%M%S", time.localtime(started)) + ".mp4"
                out_path = day_dir / fname
                if out_path.exists():
                    out_path = day_dir / f"{time.strftime('%H%M%S', time.localtime(started))}-{uuid.uuid4().hex[:4]}.mp4"
                rel_path = f"rekaman/{_cam_dir_name(key)}/{date_str}/{out_path.name}"
                with self._lock:
                    self._active[f"cont:{key}"] = {
                        "camera_id": key,
                        "camera_name": cam.get("name") or key,
                        "mode": MODE_CONTINUOUS,
                        "since": int(started),
                    }
                ok, info = self._run_ffmpeg_record(cam, out_path, seg_seconds, tag=f"cont:{key}")
                with self._lock:
                    self._active.pop(f"cont:{key}", None)
                if ok:
                    if self._finalize(cam, out_path, rel_path, started, MODE_CONTINUOUS) is not None:
                        self._segments_done += 1
                else:
                    try:
                        out_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if info != "dihentikan":
                        self._set_error(f"Rekam {cam.get('name')}: {info}")
                        self._stop.wait(5)
        finally:
            with self._lock:
                self._cam_threads.pop(key, None)
                self._active.pop(f"cont:{key}", None)

    def _resolve_camera(self, key: str) -> dict | None:
        try:
            all_cams = self._get_cameras().get("cameras", [])
        except Exception as exc:
            self._set_error(f"Gagal membaca daftar kamera: {exc}")
            return None
        for cam in all_cams:
            if camera_key(cam) == key:
                return cam
        return None

    def trigger_clip(self, cam: dict) -> bool:
        try:
            with self._lock:
                s = dict(self._settings)
                if not s.get("record_enabled") or not s.get("clip_on_detect"):
                    return False
                key = camera_key(cam)
                if key in self._clip_active:
                    return False
                if len(self._active) >= LOAD_GUARD.max_recordings:
                    self._set_error(f"Klip dilewati: batas rekaman bersamaan penuh ({LOAD_GUARD.max_recordings}).")
                    return False
                self._clip_active.add(key)
                clip_seconds = int(s.get("clip_seconds") or 30)
            if cam_type(cam) == "youtube":
                with self._lock:
                    self._clip_active.discard(key)
                return False
            t = threading.Thread(
                target=self._clip_run, args=(cam, key, clip_seconds),
                name=f"cctipi-clip-{key[:16]}", daemon=True,
            )
            t.start()
            return True
        except Exception as exc:
            self._set_error(f"Klip deteksi: {exc.__class__.__name__}: {exc}")
            return False

    def _clip_run(self, cam: dict, key: str, clip_seconds: int) -> None:
        started = time.time()
        try:
            date_str = time.strftime("%Y-%m-%d", time.localtime(started))
            day_dir = REKAMAN_DIR / _cam_dir_name(key) / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            fname = "klip-" + time.strftime("%H%M%S", time.localtime(started)) + ".mp4"
            out_path = day_dir / fname
            if out_path.exists():
                out_path = day_dir / f"klip-{time.strftime('%H%M%S', time.localtime(started))}-{uuid.uuid4().hex[:4]}.mp4"
            rel_path = f"rekaman/{_cam_dir_name(key)}/{date_str}/{out_path.name}"
            with self._lock:
                self._active[f"clip:{key}"] = {
                    "camera_id": key,
                    "camera_name": cam.get("name") or key,
                    "mode": MODE_DETECTION,
                    "since": int(started),
                }
            ok, info = self._run_ffmpeg_record(cam, out_path, clip_seconds, tag=f"clip:{key}")
            if ok:
                self._finalize(cam, out_path, rel_path, started, MODE_DETECTION)
            else:
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                if info != "dihentikan":
                    self._set_error(f"Klip {cam.get('name')}: {info}")
        except Exception as exc:
            self._set_error(f"Klip deteksi: {exc.__class__.__name__}: {exc}")
        finally:
            with self._lock:
                self._clip_active.discard(key)
                self._active.pop(f"clip:{key}", None)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._manager_loop, name="cctipi-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_all_procs()

    def shutdown(self) -> None:
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)

    def _kill_all_procs(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _manager_loop(self) -> None:
        try:
            self.cleanup_now()
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                with self._lock:
                    s = dict(self._settings)
                wanted: set[str] = set()
                if s.get("record_enabled"):
                    wanted = set(list(s.get("continuous_cameras") or [])[: LOAD_GUARD.max_recordings])
                with self._lock:
                    running = set(self._cam_threads)
                for key in wanted - running:
                    t = threading.Thread(
                        target=self._segment_loop, args=(key,),
                        name=f"cctipi-rec-{key[:16]}", daemon=True,
                    )
                    with self._lock:
                        self._cam_threads[key] = t
                    t.start()
                if time.time() - self._last_cleanup > _CLEANUP_INTERVAL:
                    self.cleanup_now()
                self._stop.wait(3)
            except Exception as exc:
                self._set_error(f"Recorder: {exc.__class__.__name__}: {exc}")
                self._stop.wait(3)

    def status(self) -> dict:
        with self._lock:
            s = dict(self._settings)
            active = list(self._active.values())
            procs_running = sum(1 for p in self._procs.values() if p.poll() is None)
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()
        return {
            "available": shutil.which("ffmpeg") is not None,
            "enabled": bool(s.get("record_enabled")),
            "running": bool(s.get("record_enabled")) and bool(self._thread and self._thread.is_alive()),
            "recording": active,
            "ffmpeg_processes": procs_running,
            "max_concurrent_recordings": LOAD_GUARD.max_recordings,
            "continuous_cameras": len(s.get("continuous_cameras") or []),
            "recordings_count": int(row["n"]) if row else 0,
            "total_size_bytes": self._folder_size(),
            "quota_bytes": int(float(s.get("quota_gb") or 20) * (1024 ** 3)),
            "retention_days": s.get("retention_days"),
            "segments_completed": self._segments_done,
            "last_error": self._last_error,
        }

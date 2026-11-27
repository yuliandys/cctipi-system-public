#!/usr/bin/env python3
"""CCTipi System v1.1 — Mode Live AI realtime (PILOT, maks 1 kamera)."""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import threading
import time

import ai as _ai
from storage_utils import redact_sensitive_text
from system_health import LOAD_GUARD, CapacityError

try:
    import cv2
    import numpy as np

    _CV_OK = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _CV_OK = False

AUTO_STOP_SECONDS = int(os.environ.get("CCTIPI_LIVEAI_AUTOSTOP_SECONDS", "600"))
NO_VIEWER_SECONDS = int(os.environ.get("CCTIPI_LIVEAI_NO_VIEWER_SECONDS", "45"))
STALL_SECONDS = 20
DECODE_WIDTH = 640
READER_FPS = 15
DISPLAY_FPS = 12.0
INFER_MAX_FPS = 5.0
FORCE_INFER_SECONDS = 2.0
JPEG_QUALITY = 70
MOTION_SIZE = (320, 180)
MOTION_PIXEL_DELTA = 20
MOTION_MIN_PIXELS = 24
UNSUPPORTED_TYPES = {
    "youtube": "YouTube tidak didukung Live AI.",
    "http": "Kamera snapshot (gambar diam per permintaan) tidak didukung Live AI — gunakan HLS, MJPEG, atau RTSP.",
}


def motion_pixels(prev_gray, gray) -> int:
    diff = cv2.absdiff(prev_gray, gray)
    return int(np.count_nonzero(diff > MOTION_PIXEL_DELTA))


def gate_image(frame):
    small = cv2.resize(frame, MOTION_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (5, 5), 0)


class _StreamReader:
    """Pembaca stream persisten via ffmpeg rawvideo pipe dengan jaminan pelepasan proses."""

    def __init__(self, cam: dict, width: int, height: int):
        self._cam = cam
        self.width = width
        self.height = height
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        self.last_frame_at = 0.0
        self.ended = False
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=5)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._slot_held = False
        self.error = ""

    def _ffmpeg_cmd(self) -> list[str] | None:
        url = (self._cam.get("url") or "").strip()
        ffmpeg_bin = shutil.which("ffmpeg")
        if not url or not ffmpeg_bin or not _ai.ffmpeg_input_allowed(url):
            return None
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin"]
        cmd += _ai.ffmpeg_timeout_args(url)
        if url.startswith(("rtsp://", "rtsps://")):
            cmd += ["-rtsp_transport", "tcp"]
        elif url.startswith(("http://", "https://")):
            header_blob = "".join(f"{k}: {v}\r\n" for k, v in _ai._http_headers_for(url).items())
            if header_blob:
                cmd += ["-headers", header_blob]
        if _ai.cam_type(self._cam) == "hls" or ".m3u8" in url.lower():
            cmd += ["-re"]
        filt = f"fps={READER_FPS},scale={self.width}:{self.height}"
        cmd += ["-i", url, "-an", "-vf", filt, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        return cmd

    def start(self) -> bool:
        cmd = self._ffmpeg_cmd()
        if cmd is None:
            self.error = "ffmpeg tidak ditemukan atau URL kamera kosong"
            return False
        LOAD_GUARD.acquire_ffmpeg("live_ai")
        self._slot_held = True
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            self._slot_held = False
            LOAD_GUARD.release_ffmpeg("live_ai")
            self.error = redact_sensitive_text(f"Gagal menjalankan ffmpeg: {exc}")
            return False
        self.last_frame_at = time.time()
        threading.Thread(target=self._read_loop, name="cctipi-liveai-reader", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name="cctipi-liveai-stderr", daemon=True).start()
        return True

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = redact_sensitive_text(line.decode("utf-8", "replace").strip())
                if text:
                    self._stderr_tail.append(text)
        except Exception:
            pass

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self.ended = True
            return
        w, h = self.width, self.height
        frame_bytes = w * h * 3
        buf = bytearray()
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= frame_bytes:
                    frame = np.frombuffer(bytes(buf[:frame_bytes]), dtype=np.uint8).reshape((h, w, 3)).copy()
                    del buf[:frame_bytes]
                    with self._lock:
                        self._frame = frame
                        self._seq += 1
                        self.last_frame_at = time.time()
        except Exception as exc:
            self._stderr_tail.append(redact_sensitive_text(f"{exc.__class__.__name__}: {exc}"))
        finally:
            self.ended = True

    def end_reason(self) -> str:
        detail = self._stderr_tail[-1] if self._stderr_tail else ""
        return redact_sensitive_text(detail)[:200]

    def latest(self) -> tuple[object, int]:
        with self._lock:
            return self._frame, self._seq

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
            self._proc = None
            held = self._slot_held
            self._slot_held = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            finally:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except Exception:
                            pass
        if held:
            LOAD_GUARD.release_ffmpeg("live_ai")


class LiveAiService:
    """Mesin Live AI: 1 sesi pada satu waktu, MJPEG beranotasi + auto-stop ketat."""

    def __init__(self, get_cameras_fn, ai_service=None):
        self._get_cameras = get_cameras_fn
        self._ai = ai_service
        self._lock = threading.Lock()
        self._session: dict | None = None
        self._starting: str | None = None
        self._last_end: dict | None = None

    def available(self) -> tuple[bool, str]:
        if not _CV_OK:
            return False, "OpenCV/NumPy tidak terpasang (jalankan dengan .venv/bin/python)."
        if self._ai is None:
            return False, "Modul AI (ai.py) tidak termuat."
        if not _ai.AI_AVAILABLE:
            return False, _ai._UNAVAILABLE_REASON or "AI tidak tersedia."
        if shutil.which("ffmpeg") is None:
            return False, "ffmpeg tidak ditemukan di PATH."
        return True, ""

    def _find_camera(self, key: str) -> dict | None:
        try:
            cams = self._get_cameras().get("cameras", [])
        except Exception:
            return None
        for cam in cams:
            if _ai.camera_key(cam) == key:
                return cam
        return None

    def _probe_frame_size(self, cam: dict) -> tuple[int, int] | None:
        data = _ai.grab_frame(cam)
        if not data:
            return None
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        return w, h

    @staticmethod
    def _window_fps(timestamps: collections.deque, now: float, span: float = 5.0) -> float:
        recent = [t for t in list(timestamps) if now - t <= span]
        if len(recent) < 2:
            return 0.0
        elapsed = recent[-1] - recent[0]
        return round((len(recent) - 1) / elapsed, 2) if elapsed > 0 else 0.0

    def start(self, key: str) -> tuple[bool, str]:
        ok, reason = self.available()
        if not ok:
            return False, reason
        cam = self._find_camera(key)
        if cam is None:
            return False, "Kamera tidak ditemukan."
        if not (cam.get("url") or "").strip():
            return False, "Kamera ini tidak punya URL stream."
        ctype = _ai.cam_type(cam)
        if ctype in UNSUPPORTED_TYPES:
            return False, UNSUPPORTED_TYPES[ctype]

        with self._lock:
            if self._session is not None:
                if self._session["key"] == key:
                    return True, "Live AI sudah berjalan untuk kamera ini."
                return False, self._busy_message(self._session["cam"].get("name") or self._session["key"])
            if self._starting is not None:
                if self._starting == key:
                    return False, "Live AI kamera ini sedang disiapkan — tunggu sebentar."
                return False, "Live AI kamera lain sedang disiapkan — coba lagi sebentar."
            self._starting = key

        reader: _StreamReader | None = None
        try:
            size = self._probe_frame_size(cam)
            if size is None:
                return False, "Tidak bisa mengambil frame dari kamera (stream tidak terjangkau)."
            w, h = size
            if w > DECODE_WIDTH:
                w, h = DECODE_WIDTH, max(2, int(h * DECODE_WIDTH / w) // 2 * 2)
            reader = _StreamReader(cam, w, h)
            if not reader.start():
                return False, reader.error or "Gagal membuka stream kamera."
            now = time.time()
            session = {
                "key": key, "cam": cam, "reader": reader,
                "stop": threading.Event(), "started_at": now, "thread": None,
                "detections": [], "det_version": 0,
                "last_detection_at": 0.0, "last_infer_at": 0.0,
                "last_viewer_at": now, "viewers": 0,
                "frames_read": 0, "frames_inferred": 0, "frames_skipped_motion": 0,
                "frames_displayed": 0, "snapshots_saved": 0, "last_error": "",
                "_display_window": collections.deque(maxlen=200),
                "_detect_window": collections.deque(maxlen=100),
            }
            thread = threading.Thread(
                target=self._run_inference, args=(session,),
                name="cctipi-liveai-infer", daemon=True,
            )
            session["thread"] = thread
            with self._lock:
                self._session = session
                self._last_end = None
            thread.start()
            reader = None  # kepemilikan pindah ke session
            return True, "Live AI dimulai."
        except CapacityError:
            raise
        finally:
            if reader is not None:
                reader.stop()
            with self._lock:
                self._starting = None

    @staticmethod
    def _busy_message(name: str) -> str:
        return (f"Live AI pilot hanya untuk 1 kamera — matikan dulu kamera \"{name}\" "
                "sebelum menyalakan yang lain.")

    def stop(self, key: str | None = None, reason: str = "Dimatikan.") -> bool:
        with self._lock:
            session = self._session
            if session is None or (key is not None and session["key"] != key):
                return False
            self._session = None
            self._last_end = {"key": session["key"], "reason": redact_sensitive_text(reason), "at": time.time()}
        self._teardown(session)
        return True

    def shutdown(self) -> None:
        self.stop(reason="Server berhenti.")

    def _teardown(self, session: dict) -> None:
        session["stop"].set()
        try:
            session["reader"].stop()
        except Exception:
            pass
        thread = session.get("thread")
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _end_reason(self, session: dict, now: float) -> str | None:
        reader: _StreamReader = session["reader"]
        auto_stop = AUTO_STOP_SECONDS
        no_viewer = NO_VIEWER_SECONDS
        if now - session["started_at"] >= auto_stop:
            return f"Auto-stop setelah {max(1, auto_stop // 60)} menit."
        if reader.ended:
            detail = reader.end_reason()
            return "Stream kamera terputus" + (f": {detail}" if detail else ".")
        if now - reader.last_frame_at > STALL_SECONDS:
            return f"Stream kamera tidak mengirim gambar selama {STALL_SECONDS} detik."
        if session.get("viewers", 0) <= 0 and now - session["last_viewer_at"] >= no_viewer:
            return f"Tidak ada yang menonton selama {no_viewer} detik."
        return None

    def _run_inference(self, session: dict) -> None:
        reader: _StreamReader = session["reader"]
        stop_event: threading.Event = session["stop"]
        last_seq = -1
        last_infer = 0.0
        prev_gray = None
        reason = None
        try:
            while not stop_event.is_set():
                now = time.time()
                reason = self._end_reason(session, now)
                if reason:
                    break
                frame, seq = reader.latest()
                if frame is None or seq == last_seq:
                    stop_event.wait(0.02)
                    continue
                last_seq = seq
                session["frames_read"] += 1
                if now - last_infer < (1.0 / INFER_MAX_FPS):
                    continue
                gray = gate_image(frame)
                forced = now - last_infer >= FORCE_INFER_SECONDS
                moved = prev_gray is None or motion_pixels(prev_gray, gray) >= MOTION_MIN_PIXELS
                prev_gray = gray
                if not (moved or forced):
                    session["frames_skipped_motion"] += 1
                    continue
                last_infer = now
                detections = self._ai.detect_on_image(frame)
                done = time.time()
                session["frames_inferred"] += 1
                session["last_infer_at"] = done
                session["_detect_window"].append(done)
                session["detections"] = detections
                session["det_version"] += 1
                if detections:
                    session["last_detection_at"] = done
                    try:
                        if self._ai.offer_live_snapshot(session["cam"], detections, frame):
                            session["snapshots_saved"] += 1
                    except Exception as exc:
                        session["last_error"] = f"Jepretan: {exc.__class__.__name__}"
        except Exception as exc:
            reason = redact_sensitive_text(f"Live AI error: {exc.__class__.__name__}: {exc}")
        finally:
            with self._lock:
                if self._session is session:
                    self._session = None
                    self._last_end = {
                        "key": session["key"],
                        "reason": redact_sensitive_text(reason or "Dimatikan."),
                        "at": time.time(),
                    }
            session["stop"].set()
            try:
                reader.stop()
            except Exception:
                pass

    def status(self, key: str | None = None) -> dict:
        ok, reason = self.available()
        with self._lock:
            session = self._session
            starting = self._starting
            last_end = self._last_end
        base = {
            "available": ok,
            "unavailable_reason": reason,
            "active": False,
            "starting": bool(starting and (key is None or starting == key)),
            "max_cameras": 1,
            "auto_stop_seconds": AUTO_STOP_SECONDS,
            "no_viewer_seconds": NO_VIEWER_SECONDS,
            "target_fps": DISPLAY_FPS,
            "display_target_fps": DISPLAY_FPS,
            "infer_max_fps": INFER_MAX_FPS,
            "decode_width": DECODE_WIDTH,
        }
        if session is None or (key is not None and session["key"] != key):
            if last_end and (key is None or last_end["key"] == key):
                base["ended_reason"] = last_end["reason"]
                base["ended_at"] = int(last_end["at"])
            return base
        now = time.time()
        display_fps = self._window_fps(session["_display_window"], now)
        detect_fps = self._window_fps(session["_detect_window"], now)
        base.update({
            "active": True,
            "camera_key": session["key"],
            "camera_name": session["cam"].get("name") or session["key"],
            "viewers": session.get("viewers", 0),
            "started_at": int(session["started_at"]),
            "elapsed_seconds": int(now - session["started_at"]),
            "remaining_seconds": max(0, int(AUTO_STOP_SECONDS - (now - session["started_at"]))),
            "fps": detect_fps,
            "display_fps": display_fps,
            "detect_fps": detect_fps,
            "frames_read": session["frames_read"],
            "frames_inferred": session["frames_inferred"],
            "frames_skipped_motion": session["frames_skipped_motion"],
            "frames_displayed": session["frames_displayed"],
            "snapshots_saved": session["snapshots_saved"],
            "detections": session["detections"],
            "detection_count": len(session["detections"]),
            "last_detection_at": int(session["last_detection_at"]) or None,
            "last_error": redact_sensitive_text(session["last_error"]),
            "stream_url": f"/api/live-ai/{session['key']}.mjpg",
        })
        return base

    def mjpeg_frames(self, key: str):
        with self._lock:
            session = self._session
            if session is None or session["key"] != key:
                return
            session["viewers"] = session.get("viewers", 0) + 1
            session["last_viewer_at"] = time.time()
        reader = session["reader"]
        tick = 1.0 / DISPLAY_FPS
        last_key = None
        last_jpeg: bytes | None = None
        next_at = time.monotonic()
        try:
            while True:
                with self._lock:
                    if self._session is not session:
                        return
                if session["stop"].is_set():
                    return
                next_at += tick
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()
                session["last_viewer_at"] = time.time()
                frame, seq = reader.latest()
                if frame is None:
                    continue
                cache_key = (seq, session["det_version"])
                if cache_key == last_key and last_jpeg is not None:
                    data = last_jpeg
                else:
                    detections = session["detections"]
                    img = _ai.draw_detections(frame, detections) if detections else frame
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                    if not ok:
                        continue
                    data = buf.tobytes()
                    last_key, last_jpeg = cache_key, data
                session["frames_displayed"] += 1
                session["_display_window"].append(time.time())
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n"
                )
        finally:
            with self._lock:
                session["viewers"] = max(0, session.get("viewers", 1) - 1)
                session["last_viewer_at"] = time.time()

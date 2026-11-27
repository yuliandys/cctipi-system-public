#!/usr/bin/env python3
"""CCTipi System v1.0 — Modul AI Jepretan + notifikasi Telegram.

Prinsip privasi (PENTING):
- Deteksi TIDAK PERNAH berjalan untuk kamera yang tidak dipilih (opt-in)
  oleh pengguna. Daftar opt-in bawaan KOSONG — tidak ada kamera yang
  dipantau sebelum pengguna memilihnya secara eksplisit di Pengaturan.
- Semua pemrosesan lokal di mesin ini. Satu-satunya pengiriman keluar
  adalah foto jepretan ke Telegram, dan itu hanya bila pengguna
  mengaktifkan notifikasi + mengisi token bot miliknya sendiri.

Degradasi anggun: bila dependensi AI (onnxruntime / opencv / numpy) atau
berkas model tidak tersedia, modul ini tetap termuat, status melaporkan
"AI tidak tersedia", dan seluruh fungsi dasar CCTipi System tetap berjalan.
Jalankan server dengan interpreter venv untuk mengaktifkan AI:
    .venv/bin/python server.py
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import net_guard
from storage_utils import closing_sqlite, redact_sensitive_text, write_private
from system_health import LOAD_GUARD, CapacityError

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("CCTIPI_STATE_DIR") or BASE_DIR).resolve()  # data runtime & rahasia
DATA_DIR = STATE_DIR / "data"
JEPRETAN_DIR = DATA_DIR / "jepretan"
DB_FILE = DATA_DIR / "cctipi.db"
SETTINGS_FILE = DATA_DIR / "ai_settings.json"
ENHANCED_DIR = DATA_DIR / "jepretan-enhanced"
MODEL_FILE = BASE_DIR / "models" / "yolo11n.onnx"
MODEL_NAME = "YOLO11n (ONNX, CPU)"
MODEL_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"
)
# SHA-256 resmi rilis Ultralytics assets v8.3.0/yolo11n.onnx (10.930.182 byte).
# Set CCTIPI_MODEL_SHA256=<hex64> untuk model kustom, atau 'skip' untuk melewati cek.
MODEL_SHA256 = os.environ.get(
    "CCTIPI_MODEL_SHA256",
    "634279b40c07c6391472c51ad45b81ebc48706a9a1fe72dd3396322acd0c053b",
).strip().lower()


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def verify_model_checksum(path: Path | None = None, expected_sha256: str | None = None) -> tuple[bool, str]:
    """Verifikasi SHA-256 berkas model YOLO11n ONNX."""
    target = path if path is not None else MODEL_FILE
    exp = (expected_sha256 if expected_sha256 is not None else MODEL_SHA256).strip().lower()
    if not target.is_file():
        return False, f"Berkas model tidak ditemukan: {target.name}"
    if exp in ("", "skip"):
        return True, "verifikasi dilewati"
    actual = compute_sha256(target)
    if actual != exp:
        return False, f"Checksum SHA-256 model tidak cocok (dapat {actual[:12]}…, wajib {exp[:12]}…)."
    return True, actual


def download_model(
    dest: Path | None = None,
    url: str = MODEL_URL,
    expected_sha256: str | None = None,
) -> Path:
    """Unduh bobot resmi YOLO11n secara atomik & verifikasi SHA-256 sebelum dipakai."""
    target = dest if dest is not None else MODEL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".onnx.part")
    req = urllib.request.Request(url, headers={"User-Agent": "CCTipi-ModelDownloader/1.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    ok, detail = verify_model_checksum(tmp, expected_sha256=expected_sha256)
    if not ok:
        tmp.unlink(missing_ok=True)
        raise ValueError(detail)
    tmp.replace(target)
    return target

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Dependensi AI (opsional — degradasi anggun bila tidak ada)
# ---------------------------------------------------------------------------
_UNAVAILABLE_REASON = ""
try:
    import cv2
    import numpy as np
    import onnxruntime as ort

    try:
        import enhance as _enhance_module
    except Exception:
        _enhance_module = None

    AI_AVAILABLE = True
except Exception as exc:  # pragma: no cover - tergantung lingkungan
    cv2 = None  # type: ignore
    np = None  # type: ignore
    ort = None  # type: ignore
    _enhance_module = None
    AI_AVAILABLE = False
    _UNAVAILABLE_REASON = (
        f"Dependensi AI tidak terpasang ({exc.__class__.__name__}: {exc}). "
        "Jalankan server dengan .venv/bin/python (lihat README)."
    )

if AI_AVAILABLE and not MODEL_FILE.exists():
    AI_AVAILABLE = False
    _UNAVAILABLE_REASON = (
        f"Berkas model tidak ditemukan: {MODEL_FILE.name}. "
        f"Unduh bobot resmi YOLO11n dari {MODEL_URL} ke folder models/."
    )

# ---------------------------------------------------------------------------
# Kelas COCO (80) + pemetaan kategori CCTipi System
# ---------------------------------------------------------------------------
COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

CATEGORIES: dict[str, set[int]] = {
    "orang": {0},
    "kendaraan": {1, 2, 3, 5, 7},  # bicycle, car, motorcycle, bus, truck
    "hewan": {14, 15, 16, 17, 18, 19, 20, 21, 22, 23},
    "barang": {24, 26, 28},        # backpack, handbag, suitcase
}
CATEGORY_LABELS = {
    "orang": "Orang", "kendaraan": "Kendaraan", "hewan": "Hewan",
    "barang": "Barang bawaan",
}
# Kategori baru bawaannya mati agar pengguna lama tidak tiba-tiba
# menerima jepretan/notifikasi jenis baru setelah update.
CATEGORY_DEFAULTS = {"orang": True, "kendaraan": True, "hewan": True, "barang": False}
CLASS_CATEGORY = {
    cid: cat for cat, ids in CATEGORIES.items() for cid in ids
}
LABEL_ID = {
    "person": "Orang", "bicycle": "Sepeda", "car": "Mobil",
    "motorcycle": "Motor", "bus": "Bus", "truck": "Truk", "bird": "Burung",
    "cat": "Kucing", "dog": "Anjing", "horse": "Kuda", "sheep": "Domba",
    "cow": "Sapi", "elephant": "Gajah", "bear": "Beruang", "zebra": "Zebra",
    "giraffe": "Jerapah", "backpack": "Ransel", "handbag": "Tas",
    "suitcase": "Koper",
}
# Nama objek (COCO) yang boleh dipilih satu per satu di Pengaturan.
KNOWN_OBJECTS = {COCO_NAMES[cid] for ids in CATEGORIES.values() for cid in ids}
CATEGORY_COLORS = {  # BGR untuk OpenCV
    "orang": (64, 220, 120),
    "kendaraan": (255, 170, 40),
    "hewan": (200, 90, 230),
    "barang": (40, 140, 245),
}


def class_catalog() -> list[dict]:
    """Daftar kategori + objek untuk UI Pengaturan & filter galeri."""
    return [
        {
            "key": cat,
            "label": CATEGORY_LABELS.get(cat, cat.title()),
            "default": CATEGORY_DEFAULTS.get(cat, True),
            "objects": [
                {"name": COCO_NAMES[cid], "label": LABEL_ID.get(COCO_NAMES[cid], COCO_NAMES[cid])}
                for cid in sorted(ids)
            ],
        }
        for cat, ids in CATEGORIES.items()
    ]


def detection_enabled(det: dict, classes: dict, objects: dict | None = None) -> bool:
    """Deteksi lolos bila kategorinya aktif DAN objeknya tidak dimatikan."""
    cat = det.get("category")
    if cat not in CATEGORIES:
        return False
    if not bool(classes.get(cat, CATEGORY_DEFAULTS.get(cat, True))):
        return False
    return bool((objects or {}).get(det.get("class_name"), True))


DEFAULT_SETTINGS: dict = {
    "enabled": False,
    "interval": 10,          # detik antar siklus pemindaian
    "threshold": 0.45,       # ambang confidence
    "cooldown": 60,          # detik jeda jepretan per kamera
    "retention_days": 7,     # retensi jepretan
    "classes": dict(CATEGORY_DEFAULTS),
    "objects": {},           # {nama_objek: False} = objek dimatikan; tidak ada = aktif
    "cameras": [],           # daftar kunci kamera opt-in ("u:<url>" / "m:<manualId>")
    "telegram_enabled": False,
    "telegram_chat_id": "",
    "telegram_bot_token": "",  # rahasia — TIDAK pernah dikembalikan oleh API
    "enhance_before_detect": False,  # Fase 3: perjelas frame gelap sebelum deteksi
    "zones": {},             # v6.7: {kunci_kamera: {"polygons": [...], "schedule": {...}}}
}


# Parameter query yang berganti setiap kali sumber di-refresh (token JWT,
# tanda tangan, kedaluwarsa). Diabaikan saat membentuk kunci kamera agar
# opt-in AI / rekaman tidak "hilang" ketika token sumber diperbarui.
# HARUS sama dengan VOLATILE_PARAMS di public/app.js.
VOLATILE_PARAMS = frozenset({
    "token", "access_token", "auth", "auth_token", "authtoken", "jwt",
    "signature", "sig", "expires", "expiry", "hdnts", "wmsauthsign",
})


def normalize_stream_url(url: str) -> str:
    """Buang parameter volatil dari query string. Sengaja berbasis string
    (bukan parse+encode ulang) agar hasilnya identik dengan versi JS."""
    url = url or ""
    if "?" not in url:
        return url
    base, _, rest = url.partition("?")
    query, hash_sep, frag = rest.partition("#")
    kept = []
    for part in query.split("&"):
        if not part:
            continue
        name = urllib.parse.unquote(part.split("=", 1)[0]).lower()
        if name not in VOLATILE_PARAMS:
            kept.append(part)
    out = base + ("?" + "&".join(kept) if kept else "")
    return out + (hash_sep + frag if hash_sep else "")


def camera_key(cam: dict) -> str:
    """Kunci stabil kamera — sama dengan camKey() di frontend app.js."""
    if cam.get("manualId"):
        return f"m:{cam['manualId']}"
    return f"u:{normalize_stream_url(cam.get('url', ''))}"


def normalize_key(key: str) -> str:
    """Ubah kunci lama (URL penuh bertoken) ke bentuk stabil."""
    if isinstance(key, str) and key.startswith("u:"):
        return "u:" + normalize_stream_url(key[2:])
    return key


# Batas waktu I/O jaringan ffmpeg (mikrodetik). Tanpa ini ffmpeg bisa
# menggantung selamanya bila server kamera berhenti mengirim data.
FFMPEG_IO_TIMEOUT_US = 15_000_000


def ffmpeg_input_allowed(url: str) -> bool:
    """ffmpeg tersambung sendiri (tanpa net_guard): tolak host yang me-resolve ke
    alamat internal terlarang (SSRF). Gagal resolve biasa dibiarkan (ffmpeg gagal sendiri)."""
    try:
        net_guard.check_url_host(url)
    except net_guard.UnsafeAddressError:
        return False
    except OSError:
        pass
    return True


def ffmpeg_timeout_args(url: str) -> list[str]:
    if url.startswith(("rtsp://", "rtsps://")):
        return ["-timeout", str(FFMPEG_IO_TIMEOUT_US)]
    if url.startswith(("http://", "https://")):
        return ["-rw_timeout", str(FFMPEG_IO_TIMEOUT_US)]
    return []


def cam_type(cam: dict) -> str:
    if cam.get("type"):
        return cam["type"]
    if cam.get("provider") == "youtube":
        return "youtube"
    if cam.get("provider") == "jombangkab":
        return "mjpeg"
    return "hls"


def draw_detections(img_bgr, detections: list[dict]):
    """Salinan gambar dengan kotak + label deteksi (CCTipi System)."""
    annotated = img_bgr.copy()
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det["box"])
        color = CATEGORY_COLORS.get(det["category"], (0, 255, 255))
        label = f"{LABEL_ID.get(det['class_name'], det['class_name'])} {det['confidence']:.2f}"
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        cv2.rectangle(annotated, (x1, max(0, y1 - th - 8)),
                      (x1 + tw + 6, y1), color, -1)
        cv2.putText(annotated, label, (x1 + 3, max(th + 2, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (10, 10, 10), 2)
    return annotated


# ---------------------------------------------------------------------------
# v6.7: Zona deteksi & jadwal aktif per kamera
# ---------------------------------------------------------------------------
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def point_in_polygon(x: float, y: float, poly: list) -> bool:
    """Ray casting; poly = [[x, y], ...] (koordinat ternormalisasi 0..1)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def schedule_active(schedule: dict | None, now: float | None = None) -> bool:
    """True bila jadwal tidak dipakai, atau waktu sekarang di dalam jam aktif.
    Jam melewati tengah malam (18:00–06:00) didukung; hari mengikuti hari mulai."""
    if not schedule or not schedule.get("enabled"):
        return True
    t = time.localtime(now if now is not None else time.time())
    days = schedule.get("days")
    days = set(days) if isinstance(days, list) and days else set(range(7))
    sh, sm = (int(v) for v in schedule.get("start", "00:00").split(":"))
    eh, em = (int(v) for v in schedule.get("end", "23:59").split(":"))
    cur, start, end = t.tm_hour * 60 + t.tm_min, sh * 60 + sm, eh * 60 + em
    today = t.tm_wday  # 0 = Senin
    if start <= end:
        return today in days and start <= cur < end
    # melewati tengah malam: bagian malam milik hari ini, bagian pagi milik kemarin
    if cur >= start:
        return today in days
    return cur < end and ((today - 1) % 7) in days


def filter_by_zone(detections: list[dict], zone: dict | None, width: int, height: int) -> list[dict]:
    """Simpan deteksi yang titik kakinya (tengah-bawah kotak) ada di salah satu zona."""
    polys = (zone or {}).get("polygons") or []
    if not polys or not width or not height:
        return detections
    out = []
    for d in detections:
        x1, _y1, x2, y2 = d["box"]
        fx, fy = ((x1 + x2) / 2) / width, y2 / height
        if any(point_in_polygon(fx, fy, p) for p in polys):
            out.append(d)
    return out


def clean_zone(data: dict) -> dict | None:
    """Validasi zona dari API. None = hapus zona (tanpa poligon & tanpa jadwal)."""
    polys = []
    for poly in (data.get("polygons") or [])[:8]:
        if not isinstance(poly, list):
            continue
        pts = []
        for pt in poly[:40]:
            try:
                x, y = float(pt[0]), float(pt[1])
            except (TypeError, ValueError, IndexError):
                continue
            pts.append([round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)])
        if len(pts) >= 3:
            polys.append(pts)
    sch = data.get("schedule") or {}
    start = str(sch.get("start") or "18:00")
    end = str(sch.get("end") or "06:00")
    if not _HHMM.match(start) or not _HHMM.match(end):
        raise ValueError("Format jam harus HH:MM (mis. 18:00).")
    days = sorted({int(d) for d in (sch.get("days") or list(range(7))) if str(d).isdigit() and 0 <= int(d) <= 6})
    schedule = {"enabled": bool(sch.get("enabled")), "start": start, "end": end, "days": days or list(range(7))}
    if not polys and not schedule["enabled"]:
        return None
    return {"polygons": polys, "schedule": schedule}


def draw_zones(img_bgr, zone: dict | None):
    """Gambar garis zona (kuning) di jepretan agar konteksnya jelas."""
    polys = (zone or {}).get("polygons") or []
    if not polys:
        return img_bgr
    h, w = img_bgr.shape[:2]
    for poly in polys:
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], dtype=np.int32)
        cv2.polylines(img_bgr, [pts], True, (0, 215, 255), 2, cv2.LINE_AA)
    return img_bgr


# ---------------------------------------------------------------------------
# Detektor YOLO (ONNX Runtime, pra/pasca-proses NumPy)
# ---------------------------------------------------------------------------
class _Detector:
    def __init__(self, model_path: Path):
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.size = 640

    def infer(self, img_bgr, threshold: float) -> list[dict]:
        h, w = img_bgr.shape[:2]
        size = self.size
        scale = min(size / w, size / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(img_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
        canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.session.run(None, {self.input_name: blob})[0][0]  # (84, N)
        boxes_cxcywh = out[:4].T
        scores_all = out[4:].T
        class_ids = np.argmax(scores_all, axis=1)
        confs = scores_all[np.arange(scores_all.shape[0]), class_ids]
        mask = confs >= max(0.05, threshold - 0.15)  # pra-filter longgar, NMS dulu
        if not np.any(mask):
            return []
        boxes_cxcywh, class_ids, confs = boxes_cxcywh[mask], class_ids[mask], confs[mask]
        xyxy = np.empty_like(boxes_cxcywh)
        xyxy[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        xyxy[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        xyxy[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        xyxy[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / scale
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / scale
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, w)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, h)
        rects = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)]
                 for x1, y1, x2, y2 in xyxy]
        idxs = cv2.dnn.NMSBoxes(rects, [float(c) for c in confs], threshold, 0.45)
        results: list[dict] = []
        for i in np.array(idxs).flatten().tolist():
            conf = float(confs[i])
            if conf < threshold:
                continue
            cid = int(class_ids[i])
            x1, y1, x2, y2 = (float(v) for v in xyxy[i])
            results.append({
                "class_id": cid,
                "class_name": COCO_NAMES[cid] if cid < len(COCO_NAMES) else str(cid),
                "category": CLASS_CATEGORY.get(cid, ""),
                "confidence": round(conf, 4),
                "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            })
        results.sort(key=lambda d: -d["confidence"])
        return results[:20]


# ---------------------------------------------------------------------------
# Pengambilan frame
# ---------------------------------------------------------------------------
def _http_headers_for(url: str) -> dict:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    headers = {"User-Agent": USER_AGENT}
    if host == "stream.kuduskab.go.id":
        headers["Origin"] = "https://streaming.kuduskab.go.id"
    elif host == "cctv.jombangkab.go.id":
        headers["Referer"] = "https://cctv.jombangkab.go.id/v2/"
    else:
        headers["Origin"] = "https://samudra.jepara.go.id"
    return headers


def _ffmpeg_grab(url: str, is_rtsp: bool = False, timeout: int = 25) -> bytes | None:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not ffmpeg_input_allowed(url):
        return None
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin"]
    cmd += ffmpeg_timeout_args(url)
    if is_rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    elif url.startswith(("http://", "https://")):
        hdrs = _http_headers_for(url)
        header_blob = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        cmd += ["-headers", header_blob]
    cmd += ["-i", url, "-frames:v", "1", "-q:v", "3",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        with LOAD_GUARD.ffmpeg_slot("ai_grab"):
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        data = proc.stdout or b""
        if len(data) > 2000 and data[:2] == b"\xff\xd8":
            return data
    except CapacityError:
        return None
    except Exception:
        pass
    return None


def _http_grab(url: str, timeout: int = 12) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=_http_headers_for(url))
        with net_guard.safe_urlopen(req, timeout=timeout) as resp:
            data = resp.read(4_000_000)
        if len(data) > 2000 and data[:2] == b"\xff\xd8":
            return data
    except Exception:
        pass
    return None


def _mjpeg_grab(url: str, timeout: int = 12) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=_http_headers_for(url))
        with net_guard.safe_urlopen(req, timeout=timeout) as resp:
            buf = b""
            while len(buf) < 3_000_000:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                start = buf.find(b"\xff\xd8")
                if start >= 0:
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end > 0:
                        return buf[start:end + 2]
    except Exception:
        pass
    return None


def grab_frame(cam: dict) -> bytes | None:
    """Ambil 1 frame JPEG dari kamera. None bila gagal / tidak didukung."""
    url = (cam.get("url") or "").strip()
    if not url:
        return None
    ctype = cam_type(cam)
    if ctype == "youtube":
        return None  # stream YouTube tidak bisa diambil frame-nya secara wajar
    if url.startswith("rtsp://"):
        return _ffmpeg_grab(url, is_rtsp=True)
    if ctype == "http":
        return _http_grab(url) or _ffmpeg_grab(url)
    if ctype == "mjpeg":
        return _mjpeg_grab(url) or _ffmpeg_grab(url)
    # hls / onvif / lainnya → ffmpeg
    return _ffmpeg_grab(url)


# ---------------------------------------------------------------------------
# Layanan AI: worker + penyimpanan + Telegram
# ---------------------------------------------------------------------------
class AiService:
    def __init__(self, get_cameras_fn):
        self._get_cameras = get_cameras_fn
        self._lock = threading.Lock()
        self._settings = self._load_settings()
        self._detector: _Detector | None = None
        self._detector_error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_shot: dict[str, float] = {}
        self._last_cleanup = 0.0
        self._last_error = ""
        self._cycles = 0
        # Telegram dikirim dari thread terpisah agar worker deteksi tidak
        # tertahan hingga 20 dtk per kirim saat jaringan lambat.
        self._tg_queue: queue.Queue = queue.Queue(maxsize=50)
        self._tg_thread: threading.Thread | None = None
        # Fase 4: callback opsional (dipasang server.py -> RecorderService.
        # trigger_clip). Dipanggil sesudah jepretan tersimpan agar kamera
        # yang sama direkam sebagai klip deteksi. None = tidak terhubung.
        self.on_detection = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        JEPRETAN_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
        # migrasi: kunci kamera lama yang memuat token -> kunci stabil
        cams = settings.get("cameras") or []
        migrated = list(dict.fromkeys(normalize_key(k) for k in cams))
        if migrated != cams:
            settings["cameras"] = migrated
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

    def _telegram_config(self) -> tuple[str, str]:
        token = (os.environ.get("CCTIPI_TG_BOT_TOKEN")
                 or self._settings.get("telegram_bot_token") or "").strip()
        chat_id = (os.environ.get("CCTIPI_TG_CHAT_ID")
                   or self._settings.get("telegram_chat_id") or "").strip()
        return token, chat_id

    def telegram_configured(self) -> bool:
        token, chat_id = self._telegram_config()
        return bool(token and chat_id)

    def public_settings(self) -> dict:
        with self._lock:
            s = dict(self._settings)
        classes = dict(s.get("classes") or {})
        objects = dict(s.get("objects") or {})
        return {
            "enabled": bool(s.get("enabled")),
            "interval": s.get("interval"),
            "threshold": s.get("threshold"),
            "cooldown": s.get("cooldown"),
            "retention_days": s.get("retention_days"),
            "classes": {k: bool(classes.get(k, CATEGORY_DEFAULTS.get(k, True))) for k in CATEGORIES},
            "objects": {n: bool(objects.get(n, True)) for n in sorted(KNOWN_OBJECTS)},
            "catalog": class_catalog(),
            "cameras": list(s.get("cameras") or []),
            "telegram_enabled": bool(s.get("telegram_enabled")),
            "telegram_chat_id": s.get("telegram_chat_id") or "",
            "telegram_token_set": bool(s.get("telegram_bot_token")),
            "telegram_env": bool(os.environ.get("CCTIPI_TG_BOT_TOKEN")),
            "telegram_configured": self.telegram_configured(),
            "enhance_before_detect": bool(s.get("enhance_before_detect")),
            "enhance_available": _enhance_module is not None,
            "zones": dict(s.get("zones") or {}),
        }

    def zone_for(self, key: str) -> dict | None:
        with self._lock:
            return (self._settings.get("zones") or {}).get(key)

    def set_zone(self, key: str, data: dict) -> dict | None:
        zone = clean_zone(data if isinstance(data, dict) else {})
        with self._lock:
            zones = dict(self._settings.get("zones") or {})
            if zone is None:
                zones.pop(key, None)
            else:
                zones[key] = zone
            self._settings["zones"] = zones
            self._save_settings()
        return zone

    def update_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Payload pengaturan tidak valid")
        with self._lock:
            s = self._settings
            if "enabled" in payload:
                s["enabled"] = bool(payload["enabled"])
            if "interval" in payload:
                s["interval"] = min(3600, max(3, int(payload["interval"])))
            if "threshold" in payload:
                s["threshold"] = min(0.95, max(0.05, float(payload["threshold"])))
            if "cooldown" in payload:
                s["cooldown"] = min(86400, max(0, int(payload["cooldown"])))
            if "retention_days" in payload:
                s["retention_days"] = min(365, max(1, int(payload["retention_days"])))
            if "classes" in payload and isinstance(payload["classes"], dict):
                s["classes"] = dict(s.get("classes") or {})
                for key in CATEGORIES:
                    if key in payload["classes"]:
                        s["classes"][key] = bool(payload["classes"][key])
            if "objects" in payload and isinstance(payload["objects"], dict):
                # simpan hanya objek yang dimatikan -> objek baru otomatis aktif
                objs = dict(s.get("objects") or {})
                for name, on in payload["objects"].items():
                    if name in KNOWN_OBJECTS:
                        if on:
                            objs.pop(name, None)
                        else:
                            objs[name] = False
                s["objects"] = objs
            if "cameras" in payload and isinstance(payload["cameras"], list):
                s["cameras"] = [str(k) for k in payload["cameras"]][:2000]
            if "telegram_enabled" in payload:
                s["telegram_enabled"] = bool(payload["telegram_enabled"])
            if "telegram_chat_id" in payload:
                s["telegram_chat_id"] = str(payload["telegram_chat_id"]).strip()
            token = payload.get("telegram_bot_token")
            if isinstance(token, str) and token.strip():
                s["telegram_bot_token"] = token.strip()
            if payload.get("telegram_bot_token_clear"):
                s["telegram_bot_token"] = ""
            if "enhance_before_detect" in payload:
                s["enhance_before_detect"] = bool(payload["enhance_before_detect"])
            self._save_settings()
        return self.public_settings()

    # ---------- basis data jepretan ----------
    def _db(self):
        return closing_sqlite(DB_FILE, timeout=10)

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jepretan (
                    id TEXT PRIMARY KEY,
                    ts INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    camera_key TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    folder TEXT DEFAULT '',
                    category TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    detections TEXT DEFAULT '[]',
                    file TEXT NOT NULL
                )
                """
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jepretan)").fetchall()]
            if "enhanced" not in cols:
                conn.execute("ALTER TABLE jepretan ADD COLUMN enhanced INTEGER DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jepretan_date ON jepretan(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jepretan_ts ON jepretan(ts)")
            # migrasi kunci kamera bertoken -> kunci stabil (filter per kamera tetap jalan)
            for (old,) in conn.execute(
                    "SELECT DISTINCT camera_key FROM jepretan WHERE camera_key LIKE 'u:%?%'").fetchall():
                new = normalize_key(old)
                if new != old:
                    conn.execute("UPDATE jepretan SET camera_key=? WHERE camera_key=?", (new, old))

    def list_jepretan(self, date: str = "", camera: str = "",
                      category: str = "", limit: int = 200,
                      allowed_keys: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM jepretan WHERE 1=1"
        args: list = []
        if allowed_keys is not None:  # pembatasan wilayah per pengguna
            sql += " AND camera_key IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(list(allowed_keys)))
        if date:
            sql += " AND date = ?"
            args.append(date)
        if camera:
            sql += " AND camera_key = ?"
            args.append(camera)
        if category:
            sql += " AND category = ?"
            args.append(category)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(min(1000, max(1, int(limit))))
        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detections"] = json.loads(item.get("detections") or "[]")
            item["image_url"] = f"/api/jepretan/{item['id']}/image"
            out.append(item)
        return out

    def count_today(self) -> int:
        today = time.strftime("%Y-%m-%d")
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jepretan WHERE date = ?", (today,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_jepretan(self, jepretan_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jepretan_id or ""):
            return None
        with self._db() as conn:
            row = conn.execute("SELECT * FROM jepretan WHERE id = ?", (jepretan_id,)).fetchone()
        return dict(row) if row else None

    def jepretan_camera_key(self, jepretan_id: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jepretan_id or ""):
            return None
        with self._db() as conn:
            row = conn.execute(
                "SELECT camera_key FROM jepretan WHERE id = ?", (jepretan_id,)
            ).fetchone()
        return row["camera_key"] if row else None

    def get_image_path(self, jepretan_id: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jepretan_id or ""):
            return None
        with self._db() as conn:
            row = conn.execute(
                "SELECT file FROM jepretan WHERE id = ?", (jepretan_id,)
            ).fetchone()
        if not row:
            return None
        path = (DATA_DIR / row["file"]).resolve()
        if JEPRETAN_DIR.resolve() not in path.parents or not path.exists():
            return None
        return path

    def enhanced_image_path(self, jepretan_id: str) -> Path | None:
        """Versi Perjelas Malam dari gambar jepretan (Fase 3).

        Dihasilkan sekali lalu di-cache ke data/jepretan-enhanced/<id>.jpg.
        None bila jepretan tidak ada / modul enhance tidak tersedia.
        """
        original = self.get_image_path(jepretan_id)
        if original is None or _enhance_module is None:
            return None
        cache = ENHANCED_DIR / f"{jepretan_id}.jpg"
        if cache.exists():
            return cache
        try:
            data, _info = _enhance_module.enhance_jpeg_bytes(
                original.read_bytes(), mode="always"
            )
        except Exception:
            return None
        if not data:
            return None
        ENHANCED_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache)
        return cache

    def delete_jepretan(self, jepretan_id: str) -> bool:
        path = self.get_image_path(jepretan_id)
        with self._db() as conn:
            cur = conn.execute("DELETE FROM jepretan WHERE id = ?", (jepretan_id,))
            deleted = cur.rowcount > 0
        if deleted and path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        if deleted:
            try:
                (ENHANCED_DIR / f"{jepretan_id}.jpg").unlink(missing_ok=True)
            except Exception:
                pass
        return deleted

    def _cleanup(self) -> None:
        with self._lock:
            retention = int(self._settings.get("retention_days") or 7)
        cutoff = time.time() - retention * 86400
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, file FROM jepretan WHERE ts < ?", (int(cutoff),)
            ).fetchall()
            conn.execute("DELETE FROM jepretan WHERE ts < ?", (int(cutoff),))
        for row in rows:
            try:
                (DATA_DIR / row["file"]).unlink(missing_ok=True)
                (ENHANCED_DIR / f"{row['id']}.jpg").unlink(missing_ok=True)
            except Exception:
                pass
        # cache enhance yang jepretannya sudah tidak ada (mis. dihapus manual)
        if ENHANCED_DIR.exists():
            with self._db() as conn:
                alive = {r[0] for r in conn.execute("SELECT id FROM jepretan").fetchall()}
            for f in ENHANCED_DIR.glob("*.jpg"):
                if f.stem not in alive:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        # bersihkan folder tanggal yang kosong
        if JEPRETAN_DIR.exists():
            for day_dir in JEPRETAN_DIR.iterdir():
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    day_dir.rmdir()
        self._last_cleanup = time.time()

    # ---------- deteksi ----------
    def _get_detector(self) -> _Detector | None:
        if not AI_AVAILABLE:
            return None
        if self._detector is None and not self._detector_error:
            ok_sum, detail = verify_model_checksum(MODEL_FILE)
            if not ok_sum:
                self._detector_error = detail
                return None
            try:
                self._detector = _Detector(MODEL_FILE)
            except Exception as exc:
                self._detector_error = redact_sensitive_text(f"Gagal memuat model: {exc}")
        return self._detector

    def detect_bytes(self, jpeg_bytes: bytes, threshold: float | None = None,
                     classes: dict | None = None) -> tuple[list[dict], bytes | None]:
        """Deteksi pada 1 frame JPEG. Mengembalikan (deteksi, jpeg beranotasi)."""
        detections, annotated, _info = self.detect_frame(
            jpeg_bytes, threshold=threshold, classes=classes, enhance_first=False
        )
        return detections, annotated

    def detect_frame(self, jpeg_bytes: bytes, threshold: float | None = None,
                     classes: dict | None = None,
                     enhance_first: bool | None = None,
                     zone: dict | None = None,
                     ) -> tuple[list[dict], bytes | None, dict]:
        """Deteksi 1 frame; opsional diperjelas dulu (Fase 3).

        Bila enhance aktif, inferensi YOLO berjalan pada frame hasil
        Perjelas Malam, TAPI gambar beranotasi yang dikembalikan digambar
        di atas frame ASLI — jepretan yang tersimpan tetap frame asli.
        Info enhance (applied, kecerahan sebelum/sesudah) ikut dikembalikan.
        """
        info: dict = {"enhanced": False}
        detector = self._get_detector()
        if detector is None or not jpeg_bytes:
            return [], None, info
        with self._lock:
            s = dict(self._settings)
        thr = float(threshold if threshold is not None else s.get("threshold", 0.45))
        active = classes if classes is not None else (s.get("classes") or {})
        if enhance_first is None:
            enhance_first = bool(s.get("enhance_before_detect"))
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return [], None, info
        infer_img = img
        if enhance_first and _enhance_module is not None:
            try:
                infer_img, einfo = _enhance_module.enhance_image(img, mode="auto")
                info = {
                    "enhanced": bool(einfo.get("applied")),
                    "brightness_before": einfo.get("brightness_before"),
                    "brightness_after": einfo.get("brightness_after"),
                    "gamma": einfo.get("gamma"),
                }
            except Exception:
                infer_img = img
        raw = detector.infer(infer_img, thr)
        objects = s.get("objects") or {}
        detections = [d for d in raw if detection_enabled(d, active, objects)]
        detections = filter_by_zone(detections, zone, img.shape[1], img.shape[0])
        if not detections:
            return [], None, info
        annotated = draw_zones(draw_detections(img, detections), zone)
        ok, buf = cv2.imencode(".jpg", annotated,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return detections, (buf.tobytes() if ok else None), info

    def detect_on_image(self, img_bgr, threshold: float | None = None,
                        classes: dict | None = None) -> list[dict]:
        """Deteksi pada array gambar BGR yang sudah ter-decode (Live AI).

        Hanya mengembalikan daftar deteksi terfilter (kategori & threshold
        dari pengaturan AI). Tidak menggambar / encode JPEG di sini: Live AI
        menggambar sendiri ke frame tampilan, dan JPEG jepretan dibuat
        lewat offer_live_snapshot() hanya bila memang akan disimpan.
        """
        detector = self._get_detector()
        if detector is None or img_bgr is None:
            return []
        with self._lock:
            s = dict(self._settings)
        thr = float(threshold if threshold is not None else s.get("threshold", 0.45))
        active = classes if classes is not None else (s.get("classes") or {})
        raw = detector.infer(img_bgr, thr)
        objects = s.get("objects") or {}
        return [d for d in raw if detection_enabled(d, active, objects)]

    # ---------- jepretan: cooldown bersama + Telegram (worker & Live AI) ----------
    def _claim_cooldown(self, key: str) -> bool:
        """True bila kamera boleh membuat jepretan sekarang (lalu jeda dimulai).
        Satu catatan jeda per kamera dipakai bersama oleh worker jepretan dan
        Live AI — tidak ada jepretan ganda dari dua jalur untuk kamera sama."""
        now = time.time()
        with self._lock:
            cooldown = int(self._settings.get("cooldown") or 0)
            if now - self._last_shot.get(key, 0) < cooldown:
                return False
            self._last_shot[key] = now
        return True

    def _store_and_notify(self, cam: dict, detections: list[dict], annotated: bytes,
                          enhanced: bool = False, source: str = "Jepretan AI") -> str | None:
        jepretan_id = self.save_snapshot(cam, detections, annotated, enhanced=enhanced)
        if jepretan_id is None:
            return None
        with self._lock:
            telegram_on = bool(self._settings.get("telegram_enabled"))
        if telegram_on:
            top = detections[0]
            caption = (
                f"CCTipi — {source}\n"
                f"Kamera: {cam.get('name') or camera_key(cam)}\n"
                f"Deteksi: {LABEL_ID.get(top['class_name'], top['class_name'])} "
                f"({top['confidence']:.0%})\n"
                f"Waktu: {time.strftime('%d-%m-%Y %H:%M:%S')}"
            )
            self._ensure_tg_thread()
            try:
                self._tg_queue.put_nowait((annotated, caption))
            except queue.Full:
                self._last_error = "Telegram: antrean penuh, jepretan dilewati"
        return jepretan_id

    def offer_live_snapshot(self, cam: dict, detections: list[dict], frame_bgr) -> str | None:
        """Jalur jepretan dari Live AI. Aturannya SAMA dengan worker jepretan:
        hanya bila AI Jepretan aktif, kamera ini opt-in, dan jeda kamera
        sudah lewat. Jepretan yang tersimpan juga dikirim ke Telegram dan
        memicu klip rekaman deteksi. Mengembalikan id jepretan / None."""
        if not detections or frame_bgr is None:
            return None
        key = camera_key(cam)
        with self._lock:
            enabled = bool(self._settings.get("enabled"))
            opted_in = key in set(self._settings.get("cameras") or [])
        if not (enabled and opted_in):
            return None
        zone = self.zone_for(key)
        if not schedule_active((zone or {}).get("schedule")):
            return None
        detections = filter_by_zone(detections, zone, frame_bgr.shape[1], frame_bgr.shape[0])
        if not detections:
            return None
        if not self._claim_cooldown(key):
            return None
        ok, buf = cv2.imencode(".jpg", draw_zones(draw_detections(frame_bgr, detections), zone),
                               [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return None
        return self._store_and_notify(cam, detections, buf.tobytes(), source="Live AI")

    def save_snapshot(self, cam: dict, detections: list[dict],
                      annotated: bytes, enhanced: bool = False) -> str | None:
        """Simpan 1 jepretan beranotasi + baris DB + picu on_detection.

        Jalur yang sama seperti worker jepretan (dipakai ulang Fase 5 Live
        AI). Cooldown & Telegram tetap urusan pemanggil. Mengembalikan id
        jepretan, atau None bila gagal.
        """
        if not detections or not annotated:
            return None
        key = camera_key(cam)
        top = detections[0]
        now = time.time()
        date_str = time.strftime("%Y-%m-%d")
        jepretan_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        try:
            day_dir = JEPRETAN_DIR / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            rel_file = f"jepretan/{date_str}/{jepretan_id}.jpg"
            (DATA_DIR / rel_file).write_bytes(annotated)
            with self._db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO jepretan "
                    "(id, ts, date, camera_key, camera_name, folder, category, "
                    "class_name, confidence, detections, file, enhanced) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (jepretan_id, int(now), date_str, key,
                     cam.get("name") or key, cam.get("folder") or "",
                     top["category"], top["class_name"], top["confidence"],
                     json.dumps(detections, ensure_ascii=False), rel_file,
                     1 if enhanced else 0),
                )
        except Exception:
            return None
        # Fase 4: picu klip rekaman deteksi di server (bila recorder terhubung)
        if self.on_detection is not None:
            try:
                self.on_detection(cam)
            except Exception:
                pass
        return jepretan_id

    # ---------- Telegram ----------
    def send_telegram_photo(self, jpeg_bytes: bytes, caption: str) -> tuple[bool, str]:
        """Kirim foto ke Telegram. Mengembalikan (sukses, keterangan) — tidak pernah raise."""
        token, chat_id = self._telegram_config()
        if not token or not chat_id:
            return False, "belum terkonfigurasi"
        boundary = f"----cctipi{uuid.uuid4().hex}"
        parts = []
        for name, value in (("chat_id", chat_id), ("caption", caption[:1000])):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
            f"filename=\"jepretan.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + jpeg_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if payload.get("ok"):
                return True, "terkirim"
            return False, f"Telegram menolak: {payload.get('description', 'tidak diketahui')}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code} dari Telegram"
        except Exception as exc:
            return False, f"Gagal menghubungi Telegram: {exc.__class__.__name__}"

    def send_telegram_text(self, text: str) -> tuple[bool, str]:
        """Kirim pesan teks (dipakai alarm kamera offline / disk penuh)."""
        with self._lock:
            token, chat_id = self._telegram_config()
        if not (token and chat_id):
            return False, "Telegram belum dikonfigurasi"
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return (True, "terkirim") if payload.get("ok") else (False, f"Telegram menolak: {payload.get('description', '')}")
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code} dari Telegram"
        except Exception as exc:
            return False, f"Gagal menghubungi Telegram: {exc.__class__.__name__}"

    # ---------- worker ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="cctipi-ai", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ensure_tg_thread(self) -> None:
        if self._tg_thread and self._tg_thread.is_alive():
            return
        self._tg_thread = threading.Thread(
            target=self._tg_loop, name="cctipi-telegram", daemon=True)
        self._tg_thread.start()

    def _tg_loop(self) -> None:
        while True:
            jpeg, caption = self._tg_queue.get()
            try:
                ok, info = self.send_telegram_photo(jpeg, caption)
                if not ok and info != "belum terkonfigurasi":
                    self._last_error = f"Telegram: {info}"
            except Exception as exc:
                self._last_error = f"Telegram: {exc.__class__.__name__}"
            finally:
                self._tg_queue.task_done()

    def _resolve_optin_cameras(self) -> list[dict]:
        with self._lock:
            wanted = set(self._settings.get("cameras") or [])
        if not wanted:
            return []
        try:
            all_cams = self._get_cameras().get("cameras", [])
        except Exception as exc:
            self._last_error = f"Gagal membaca daftar kamera: {exc}"
            return []
        return [c for c in all_cams if camera_key(c) in wanted]

    def _process_camera(self, cam: dict) -> None:
        key = camera_key(cam)
        with self._lock:
            cooldown = int(self._settings.get("cooldown") or 0)
            last = self._last_shot.get(key, 0)
        if time.time() - last < cooldown:
            return  # masih jeda: tidak perlu ambil frame & jalankan model
        zone = self.zone_for(key)
        if not schedule_active((zone or {}).get("schedule")):
            return  # di luar jam aktif kamera ini
        frame = grab_frame(cam)
        if not frame:
            return
        detections, annotated, det_info = self.detect_frame(frame, zone=zone)
        if not detections or not annotated:
            return
        if not self._claim_cooldown(key):
            return  # Live AI baru saja menjepret kamera ini
        # jalur bersama: simpan + klip rekaman (on_detection) + Telegram
        self._store_and_notify(cam, detections, annotated,
                               enhanced=bool(det_info.get("enhanced")))

    def _worker_loop(self) -> None:
        try:
            self._cleanup()
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                with self._lock:
                    enabled = bool(self._settings.get("enabled"))
                    interval = int(self._settings.get("interval") or 10)
                if time.time() - self._last_cleanup > 3600:
                    self._cleanup()
                if enabled and AI_AVAILABLE:
                    cams = self._resolve_optin_cameras()
                    for cam in cams:
                        if self._stop.is_set():
                            break
                        try:
                            self._process_camera(cam)
                        except Exception as exc:
                            self._last_error = (
                                f"Kamera {cam.get('name')}: {exc.__class__.__name__}"
                            )
                        self._stop.wait(0.5)
                    self._cycles += 1
                self._stop.wait(max(3, interval) if enabled else 2)
            except Exception as exc:  # worker tidak boleh mati / melempar traceback
                self._last_error = f"Worker: {exc.__class__.__name__}: {exc}"
                self._stop.wait(3)

    # ---------- status ----------
    def status(self) -> dict:
        with self._lock:
            s = dict(self._settings)
        exists = MODEL_FILE.exists()
        ok_sum, sum_detail = verify_model_checksum(MODEL_FILE) if exists else (False, "")
        model_info = {
            "name": MODEL_NAME,
            "file": MODEL_FILE.name,
            "exists": exists,
            "size_bytes": MODEL_FILE.stat().st_size if exists else 0,
            "expected_sha256": MODEL_SHA256,
            "sha256_verified": ok_sum,
            "loaded": self._detector is not None,
        }
        reason = _UNAVAILABLE_REASON or self._detector_error or ("" if (not exists or ok_sum) else sum_detail)
        return {
            "available": AI_AVAILABLE and (not exists or ok_sum),
            "unavailable_reason": redact_sensitive_text(reason),
            "model": model_info,
            "running": bool(s.get("enabled")) and AI_AVAILABLE and ok_sum
                       and bool(self._thread and self._thread.is_alive()),
            "enabled": bool(s.get("enabled")),
            "monitored_cameras": len(s.get("cameras") or []),
            "today_count": self.count_today(),
            "telegram_configured": self.telegram_configured(),
            "telegram_enabled": bool(s.get("telegram_enabled")),
            "cycles": self._cycles,
            "last_error": redact_sensitive_text(self._last_error),
        }


if __name__ == "__main__":
    if "--download-model" in sys.argv:
        p = download_model()
        print(f"Model YOLO11n berhasil diunduh & diverifikasi SHA-256 -> {p}")
    else:
        ok_m, info_m = verify_model_checksum()
        print(f"Model: {MODEL_FILE} · terverifikasi={ok_m} ({info_m})")

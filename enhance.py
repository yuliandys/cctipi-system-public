#!/usr/bin/env python3
"""CCTipi System v1.0 — Perjelas Malam (low-light enhancement).

Memperjelas frame CCTV yang gelap memakai OpenCV klasik saja
(TANPA model AI tambahan, CPU-friendly):

1. Ukur kecerahan rata-rata (luminance kanal Y / nilai V).
2. Bila frame sudah cukup terang -> dikembalikan apa adanya
   (tidak diterapkan apa pun, supaya tidak over-expose).
3. Bila gelap:
   - CLAHE pada kanal L ruang warna LAB (menaikkan kontras lokal
     tanpa menggeser warna),
   - koreksi gamma adaptif (gamma diturunkan sebanding tingkat
     kegelapan, dibatasi agar tidak berlebihan),
   - denoise ringan (bilateral filter) untuk menahan noise yang
     ikut naik pada area sangat gelap.

Target: < 0,5 detik untuk frame 720p di CPU biasa.

Fungsi utama:
    enhance_image(img, mode='auto') -> (img_baru, info)
    info = {
        'applied': bool,          # apakah enhancement diterapkan
        'mode': 'auto'|'always'|'off',
        'brightness_before': float,  # 0..255
        'brightness_after': float,   # 0..255
        'gamma': float | None,       # gamma yang dipakai bila diterapkan
        'reason': str,
    }
"""

from __future__ import annotations

import cv2
import numpy as np

# Di bawah ambang ini frame dianggap gelap dan layak diperjelas (mode auto).
DARK_THRESHOLD = 70.0
# Target kecerahan rata-rata sesudah koreksi gamma.
TARGET_BRIGHTNESS = 110.0
# Batas gamma: terlalu kecil = gambar pucat & noise meledak.
GAMMA_MIN = 0.45
GAMMA_MAX = 1.0

_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def measure_brightness(img) -> float:
    """Kecerahan rata-rata 0..255 dari gambar BGR."""
    if img is None or img.size == 0:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray))


def _adaptive_gamma(mean_brightness: float) -> float:
    """Gamma < 1 mencerahkan. Dihitung agar rata-rata bergerak ke TARGET."""
    if mean_brightness <= 1.0:
        return GAMMA_MIN
    gamma = np.log(TARGET_BRIGHTNESS / 255.0) / np.log(mean_brightness / 255.0)
    return float(min(GAMMA_MAX, max(GAMMA_MIN, gamma)))


def enhance_image(img, mode: str = "auto"):
    """Perjelas gambar BGR bila gelap. Mengembalikan (gambar, info)."""
    info = {
        "applied": False,
        "mode": mode,
        "brightness_before": None,
        "brightness_after": None,
        "gamma": None,
        "reason": "",
    }
    if img is None or img.size == 0:
        info["reason"] = "gambar kosong"
        return img, info

    before = measure_brightness(img)
    info["brightness_before"] = round(before, 2)

    if mode == "off":
        info["brightness_after"] = info["brightness_before"]
        info["reason"] = "mode off"
        return img, info
    if mode == "auto" and before >= DARK_THRESHOLD:
        info["brightness_after"] = info["brightness_before"]
        info["reason"] = f"sudah cukup terang ({before:.0f} >= {DARK_THRESHOLD:.0f})"
        return img, info

    out = img

    # 1) CLAHE pada kanal L (LAB) — kontras lokal tanpa menggeser warna.
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    l_chan = _CLAHE.apply(l_chan)
    out = cv2.cvtColor(cv2.merge((l_chan, a_chan, b_chan)), cv2.COLOR_LAB2BGR)

    # 2) Gamma adaptif berdasarkan kecerahan sesudah CLAHE.
    mid = measure_brightness(out)
    gamma = _adaptive_gamma(mid)
    if gamma < 0.999:
        # koreksi gamma: piksel_norm ** gamma, gamma < 1 mencerahkan
        table = np.array(
            [((i / 255.0) ** gamma) * 255 for i in range(256)], dtype=np.uint8
        )
        out = cv2.LUT(out, table)
    info["gamma"] = round(gamma, 3)

    # 3) Denoise ringan — bilateral menjaga tepi objek, menahan noise malam.
    out = cv2.bilateralFilter(out, d=5, sigmaColor=40, sigmaSpace=40)

    after = measure_brightness(out)
    info["brightness_after"] = round(after, 2)
    info["applied"] = True
    info["reason"] = f"gelap ({before:.0f} < {DARK_THRESHOLD:.0f})" if mode == "auto" else "mode always"
    return out, info


def enhance_jpeg_bytes(data: bytes, mode: str = "auto"):
    """Varian bytes->bytes untuk endpoint server. (bytes|None, info)."""
    if not data:
        return None, {"applied": False, "reason": "data kosong"}
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, {"applied": False, "reason": "gagal decode JPEG"}
    out, info = enhance_image(img, mode=mode)
    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return None, {**info, "applied": False, "reason": "gagal encode JPEG"}
    return buf.tobytes(), info

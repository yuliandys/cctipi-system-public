#!/usr/bin/env python3
"""CCTipi System — MJPEG Hub.

Masalah yang diselesaikan: browser hanya membuka ±6 koneksi HTTP/1.1 sekaligus
ke satu server. Setiap tile MJPEG (Jombang, IP camera lokal) menahan satu
koneksi selamanya, sehingga dengan >6 tile MJPEG semua stream lain DAN semua
panggilan API ikut macet (tampil "offline").

Solusi: server membaca setiap kamera MJPEG SEKALI (satu pembaca bersama per
URL, dipakai semua penonton), menyimpan frame JPEG terbaru di memori, lalu
browser mengambil frame itu lewat permintaan pendek (/api/mjpeg-frame) yang
langsung selesai — koneksi browser tidak pernah tertahan.

Pembaca berhenti sendiri bila tidak ada yang meminta frame selama
``idle_timeout`` detik. Format sumber tidak penting: frame dicari dari penanda
JPEG SOI (FF D8) … EOI (FF D9), jadi multipart/x-mixed-replace,
video/x-motion-jpeg, maupun snapshot JPEG tunggal semuanya didukung.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# opener(url) -> (resp_dengan_read1, fungsi_tutup)
Opener = Callable[[str], "tuple[object, Callable[[], None]]"]


def extract_frames(buf: bytearray) -> list[bytes]:
    """Ambil semua frame JPEG utuh dari buffer; sisa data dibiarkan di buffer."""
    frames: list[bytes] = []
    while True:
        start = buf.find(SOI)
        if start < 0:
            # Simpan byte terakhir: bisa jadi separuh penanda SOI.
            del buf[: max(0, len(buf) - 1)]
            return frames
        end = buf.find(EOI, start + 2)
        if end < 0:
            if start:
                del buf[:start]
            return frames
        frames.append(bytes(buf[start : end + 2]))
        del buf[: end + 2]


class _Reader:
    def __init__(self, hub: "MjpegHub", url: str) -> None:
        self.hub = hub
        self.url = url
        self.cond = threading.Condition()
        self.frame: bytes | None = None
        self.seq = 0
        self.frame_at = 0.0
        self.error = ""
        self.last_access = time.time()
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name=f"mjpeg-hub:{url[:60]}", daemon=True)

    def idle(self) -> bool:
        return time.time() - self.last_access > self.hub.idle_timeout

    def _publish(self, frame: bytes) -> None:
        with self.cond:
            self.frame = frame
            self.seq += 1
            self.frame_at = time.time()
            self.error = ""
            self.cond.notify_all()

    def _fail(self, msg: str) -> None:
        with self.cond:
            self.error = msg
            self.cond.notify_all()

    def _run(self) -> None:
        backoff = 1.0
        try:
            while not self.idle() and not self.hub.closed:
                close = None
                try:
                    resp, close = self.hub.opener(self.url)
                    buf = bytearray()
                    got_frame = False
                    while not self.idle() and not self.hub.closed:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break  # sumber menutup (mis. snapshot JPEG tunggal)
                        buf += chunk
                        frames = extract_frames(buf)
                        if frames:
                            got_frame = True
                            backoff = 1.0
                            self._publish(frames[-1])
                        elif len(buf) > self.hub.max_frame_bytes:
                            raise ValueError("Data dari sumber bukan JPEG/MJPEG")
                    if not got_frame:
                        self._fail("Sumber tidak mengirim gambar")
                except Exception as exc:  # jaringan / HTTP / format
                    self._fail(str(exc)[:160] or exc.__class__.__name__)
                finally:
                    if close is not None:
                        try:
                            close()
                        except Exception:
                            pass
                if self.idle() or self.hub.closed:
                    break
                # Sambung ulang: cepat untuk sumber snapshot, mundur bila error.
                time.sleep(min(backoff, 10.0) if self.error else self.hub.snapshot_interval)
                if self.error:
                    backoff = min(backoff * 2, 10.0)
        finally:
            self.stopped = True
            self.hub._forget(self)
            with self.cond:
                self.cond.notify_all()


class MjpegHub:
    def __init__(
        self,
        opener: Opener,
        idle_timeout: float = 30.0,
        max_readers: int = 64,
        max_frame_bytes: int = 8 * 1024 * 1024,
        snapshot_interval: float = 0.5,
    ) -> None:
        self.opener = opener
        self.idle_timeout = idle_timeout
        self.max_readers = max_readers
        self.max_frame_bytes = max_frame_bytes
        self.snapshot_interval = snapshot_interval
        self.closed = False
        self._lock = threading.Lock()
        self._readers: dict[str, _Reader] = {}

    def _forget(self, reader: _Reader) -> None:
        with self._lock:
            if self._readers.get(reader.url) is reader:
                del self._readers[reader.url]

    def _reader_for(self, url: str) -> _Reader:
        with self._lock:
            reader = self._readers.get(url)
            if reader is None or reader.stopped:
                if len(self._readers) >= self.max_readers:
                    raise OverflowError("Terlalu banyak kamera MJPEG aktif sekaligus")
                reader = _Reader(self, url)
                self._readers[url] = reader
                reader.thread.start()
            reader.last_access = time.time()
            return reader

    def get_frame(self, url: str, after_seq: int = 0, wait: float = 2.0, first_wait: float = 12.0):
        """Kembalikan (jpeg|None, seq, error).

        Menunggu frame yang lebih baru dari ``after_seq`` hingga ``wait`` detik
        (atau ``first_wait`` bila belum pernah ada frame). Bila tidak ada frame
        baru, frame terakhir dikembalikan lagi agar klien tetap tampil.
        """
        reader = self._reader_for(url)
        deadline = time.time() + (wait if reader.frame is not None else first_wait)
        with reader.cond:
            while reader.seq <= after_seq and not reader.stopped:
                if reader.frame is None and reader.error and time.time() > deadline - first_wait + 4:
                    break  # gagal jelas & sudah ditunggu sebentar: jangan menahan klien lama
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                reader.cond.wait(min(remaining, 0.5))
                reader.last_access = time.time()
            return reader.frame, reader.seq, reader.error

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_readers": len(self._readers),
                "cameras": [
                    {"url": r.url, "seq": r.seq, "age_s": round(time.time() - r.frame_at, 1) if r.frame_at else None, "error": r.error}
                    for r in self._readers.values()
                ],
            }

    def close(self) -> None:
        self.closed = True

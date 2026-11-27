#!/usr/bin/env python3
"""Diagnosa sumber CCTV Jombang untuk CCTipi System.

Jalankan dari folder cctipi-system (Windows):
    .venv\\Scripts\\python tools\\diag_jombang.py
atau:
    python tools/diag_jombang.py

Hasil dicetak ke layar dan disimpan ke diag_jombang.txt. Kirim isinya ke
pengembang. Isinya hanya data publik dari situs Jombang (token ZoneMinder
publik yang memang tampil di halaman untuk semua pengunjung, berlaku ±2 jam);
tidak ada password, sesi, atau data CCTipi yang ikut terkirim.
"""

from __future__ import annotations

import base64
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PAGE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://cctv.jombangkab.go.id/v2/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
KEYWORDS = re.compile(r"token|login|refresh|nph-zms|zm/api|auth|setInterval|fetch\(|ajax|\$\.get|\$\.post|XMLHttpRequest|data-url|\.src\s*=", re.I)

_ctx = ssl.create_default_context()
try:
    import certifi  # tersedia di .venv CCTipi
    _ctx.load_verify_locations(cafile=certifi.where())
except Exception:
    pass

lines: list[str] = []


def out(text: str = "") -> None:
    print(text)
    lines.append(text)


def get(url: str, referer: str | None = None, limit: int = 2_000_000, timeout: float = 15.0):
    """GET sederhana; kembalikan (status, content_type, bytes, error)."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
        p = urllib.parse.urlsplit(referer)
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = b""
            start = time.time()
            while len(data) < limit and time.time() - start < timeout:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                data += chunk
                if "multipart" in ct.lower() or "jpeg" in ct.lower():
                    if len(data) > 200_000:
                        break
            return resp.status, ct, data, ""
    except urllib.error.HTTPError as exc:
        body = exc.read(600) if hasattr(exc, "read") else b""
        return exc.code, exc.headers.get("Content-Type", ""), body, f"HTTP {exc.code}"
    except Exception as exc:
        return 0, "", b"", f"{exc.__class__.__name__}: {exc}"


def jwt_claims(url: str) -> dict | None:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for vals in q.values():
        for v in vals:
            parts = v.split(".")
            if len(parts) == 3:
                try:
                    pad = "=" * (-len(parts[1]) % 4)
                    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                except Exception:
                    continue
    return None


def fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def snippets(text: str, label: str, max_items: int = 25) -> None:
    hits = 0
    covered = -1
    for m in KEYWORDS.finditer(text):
        if m.start() < covered:
            continue  # sudah tercakup potongan sebelumnya
        a, b = max(0, m.start() - 160), min(len(text), m.end() + 220)
        covered = b
        frag = re.sub(r"\s+", " ", text[a:b]).strip()
        out(f"  [{label}] …{frag}…")
        hits += 1
        if hits >= max_items:
            out(f"  [{label}] (dipotong, masih ada kecocokan lain)")
            break
    if not hits:
        out(f"  [{label}] tidak ada kata kunci token/login/fetch.")


def main() -> None:
    now = time.time()
    out(f"Diagnosa Jombang — {time.strftime('%Y-%m-%d %H:%M:%S')}  (jam server ini)")
    out(f"Halaman: {PAGE_URL}")
    status, ct, data, err = get(PAGE_URL)
    out(f"1) Halaman: status={status} type={ct} ukuran={len(data)} byte {err}")
    if not data:
        out("   Halaman tidak terbaca — cek koneksi server ke cctv.jombangkab.go.id.")
        return
    page = data.decode("utf-8", errors="replace")

    cards = re.findall(r'<div[^>]*class="[^"]*cam-card[^"]*"[^>]*>', page, re.I)
    urls = []
    for tag in cards:
        m = re.search(r'data-url="([^"]+)"', tag, re.I)
        if m:
            urls.append(html.unescape(m.group(1)))
    out(f"2) Kartu kamera: {len(cards)} | data-url: {len(urls)}")
    exp_list = []
    for u in urls:
        c = jwt_claims(u)
        if c and "exp" in c:
            exp_list.append(c)
    if exp_list:
        expired = sum(1 for c in exp_list if c["exp"] < now)
        iats = sorted(c.get("iat", 0) for c in exp_list)
        out(f"   Token JWT: {len(exp_list)} | kedaluwarsa: {expired} | dibuat paling lama {fmt_ts(iats[0])}, paling baru {fmt_ts(iats[-1])}")
        sample = exp_list[0]
        out(f"   Contoh klaim (tanpa tanda tangan): { {k: (fmt_ts(v) if k in ('iat', 'exp') else v) for k, v in sample.items()} }")
    for u in urls[:2]:
        p = urllib.parse.urlsplit(u)
        out(f"   Contoh URL: {p.scheme}://{p.netloc}{p.path}?{p.query[:90]}…")
    other_tokens = re.findall(r"token[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_\-\.]{20,})", page)
    out(f"   Token lain di HTML (di luar data-url): {len(other_tokens)}")

    out("3) Skrip di halaman:")
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.I | re.S)
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page, re.I)
    out(f"   inline: {len(inline)} | eksternal: {len(srcs)}")
    for i, js in enumerate(inline):
        if KEYWORDS.search(js):
            snippets(js, f"inline#{i}")
    for s in srcs:
        full = urllib.parse.urljoin(PAGE_URL, html.unescape(s))
        same_site = urllib.parse.urlsplit(full).hostname == urllib.parse.urlsplit(PAGE_URL).hostname
        out(f"   - {full}{'' if same_site else '  (situs lain, dilewati)'}")
        if same_site:
            st, _, js_bytes, e = get(full, referer=PAGE_URL, limit=800_000)
            if js_bytes:
                snippets(js_bytes.decode("utf-8", errors="replace"), Path(urllib.parse.urlsplit(full).path).name or "js")
            else:
                out(f"     gagal dibaca: {st} {e}")

    out("4) Uji putar satu kamera dengan token dari halaman:")
    if urls:
        st, ctype, body, e = get(urls[0], referer=PAGE_URL, limit=120_000, timeout=10)
        kind = "JPEG/MJPEG" if b"\xff\xd8" in body[:200_000] else "bukan gambar"
        out(f"   status={st} type={ctype} byte={len(body)} isi={kind} {e}")
        if kind != "JPEG/MJPEG" and body:
            out(f"   isi awal: {body[:300].decode('utf-8', errors='replace')!r}")
    else:
        out("   tidak ada URL kamera di halaman.")

    out("5) Endpoint ZoneMinder publik (hanya baca, tanpa login):")
    base = f"{urllib.parse.urlsplit(PAGE_URL).scheme}://{urllib.parse.urlsplit(PAGE_URL).netloc}"
    for path in ("/zm/api/host/getVersion.json", "/zm/api/host/login.json", "/zm/api/monitors.json"):
        st, ctype, body, e = get(base + path, referer=PAGE_URL, limit=4000, timeout=10)
        out(f"   {path}: status={st} type={ctype} isi={body[:200].decode('utf-8', errors='replace')!r} {e}")

    Path("diag_jombang.txt").write_text("\n".join(lines), encoding="utf-8")
    out()
    out("Selesai. Hasil juga tersimpan di diag_jombang.txt — kirim isinya ke pengembang.")


if __name__ == "__main__":
    main()

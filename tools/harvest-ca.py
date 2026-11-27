#!/usr/bin/env python3
"""Ambil intermediate CA yang tidak dikirim server, untuk semua host kamera.

Sebagian server CCTV pemerintah hanya mengirim leaf certificate. Browser
menambal ini sendiri, urllib/OpenSSL tidak — hasilnya
`unable to get local issuer certificate` dan stream dicoba ulang terus.

Skrip ini mengambil URL AIA (CA Issuers) dari tiap leaf, mengunduh
intermediate-nya, menyimpannya ke extras/ca/, lalu memverifikasi ulang
bahwa host benar-benar bisa diverifikasi setelahnya.

Pakai:
    python3 tools/harvest-ca.py            # hanya host yang gagal
    python3 tools/harvest-ca.py --all      # semua host di cameras.json
"""

from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CA_DIR = ROOT / "extras" / "ca"


def hosts_from_cameras() -> list[str]:
    data = json.loads((ROOT / "cameras.json").read_text(encoding="utf-8"))
    cams = data if isinstance(data, list) else data.get("cameras", [])
    hosts = set()
    for cam in cams:
        url = cam.get("url", "")
        if url.startswith("https"):
            host = urllib.parse.urlparse(url).netloc.split(":")[0]
            if host and "youtube" not in host:
                hosts.add(host)
    return sorted(hosts)


def verify(host: str, cafile: str | None = None) -> str:
    """Return '' bila verifikasi lolos, selain itu pesan error."""
    try:
        ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def _decode(pem_path: str) -> dict:
    """Subjek/issuer sertifikat (tanpa paket tambahan)."""
    return ssl._ssl._test_decode_cert(pem_path)  # noqa: SLF001 - API internal stdlib, stabil sejak 3.x


def trusted_intermediate(host: str, pem: str, roots: str | None) -> str:
    """'' bila `pem` adalah intermediate sah: bukan self-signed DAN rantai
    leaf -> pem -> root tepercaya (certifi) lolos TANPA partial chain.
    Mencegah CA palsu (mis. dari MITM di unduhan AIA yang memakai HTTP)
    ikut tersimpan lalu dipercaya untuk semua koneksi HTTPS kamera."""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8") as tmp:
        tmp.write(pem)
        path = tmp.name
    try:
        info = _decode(path)
        if info.get("subject") == info.get("issuer"):
            return "sertifikat self-signed (root), bukan intermediate — ditolak"
        ctx = ssl.create_default_context(cafile=roots) if roots else ssl.create_default_context()
        ctx.load_verify_locations(cafile=path)
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN  # wajib sampai root tepercaya
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:90]
    finally:
        Path(path).unlink(missing_ok=True)


def leaf_der(host: str) -> bytes:
    ctx = ssl._create_unverified_context()  # noqa: S323 - hanya ambil cert, bukan data
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def aia_urls(der: bytes) -> list[str]:
    """Ambil URL CA Issuers dari ekstensi AIA (scan mentah, tanpa pyasn1)."""
    urls = []
    for raw in re.findall(rb"http://[a-zA-Z0-9./_\-]+", der):
        text = raw.decode()
        # DER menyisipkan byte tag di ujung URL (mis. "...CA.crt0"); buang.
        text = re.sub(r"0+$", "", text)
        if text.lower().endswith((".crt", ".cer", ".pem")) or "/ca" in text.lower():
            if text not in urls:
                urls.append(text)
    return urls


def issuer_hint(url: str, der: bytes) -> str:
    """Nama file dari basename URL AIA (paling andal), fallback ke CN issuer."""
    base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if base:
        return re.sub(r"[^A-Za-z0-9]+", "", base.rsplit(".", 1)[0])
    for pattern in (rb"Sectigo[A-Za-z0-9 ]+", rb"DigiCert [A-Za-z0-9 ]+", rb"GlobalSign [A-Za-z0-9 ]+"):
        found = re.findall(pattern, der)
        if found:
            return re.sub(rb"[^A-Za-z0-9]+", b"", found[0]).decode()
    return "intermediate"


def main() -> int:
    only_broken = "--all" not in sys.argv
    CA_DIR.mkdir(parents=True, exist_ok=True)

    # Kumpulan CA yang sudah tersedia: certifi + extras/ca
    try:
        import certifi

        cafiles = [certifi.where()]
    except Exception:
        cafiles = []
    cafiles += [str(p) for p in sorted(CA_DIR.glob("*.pem"))]

    saved = 0
    for host in hosts_from_cameras():
        err = verify(host, cafiles[0] if cafiles else None)
        if not err:
            print(f"[ok]    {host}")
            continue
        if "unable to get local issuer" not in err and "CERTIFICATE_VERIFY_FAILED" not in err:
            print(f"[skip]  {host} — {err.split(':')[-1].strip()[:60]}")
            continue

        print(f"[chain] {host} — intermediate hilang, mengambil dari AIA…")
        try:
            der = leaf_der(host)
        except Exception as exc:  # noqa: BLE001
            print(f"        gagal baca leaf: {str(exc)[:80]}")
            continue

        urls = aia_urls(der)
        if not urls:
            print("        tidak ada URL AIA di sertifikat")
            continue

        name = issuer_hint(urls[0], der)
        out = CA_DIR / f"{name}.pem"
        if out.exists() and out.stat().st_size > 500:
            print(f"        {out.name} sudah ada")
            continue

        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
                    body = resp.read()
            except Exception as exc:  # noqa: BLE001
                print(f"        unduh gagal {url}: {str(exc)[:60]}")
                continue
            if len(body) < 500:
                print(f"        {url} mengembalikan {len(body)} B — bukan sertifikat, dilewati")
                continue
            pem = (
                ssl.DER_cert_to_PEM_cert(body)
                if body[:1] == b"\x30"
                else body.decode("utf-8", "replace")
            )
            try:
                ssl.create_default_context(cafile=None).load_verify_locations(cadata=pem)
            except Exception as exc:  # noqa: BLE001
                print(f"        PEM tidak valid dari {url}: {str(exc)[:60]}")
                continue
            problem = trusted_intermediate(host, pem, cafiles[0] if cafiles else None)
            if problem:
                print(f"        DITOLAK — tidak tersambung ke root tepercaya: {problem}")
                continue
            out.write_text(pem, encoding="utf-8")
            saved += 1
            print(f"        tersimpan: {out.name} ({len(pem)} B)")
            break

        again = verify(host, str(out)) if out.exists() else "no chain"
        print(f"        verifikasi ulang: {'OK' if not again else again[:70]}")

    print(f"\nSelesai. {saved} intermediate baru di {CA_DIR}")
    print("Restart layanan agar konteks TLS dimuat ulang:  cctipi restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
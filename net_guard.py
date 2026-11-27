#!/usr/bin/env python3
"""Proteksi SSRF tingkat koneksi: cek IP hasil resolve DNS + redirect aman.

Validasi hostname saja (camera_store.validate_stream_host) tidak cukup karena
hostname publik bisa me-resolve ke 127.0.0.1 / 169.254.169.254 / IP LAN
(mis. ``127.0.0.1.nip.io``) atau berubah lewat DNS rebinding. Modul ini:

- me-resolve host tepat sebelum tersambung, menolak bila ADA alamat terlarang,
  lalu tersambung langsung ke IP yang sudah diperiksa (anti DNS rebinding);
- menyediakan koneksi http.client & handler urllib yang memakai pemeriksaan itu
  di SETIAP hop (termasuk sesudah redirect);
- membatasi redirect hanya ke http/https.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import sys
import urllib.parse
import urllib.request

_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Hanya untuk unit test end-to-end yang memakai upstream palsu di 127.0.0.1.
_TEST_ALLOW_LOOPBACK = False


class UnsafeAddressError(ValueError):
    """Host me-resolve ke alamat yang dilarang (SSRF)."""


def private_allowed() -> bool:
    store = sys.modules.get("camera_store")
    if store is not None and getattr(store, "ALLOW_PRIVATE_CAMERA_HOSTS", False):
        return True
    return os.environ.get("CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS", "").lower() in ("1", "true", "yes")


def ip_block_reason(ip_text: str, *, allow_private: bool | None = None) -> str:
    """'' bila IP boleh dihubungi, selain itu alasan penolakan."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return "alamat IP tidak valid"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if _TEST_ALLOW_LOOPBACK and ip.is_loopback:
        return ""
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "loopback/link-local/reserved"
    if allow_private is None:
        allow_private = private_allowed()
    if (ip.is_private or (ip.version == 4 and ip in _CGNAT)) and not allow_private:
        return "jaringan privat"
    return ""


def resolve_safe(host: str, port: int, *, allow_private: bool | None = None) -> list[tuple]:
    """Resolve host & pastikan SEMUA alamatnya aman. Kembalikan hasil getaddrinfo."""
    if not host:
        raise UnsafeAddressError("Host tujuan kosong.")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"Gagal me-resolve host '{host}': {exc}") from exc
    if not infos:
        raise OSError(f"Host '{host}' tidak memiliki alamat.")
    for info in infos:
        reason = ip_block_reason(info[4][0], allow_private=allow_private)
        if reason:
            raise UnsafeAddressError(
                f"Host '{host}' me-resolve ke {info[4][0]} ({reason}) — ditolak demi keamanan SSRF."
            )
    return infos


def check_url_host(url: str, *, allow_private: bool | None = None) -> None:
    """Pemeriksaan awal untuk URL yang dibuka proses lain (ffmpeg/ffprobe)."""
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or {"https": 443, "rtsp": 554, "rtsps": 322}.get(parsed.scheme, 80)
    resolve_safe(parsed.hostname or "", port, allow_private=allow_private)


def _connect_checked(host: str, port: int, timeout, source_address=None) -> socket.socket:
    last_exc: Exception | None = None
    for family, socktype, proto, _canon, addr in resolve_safe(host, port):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            return sock
        except OSError as exc:
            last_exc = exc
            sock.close()
    raise last_exc or OSError(f"Tidak dapat tersambung ke {host}:{port}")


class SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_checked(self.host, self.port, self.timeout, self.source_address)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = _connect_checked(self.host, self.port, self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext | None = None):
        super().__init__(context=context)
        self._ctx = context

    def https_open(self, req):
        return self.do_open(SafeHTTPSConnection, req, context=self._ctx)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise UnsafeAddressError(f"Redirect ke skema tidak diizinkan: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_safe_opener(*extra_handlers, context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _SafeHTTPHandler(), _SafeHTTPSHandler(context), _SafeRedirectHandler(), *extra_handlers
    )


def safe_urlopen(req, *, timeout: float, context: ssl.SSLContext | None = None):
    return build_safe_opener(context=context).open(req, timeout=timeout)

#!/usr/bin/env python3
"""Regional CCTV camera scrapers, HTML/JSON parsers, and token-expiry helpers."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import net_guard

_ssl_ctx = ssl.create_default_context()
try:
    import certifi as _certifi
    _ssl_ctx.load_verify_locations(cafile=_certifi.where())
except Exception:
    pass
try:
    from pathlib import Path as _Path
    for _extra_ca in sorted((_Path(__file__).resolve().parent / "extras" / "ca").glob("*.pem")):
        _ssl_ctx.load_verify_locations(cafile=str(_extra_ca))
except Exception:
    pass



def _urlopen(req, *, timeout: float):
    """GET sumber scraper lewat net_guard: IP hasil DNS & setiap redirect diperiksa (anti SSRF)."""
    return net_guard.safe_urlopen(req, timeout=timeout, context=_ssl_ctx)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

SAMUDRA_CCTV_URL = "https://samudra.jepara.go.id/cctv"
KUDUS_CCTV_API_URL = "https://kudussehat.kuduskab.go.id/api/get-cctv"
KUDUS_API_KEY = os.environ.get("CCTIPI_KUDUS_API_KEY", "")
DEMAK_YOUTUBE_URL = "https://www.youtube.com/@cctvdishubdemak/streams"
JOMBANG_CCTV_URL = "https://cctv.jombangkab.go.id/v2/"
TOLL_CCTV_URL = "https://binamarga.pu.go.id/contents/cctv_tol/?id_ruas=kertosono-mojokerto"
JOGJA_CCTV_API_URL = "https://cctv.jogjakota.go.id/home/getdata"
SEMARANG_CCTV_URL = "https://pantausemar.semarangkota.go.id/"
PATI_CCTV_URL = "https://cctvkabpati.biz.id/"
REMBANG_CCTV_API_URL = "https://smartcity.rembangkab.go.id/api/v1/cctv"
SOLO_CCTV_URL = "https://ccroom-dishub.surakarta.go.id/cctv"


def _extract_balanced_json(text: str, start_idx: int) -> str | None:
    """Ambil literal objek/array JSON seimbang mulai dari '{' atau '[' pada start_idx."""
    if start_idx < 0 or start_idx >= len(text):
        return None
    opener = text[start_idx]
    closer = "}" if opener == "{" else ("]" if opener == "[" else "")
    if not closer:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    return None


# ---------- 1. Jepara (HTMLParser) ----------

class _SelectOptionParser(HTMLParser):
    def __init__(self, target_select_id: str = "pilih-cctv") -> None:
        super().__init__()
        self.target_id = target_select_id.lower()
        self.in_target_select = False
        self.found_target_select = False
        self.current_val: str | None = None
        self.current_text: list[str] = []
        self.target_options: list[tuple[str, str]] = []
        self.all_options: list[tuple[str, str]] = []
        self.data_attrs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()
        if t == "select" and ad.get("id", "").lower() == self.target_id:
            self.in_target_select = True
            self.found_target_select = True
        elif t == "option":
            self.current_val = ad.get("value", "")
            self.current_text = []
        url = ad.get("data-url") or ad.get("data-stream") or ad.get("data-src") or ""
        name = ad.get("data-name") or ad.get("data-title") or ad.get("title") or ""
        if url.startswith("http") and name:
            self.data_attrs.append((url, name))

    def handle_data(self, data: str) -> None:
        if self.current_val is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "option" and self.current_val is not None:
            pair = (self.current_val, "".join(self.current_text))
            self.all_options.append(pair)
            if self.in_target_select:
                self.target_options.append(pair)
            self.current_val = None
            self.current_text = []
        elif t == "select" and self.in_target_select:
            self.in_target_select = False


def parse_cameras_from_html(page_html: str) -> list[dict]:
    """Extract Jepara cameras using stdlib HTMLParser (tolerates markup changes)."""
    parser = _SelectOptionParser("pilih-cctv")
    parser.feed(page_html or "")
    candidates = parser.target_options if parser.found_target_select else parser.all_options
    if not candidates and parser.data_attrs:
        candidates = parser.data_attrs
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for raw_url, raw_name in candidates:
        url = html.unescape(raw_url).strip()
        if not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)
        name = html.unescape(re.sub(r"\s+", " ", raw_name).strip()) or f"Jepara #{len(cameras) + 1}"
        provider = "lifemedia" if "lifemedia.id" in url else "diskominfo"
        cameras.append({
            "id": len(cameras) + 1,
            "name": name,
            "url": url,
            "provider": provider,
            "folder": "Jepara",
        })
    return cameras


parse_jepara_html = parse_cameras_from_html


def fetch_jepara_cameras() -> list[dict]:
    req = urllib.request.Request(SAMUDRA_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=12) as resp:
        page_html = resp.read().decode("utf-8", errors="replace")
    return parse_cameras_from_html(page_html)


# ---------- 2. Kudus (JSON) ----------

def parse_kudus_json(data: dict | list | str, start_id: int = 1) -> list[dict]:
    if isinstance(data, str):
        data = json.loads(data)
    items = (
        data.get("camera") or data.get("data") or []
        if isinstance(data, dict)
        else (data if isinstance(data, list) else [])
    )
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or item.get("stream_url") or "").strip()
        name = re.sub(r"\s+", " ", (item.get("nama") or item.get("name") or "").strip())
        if not url.startswith("http") or not name or url in seen_urls:
            continue
        seen_urls.add(url)
        cam_obj: dict = {
            "id": start_id + len(cameras),
            "name": name,
            "url": url,
            "provider": "kuduskab",
            "folder": "Kudus",
        }
        try:
            lat_v = item.get("lat")
            lng_v = item.get("long") if item.get("long") is not None else item.get("lng")
            if lat_v and lng_v:
                cam_obj["lat"] = float(lat_v)
                cam_obj["lng"] = float(lng_v)
        except (TypeError, ValueError):
            pass
        cameras.append(cam_obj)
    return cameras


def fetch_kudus_cameras(start_id: int = 1) -> list[dict]:
    api_key = os.environ.get("CCTIPI_KUDUS_API_KEY", KUDUS_API_KEY)
    req = urllib.request.Request(
        KUDUS_CCTV_API_URL,
        headers={
            "User-Agent": USER_AGENT,
            "X-SDC": api_key,
            "Origin": "https://streaming.kuduskab.go.id",
            "Referer": "https://streaming.kuduskab.go.id/",
        },
    )
    with _urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_kudus_json(raw, start_id=start_id)


# ---------- 3. Demak (YouTube HTML) ----------

def parse_demak_html(html_text: str, start_id: int = 1) -> list[dict]:
    m = re.search(r"ytInitialData\s*=\s*\{", html_text or "")
    if not m:
        return []
    brace_pos = html_text.find("{", m.start())
    blob = _extract_balanced_json(html_text, brace_pos)
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except ValueError:
        return []

    cameras: list[dict] = []
    seen_ids: set[str] = set()

    def clean_title(raw: str) -> str:
        text = re.sub(r"^🔴\s*\[LIVE CCTV\]\s*", "", raw, flags=re.IGNORECASE)
        text = re.sub(r"\s+by\s+DISHUB\s+KAB\.\s*DEMAK\s*$", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def add_vid(vid: str | None, title: str) -> None:
        if vid and title and vid not in seen_ids:
            seen_ids.add(vid)
            cameras.append({
                "id": start_id + len(cameras),
                "name": clean_title(title),
                "url": f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&rel=0",
                "videoId": vid,
                "type": "youtube",
                "provider": "youtube",
                "folder": "Demak",
            })

    def walk(obj) -> None:
        if isinstance(obj, dict):
            if "lockupViewModel" in obj:
                vm = obj["lockupViewModel"]
                title = (
                    vm.get("metadata", {})
                    .get("lockupMetadataViewModel", {})
                    .get("title", {})
                    .get("content", "")
                    .strip()
                )
                add_vid(vm.get("contentId"), title)
            for rkey in ("videoRenderer", "gridVideoRenderer"):
                if rkey in obj and isinstance(obj[rkey], dict):
                    vr = obj[rkey]
                    vid = vr.get("videoId")
                    t_obj = vr.get("title") or {}
                    title = t_obj.get("simpleText") or "".join(
                        r.get("text", "") for r in (t_obj.get("runs") or []) if isinstance(r, dict)
                    )
                    add_vid(vid, title.strip())
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return cameras


def fetch_demak_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(
        DEMAK_YOUTUBE_URL,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_demak_html(html_text, start_id=start_id)


# ---------- 4. Jombang (HTMLParser + cctvData JSON fallback) ----------

class _CardAttrParser(HTMLParser):
    """Parser atribut kartu kamera generik (Jombang & Solo)."""

    def __init__(self, url_attrs: tuple[str, ...], name_attrs: tuple[str, ...]) -> None:
        super().__init__()
        self.url_attrs = url_attrs
        self.name_attrs = name_attrs
        self.items: list[tuple[str, str]] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        url = next((ad[a] for a in self.url_attrs if ad.get(a)), "").strip()
        name = next((ad[a] for a in self.name_attrs if ad.get(a)), "").strip()
        if url and name:
            self.items.append((url, name))


def _parse_jombang_cctv_data(html_text: str) -> dict[str, dict]:
    m = re.search(r"cctvData\s*=\s*\[", html_text or "")
    if not m:
        return {}
    arr_pos = html_text.find("[", m.start())
    blob = _extract_balanced_json(html_text, arr_pos)
    if not blob:
        return {}
    try:
        items = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    out: dict[str, dict] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith("http") or url in out:
            continue
        coords: dict[str, float] = {}
        try:
            lat, lng = float(item.get("lat")), float(item.get("lng"))
            if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng):
                coords = {"lat": lat, "lng": lng}
        except (TypeError, ValueError):
            pass
        out[url] = {"name": html.unescape(str(item.get("nama") or item.get("name") or "")).strip(), "coords": coords}
    return out


def parse_jombang_html(html_text: str, start_id: int = 1) -> list[dict]:
    meta = _parse_jombang_cctv_data(html_text or "")
    parser = _CardAttrParser(("data-url", "data-stream-url", "data-src"), ("data-cam-name", "data-name", "title"))
    parser.feed(html_text or "")

    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for raw_url, raw_name in parser.items:
        url = html.unescape(raw_url).strip()
        name = html.unescape(re.sub(r"\s+", " ", raw_name).strip())
        if not url.startswith("http") or not name or url in seen_urls:
            continue
        seen_urls.add(url)
        cam: dict = {
            "id": start_id + len(cameras),
            "name": name,
            "url": url,
            "type": "mjpeg",
            "provider": "jombangkab",
            "folder": "Jombang",
        }
        cam.update(meta.get(url, {}).get("coords", {}))
        cameras.append(cam)

    if not cameras:
        for url, item in meta.items():
            if not item.get("name"):
                continue
            cam = {
                "id": start_id + len(cameras),
                "name": item["name"],
                "url": url,
                "type": "mjpeg",
                "provider": "jombangkab",
                "folder": "Jombang",
            }
            cam.update(item.get("coords", {}))
            cameras.append(cam)
    return cameras


def fetch_jombang_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(JOMBANG_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_jombang_html(html_text, start_id=start_id)


# ---------- 5. Tol Bina Marga (HTMLParser) ----------

class _TollHtmlParser(HTMLParser):
    """Pasangkan <source src='...m3u8'> / <video src='...m3u8'> dengan heading terdekat."""

    HEADINGS = {"h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self.pending_url: str | None = None
        self.last_heading: str = ""
        self.in_heading: str | None = None
        self.heading_buf: list[str] = []
        self.pairs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        ad = {k.lower(): (v or "") for k, v in attrs}
        if t in ("source", "video"):
            src = (ad.get("src") or ad.get("data-src") or "").strip()
            if ".m3u8" in src.lower():
                title_attr = (ad.get("data-title") or ad.get("title") or "").strip()
                if title_attr:
                    self.pairs.append((src, title_attr))
                    self.pending_url = None
                else:
                    if self.pending_url and self.last_heading:
                        self.pairs.append((self.pending_url, self.last_heading))
                        self.last_heading = ""
                    self.pending_url = src
        elif t in self.HEADINGS:
            self.in_heading = t
            self.heading_buf = []

    def handle_data(self, data: str) -> None:
        if self.in_heading is not None:
            self.heading_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_heading and tag.lower() == self.in_heading:
            text = re.sub(r"\s+", " ", "".join(self.heading_buf)).strip()
            self.in_heading = None
            self.heading_buf = []
            if text:
                if self.pending_url:
                    self.pairs.append((self.pending_url, text))
                    self.pending_url = None
                else:
                    self.last_heading = text


def parse_toll_html(html_text: str, start_id: int = 1) -> list[dict]:
    parser = _TollHtmlParser()
    parser.feed(html_text or "")
    if parser.pending_url and parser.last_heading:
        parser.pairs.append((parser.pending_url, parser.last_heading))
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for raw_url, raw_title in parser.pairs:
        url = html.unescape(raw_url).strip()
        name = html.unescape(re.sub(r"\s+", " ", raw_title).strip())
        if not url.startswith("http") or not name or url in seen_urls:
            continue
        seen_urls.add(url)
        cameras.append({
            "id": start_id + len(cameras),
            "name": name,
            "url": url,
            "provider": "tol",
            "folder": "Tol",
        })
    return cameras


def fetch_toll_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(TOLL_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_toll_html(html_text, start_id=start_id)


# ---------- 6. Jogja (JSON) ----------

def parse_jogja_json(data: list | dict | str, start_id: int = 1) -> list[dict]:
    if isinstance(data, str):
        data = json.loads(data)
    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("cctv_status", "0")) != "0":
            continue
        url = (item.get("cctv_link") or item.get("url") or "").strip()
        name = re.sub(r"\s+", " ", (item.get("cctv_title") or item.get("name") or "").strip())
        if not url.startswith("http") or not name or url in seen_urls:
            continue
        seen_urls.add(url)
        cam_obj: dict = {
            "id": start_id + len(cameras),
            "name": name,
            "url": url,
            "provider": "jogja",
            "folder": "Jogja",
        }
        try:
            if item.get("cctv_latitude") and item.get("cctv_longitude"):
                cam_obj["lat"] = float(item["cctv_latitude"])
                cam_obj["lng"] = float(item["cctv_longitude"])
        except (TypeError, ValueError):
            pass
        cameras.append(cam_obj)
    return cameras


def fetch_jogja_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(
        JOGJA_CCTV_API_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://cctv.jogjakota.go.id/",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with _urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_jogja_json(raw, start_id=start_id)


# ---------- 7. Semarang (HTML embedded JSON) ----------

def parse_semarang_html(html_text: str, start_id: int = 1) -> list[dict]:
    m = re.search(r'\[\s*\{\s*"cctv_id"\s*:', html_text or "")
    if not m:
        return []
    blob = _extract_balanced_json(html_text, m.start())
    if not blob:
        return []
    try:
        arr = json.loads(blob)
    except ValueError:
        return []
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for group in arr if isinstance(arr, list) else []:
        if not isinstance(group, dict):
            continue
        group_name = re.sub(r"\s+", " ", (group.get("owner_name") or "").strip())
        for lk in group.get("links") or group.get("cctv_link") or []:
            if not isinstance(lk, dict):
                continue
            url = (lk.get("url") or lk.get("link") or "").strip()
            name = re.sub(r"\s+", " ", (lk.get("name") or group_name).strip())
            if not url.startswith("http") or not name or url in seen_urls:
                continue
            seen_urls.add(url)
            cam_obj: dict = {
                "id": start_id + len(cameras),
                "name": name,
                "url": url,
                "provider": "semarang",
                "folder": "Semarang",
            }
            try:
                if group.get("lat") and group.get("lng"):
                    cam_obj["lat"] = float(group["lat"])
                    cam_obj["lng"] = float(group["lng"])
            except (TypeError, ValueError):
                pass
            cameras.append(cam_obj)
    return cameras


def fetch_semarang_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(SEMARANG_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_semarang_html(html_text, start_id=start_id)


# ---------- 8. Pati (HTML / JS object array) ----------

def _pati_to_hls(raw_url: str) -> str:
    u = raw_url.strip()
    m_restream = re.match(r"^(https?://streaming\.patikab\.go\.id)/([a-f0-9-]+)\.html$", u, re.I)
    if m_restream:
        return f"{m_restream.group(1)}/memfs/{m_restream.group(2)}.m3u8"
    m_ant = re.match(r"^(https?://[^/]+/[^/]+)/play\.html\?name=([a-zA-Z0-9_-]+)$", u)
    if m_ant:
        return f"{m_ant.group(1)}/streams/{m_ant.group(2)}.m3u8"
    return u


def parse_pati_html(html_text: str, start_id: int = 1) -> list[dict]:
    # Mendukung kutip tunggal/ganda & urutan nama/url terbalik di dalam blok objek JS `{...}`
    pairs: list[tuple[str, str]] = []
    for obj_match in re.finditer(r"\{[^{}]{10,600}\}", html_text or ""):
        chunk = obj_match.group(0)
        nm = re.search(r"""(?:nama|name)\s*:\s*(['"])(.*?)\1""", chunk, re.S)
        um = re.search(r"""url\s*:\s*(['"])(https?://.*?)\1""", chunk, re.S)
        if nm and um:
            pairs.append((nm.group(2), um.group(2)))

    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for raw_name, raw_url in pairs:
        name = re.sub(r"\s+", " ", html.unescape(raw_name).strip())
        hls_url = _pati_to_hls(html.unescape(raw_url))
        if not hls_url.startswith("http") or not name or hls_url in seen_urls:
            continue
        seen_urls.add(hls_url)
        cameras.append({
            "id": start_id + len(cameras),
            "name": name,
            "url": hls_url,
            "provider": "pati",
            "folder": "Pati",
        })

    # Hanya tambahkan ekstra A7 bila halaman Pati memang berhasil di-parse (agar bila situs
    # berubah total -> mengembalikan [] sehingga fallback cache wilayah terpakai & tercatat di log).
    if cameras:
        extra_url = "https://streaming.patikab.go.id/memfs/598ad944-6057-4c75-8d39-4b2aaf5bf3d6.m3u8"
        if extra_url not in seen_urls:
            cameras.append({
                "id": start_id + len(cameras),
                "name": "Dishub Pati (A7)",
                "url": extra_url,
                "provider": "pati",
                "folder": "Pati",
            })
    return cameras


def fetch_pati_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(PATI_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_pati_html(html_text, start_id=start_id)


# ---------- 9. Rembang (JSON) ----------

def parse_rembang_json(data: dict | list | str, start_id: int = 1) -> list[dict]:
    if isinstance(data, str):
        data = json.loads(data)
    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("is_active", 1):
            continue
        name = re.sub(r"\s+", " ", (item.get("name") or item.get("nama") or "").strip())
        img_url = (item.get("image_preview") or "").strip()
        direct_hls = (item.get("stream_url") or item.get("hls_url") or "").strip()
        vid = (item.get("youtube_broadcast_id") or "").strip()
        if not name:
            continue

        if direct_hls.startswith("http"):
            cam_url, cam_type = direct_hls, "hls"
        elif img_url.startswith("http") and "/images/live.jpg" in img_url:
            cam_url = re.sub(r"/images/live\.jpg.*$", "/hls/live.stream.m3u8", img_url)
            cam_type = "hls"
        elif vid:
            cam_url = f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&rel=0"
            cam_type = "youtube"
        else:
            continue

        if cam_url in seen_urls:
            continue
        seen_urls.add(cam_url)
        cam_entry: dict = {
            "id": start_id + len(cameras),
            "name": name,
            "url": cam_url,
            "type": cam_type,
            "provider": "rembang",
            "folder": "Rembang",
        }
        if vid:
            cam_entry["videoId"] = vid
        try:
            coord = (item.get("coordinate") or "").split(",")
            if len(coord) == 2:
                cam_entry["lat"] = float(coord[0].strip())
                cam_entry["lng"] = float(coord[1].strip())
        except (TypeError, ValueError):
            pass
        cameras.append(cam_entry)
    return cameras


def fetch_rembang_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(
        REMBANG_CCTV_API_URL,
        headers={"User-Agent": USER_AGENT, "Referer": "https://cctv.rembangkab.go.id/"},
    )
    with _urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_rembang_json(raw, start_id=start_id)


# ---------- 10. Solo (HTMLParser) ----------

def parse_solo_cameras_from_html(html_text: str, start_id: int = 1) -> list[dict]:
    """Parse camera list from ccroom-dishub.surakarta.go.id/cctv HTML using HTMLParser."""
    parser = _CardAttrParser(("data-stream-url", "data-url", "data-src"), ("data-name", "data-title", "title"))
    parser.feed(html_text or "")
    cameras: list[dict] = []
    seen_urls: set[str] = set()
    for raw_url, raw_name in parser.items:
        url = html.unescape(raw_url).strip()
        name = html.unescape(re.sub(r"\s+", " ", raw_name).strip())
        if not url.startswith("http") or not name or url in seen_urls:
            continue
        seen_urls.add(url)
        cameras.append({
            "id": start_id + len(cameras),
            "name": name,
            "url": url,
            "type": "flv",
            "provider": "solo",
            "folder": "Solo",
        })
    return cameras


def fetch_solo_cameras(start_id: int = 1) -> list[dict]:
    req = urllib.request.Request(SOLO_CCTV_URL, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")
    return parse_solo_cameras_from_html(html_text, start_id=start_id)


# ---------- Custom Region & Token Helpers ----------

def fetch_custom_region_cameras(src: dict) -> list[dict]:
    folder = (src.get("folder") or "Custom").strip()
    api_url = (src.get("apiUrl") or "").strip()
    fmt = (src.get("format") or "").lower()
    if not api_url.startswith(("http://", "https://")):
        return []
    if api_url.split("?")[0].lower().endswith(".m3u8"):
        return [{"name": src.get("title") or folder, "url": api_url, "type": "hls", "provider": "manual", "folder": folder}]
    if "mjpeg" in fmt:
        return [{"name": src.get("title") or folder, "url": api_url, "type": "mjpeg", "provider": "manual", "folder": folder}]

    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=12) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    m3u8s = list(dict.fromkeys(re.findall(r'https?://[^"\'\s<>\\]+\.m3u8[^"\'\s<>\\]*', text)))
    return [
        {"name": f"{folder} #{idx + 1}", "url": u, "type": "hls", "provider": "manual", "folder": folder}
        for idx, u in enumerate(m3u8s)
    ]


BUILTIN_FETCHERS = {
    "Jepara": fetch_jepara_cameras,
    "Kudus": fetch_kudus_cameras,
    "Demak": fetch_demak_cameras,
    "Jombang": fetch_jombang_cameras,
    "Tol": fetch_toll_cameras,
    "Jogja": fetch_jogja_cameras,
    "Semarang": fetch_semarang_cameras,
    "Pati": fetch_pati_cameras,
    "Rembang": fetch_rembang_cameras,
    "Solo": fetch_solo_cameras,
}


def jwt_expiry(url: str) -> int | None:
    """Ambil klaim `exp` dari token JWT di query URL (tanpa verifikasi)."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:
        return None
    for name in ("token", "jwt", "access_token"):
        for value in query.get(name, []):
            parts = value.split(".")
            if len(parts) != 3:
                continue
            try:
                pad = "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                return int(claims.get("exp"))
            except Exception:
                continue
    return None


def mark_token_expiry(cams: list[dict], now: float) -> list[dict]:
    out = []
    for cam in cams:
        exp = jwt_expiry(cam.get("url", ""))
        if exp is not None and exp < now:
            cam = {**cam, "tokenExpired": True}
        out.append(cam)
    return out

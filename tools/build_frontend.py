#!/usr/bin/env python3
"""Utilitas build, pemisahan CSS, validasi sintaks, dan minifikasi frontend CCTipi.

Tanpa dependensi eksternal (murni pustaka standar Python):
- `--split-legacy` : Gabung `styles.css` + `merge.css`, buang selector purwarupa mati &
                     aturan duplikat, lalu pecah ke `styles.css`, `css/pantau.css`, `css/pages.css`.
- `--check`        : Validasi referensi aset HTML, keseimbangan kurung CSS/JS, dan ketiadaan `merge.css`.
- `--minify`       : Hasilkan bundel produksi terkompresi di `public/dist/bundle.min.{css,js}`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT_DIR / "public"

CSS_SOURCES = [
    PUBLIC_DIR / "styles.css",
    PUBLIC_DIR / "css" / "pantau.css",
    PUBLIC_DIR / "css" / "pages.css",
]

JS_SOURCES = [
    PUBLIC_DIR / "js" / "core.js",
    PUBLIC_DIR / "js" / "pantau.js",
    PUBLIC_DIR / "js" / "rekaman.js",
    PUBLIC_DIR / "js" / "jepretan.js",
    PUBLIC_DIR / "js" / "pengaturan.js",
    PUBLIC_DIR / "app.js",
]

# Selector purwarupa lama yang sudah dipastikan tidak dipakai di HTML maupun JS aktif
DEAD_SELECTOR_PATTERNS = (
    r"\.demo-canvas\b",
    r"canvas\.sim\b",
    r"\.demo-chip\b",
    r"\.map-pin\b",
    r"\.map-label\b",
    r"\.map-pop\b(?!up)",
    r"\.slot-num\b",
    r"\.player-screen\b",
    r"\.player-time\b",
    r"\.modal-body\b",
    r"\.recordings-list\b",
    r"\.rec-card(?:-[a-z]+)?\b",
    r"\.rec-thumb\b",
    r"\.playback-pane\b",
    r"\.playback-empty\b",
    r"\.playback-player-wrap\b",
    r"\.playback-meta(?:-text)?\b",
    r"\.playback-info\b",
    r"\.playback-controls-extra\b",
    r"\.settings-modal-dialog\b",
    r"\.settings-body\b",
    r"\.settings-section\b",
    r"\.section-title\b",
    r"\.section-actions\b",
    r"\.sources-list\b",
    r"\.manual-cam-dialog\b",
    r"\.manual-cam-body\b",
    r"\.onvif-box\b",
    r"\.onvif-actions\b",
    r"\.onvif-profiles-list\b",
    r"\.onvif-profile-btn\b",
)
_DEAD_RE = re.compile("|".join(f"(?:{p})" for p in DEAD_SELECTOR_PATTERNS))

_PAGES_HINTS = (
    ".page-view", ".page-intro", ".page-tabs", ".page-tab", ".tab-count",
    ".filter-bar", ".srec-", ".rec-ico", ".rec-mini-btn", ".usage-meter",
    ".mode-chip", ".playback-dialog", ".player", ".jp-", ".cat-chip",
    ".src-chip", ".dets", ".det ", ".det{", ".cls-", ".obj-chip", ".optin-",
    ".ai-status", ".manual-", ".onvif-prof", ".source-card", ".src-row-grid",
    ".health-", ".user-list", ".user-row", ".you-chip", ".role-chip",
    ".perm-", ".uf-", ".pw-row", ".seg-row", ".zone-", ".disk-", ".audit-",
    "barGrow",
)

_PANTAU_HINTS = (
    ".home", ".is-home", ".hero-", ".stat-strip", ".stat-card", ".stat-txt",
    ".preview-", ".mini-tile", ".mini-label", ".region-", ".cctv-grid",
    ".cam-tile", ".tile-", ".media-host", ".mjpeg-view", ".live-pill",
    ".rec-badge", ".ptz-", ".ai-badge", ".slot-tools", ".empty-slot",
    ".edit-help", "body.edit-mode", "data-view='wall'", 'data-view="wall"',
    "data-page='pantau'", ".tv-wall", "body.tv-wall-mode", ".map-view",
    ".leaflet-host", ".map-legend", ".map-popup", ".layout-bar",
    ".edit-toggle", ".fill-group", ".layout-preset-group",
    "live-ring", "tile-sheen", "rec-border", "radar",
)


def strip_css_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                out.append(text[i])
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_css_blocks(css_text: str) -> list[tuple[str, str]]:
    """Urai level atas CSS menjadi daftar `(prelude, body)`."""
    clean = strip_css_comments(css_text)
    blocks: list[tuple[str, str]] = []
    i, n = 0, len(clean)
    start = 0
    depth = 0
    brace_open = -1
    quote = ""
    while i < n:
        ch = clean[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            if depth == 0:
                brace_open = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and brace_open >= 0:
                prelude = clean[start:brace_open].strip()
                body = clean[brace_open + 1 : i].strip()
                if prelude:
                    blocks.append((prelude, body))
                start = i + 1
                brace_open = -1
        elif ch == ";" and depth == 0:
            stmt = clean[start : i + 1].strip()
            if stmt:
                blocks.append((stmt, ""))
            start = i + 1
        i += 1
    return blocks


def _split_selector_list(prelude: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote = ""
    for ch in prelude:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            s = "".join(cur).strip()
            if s:
                parts.append(s)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _filter_dead_selectors(prelude: str) -> str:
    if prelude.startswith("@"):
        return prelude
    kept = [s for s in _split_selector_list(prelude) if not _DEAD_RE.search(s)]
    return ", ".join(kept)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_decls(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    depth = 0
    quote = ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == ";" and depth == 0:
            raw = "".join(cur).strip()
            if ":" in raw:
                k, v = raw.split(":", 1)
                out.append((k.strip().lower(), _norm_ws(v)))
            cur = []
        else:
            cur.append(ch)
    raw = "".join(cur).strip()
    if ":" in raw:
        k, v = raw.split(":", 1)
        out.append((k.strip().lower(), _norm_ws(v)))
    return out


def _format_decls(decls: list[tuple[str, str]], indent: str = "  ") -> str:
    # Pertahankan deklarasi terakhir untuk properti yang sama dalam satu blok
    dedup: dict[str, str] = {}
    for k, v in decls:
        dedup[k] = v
    return "\n".join(f"{indent}{k}: {v};" for k, v in dedup.items())


def _classify_rule(selector: str) -> str:
    s = _norm_ws(selector)
    if s == ".page" or s.startswith(".page ") or s.startswith(".page,"):
        return "pages"
    if any(h in s for h in _PAGES_HINTS):
        return "pages"
    if any(h in s for h in _PANTAU_HINTS):
        return "pantau"
    return "base"


def split_legacy_css() -> dict[str, int]:
    """Pecah & dedup `styles.css` + `merge.css` menjadi 3 berkas modular."""
    merge_path = PUBLIC_DIR / "merge.css"
    styles_path = PUBLIC_DIR / "styles.css"
    if not merge_path.is_file():
        raise FileNotFoundError("public/merge.css sudah dihapus; --split-legacy hanya untuk migrasi awal.")

    combined = styles_path.read_text(encoding="utf-8") + "\n" + merge_path.read_text(encoding="utf-8")
    raw_blocks = parse_css_blocks(combined)

    # Flatten ke item: (media_prelude | "", selector_or_at, body_str, is_keyframes)
    items: list[tuple[str, str, str, bool]] = []
    removed_dead = 0

    for prelude, body in raw_blocks:
        p_norm = _norm_ws(prelude)
        if p_norm.startswith("@keyframes") or p_norm.startswith("@-webkit-keyframes"):
            items.append(("", p_norm, body, True))
        elif p_norm.startswith("@media") or p_norm.startswith("@supports"):
            for sub_p, sub_b in parse_css_blocks(body):
                filtered = _filter_dead_selectors(sub_p)
                if not filtered:
                    removed_dead += 1
                    continue
                items.append((p_norm, _norm_ws(filtered), sub_b, False))
        elif not body and p_norm.startswith("@"):
            items.append(("", p_norm, "", False))
        else:
            filtered = _filter_dead_selectors(p_norm)
            if not filtered:
                removed_dead += 1
                continue
            items.append(("", _norm_ws(filtered), body, False))

    # Deduplikasi blok identik / tertimpa penuh pada (media, selector) yang sama
    deduped: list[tuple[str, str, str, bool]] = []
    seen_index: dict[tuple[str, str], int] = {}
    merged_dups = 0

    for media, sel, body, is_kf in items:
        if is_kf:
            key = ("@kf", sel.lower())
            if key in seen_index:
                deduped[seen_index[key]] = (media, sel, body, True)
                merged_dups += 1
            else:
                seen_index[key] = len(deduped)
                deduped.append((media, sel, body, True))
            continue
        if not body:
            deduped.append((media, sel, body, False))
            continue

        decls = _parse_decls(body)
        if not decls:
            continue
        key = (media, sel)
        if key in seen_index:
            prev_idx = seen_index[key]
            prev_media, prev_sel, prev_body, _ = deduped[prev_idx]
            prev_decls = _parse_decls(prev_body)
            new_props = {k for k, _ in decls}
            remaining_prev = [(k, v) for k, v in prev_decls if k not in new_props]
            if not remaining_prev:
                # Seluruh properti lama tertimpa oleh aturan baru -> hapus aturan lama
                deduped[prev_idx] = ("__DROP__", "", "", False)
                merged_dups += 1
            else:
                deduped[prev_idx] = (prev_media, prev_sel, _format_decls(remaining_prev), False)
                merged_dups += 1
        seen_index[key] = len(deduped)
        deduped.append((media, sel, _format_decls(decls), False))

    buckets: dict[str, list[tuple[str, str, str, bool]]] = {"base": [], "pantau": [], "pages": []}
    for media, sel, body, is_kf in deduped:
        if media == "__DROP__":
            continue
        bucket = _classify_rule(sel)
        buckets[bucket].append((media, sel, body, is_kf))

    def render_bucket(header: str, rows: list[tuple[str, str, str, bool]]) -> str:
        lines = [f"/* {header} */", ""]
        cur_media = ""
        for media, sel, body, is_kf in rows:
            if media != cur_media:
                if cur_media:
                    lines.append("}\n")
                cur_media = media
                if cur_media:
                    lines.append(f"{cur_media} {{")
            if not body and not is_kf:
                lines.append(f"{sel}")
            elif is_kf:
                lines.append(f"{sel} {{\n  {body.strip()}\n}}\n")
            elif cur_media:
                indented = "\n".join("  " + ln if ln.strip() else "" for ln in body.splitlines())
                lines.append(f"  {sel} {{\n{indented}\n  }}")
            else:
                lines.append(f"{sel} {{\n{body}\n}}\n")
        if cur_media:
            lines.append("}")
        return "\n".join(lines).strip() + "\n"

    (PUBLIC_DIR / "css").mkdir(parents=True, exist_ok=True)
    styles_out = render_bucket("CCTipi System — Core Tokens, Shell, Sidebar, Topbar, Modal & Responsive Base", buckets["base"])
    pantau_out = render_bucket("CCTipi System — Beranda, Multiview Grid, Tile Player, PTZ, TV Wall & Peta", buckets["pantau"])
    pages_out = render_bucket("CCTipi System — Halaman Jepretan AI, Rekaman, Health, Kamera Saya & Pengaturan", buckets["pages"])

    styles_path.write_text(styles_out, encoding="utf-8")
    (PUBLIC_DIR / "css" / "pantau.css").write_text(pantau_out, encoding="utf-8")
    (PUBLIC_DIR / "css" / "pages.css").write_text(pages_out, encoding="utf-8")
    merge_path.unlink()

    return {
        "removed_dead_blocks": removed_dead,
        "deduplicated_blocks": merged_dups,
        "styles_lines": len(styles_out.splitlines()),
        "pantau_lines": len(pantau_out.splitlines()),
        "pages_lines": len(pages_out.splitlines()),
    }


def check_css_balance(path: Path) -> list[str]:
    errors: list[str] = []
    text = strip_css_comments(path.read_text(encoding="utf-8"))
    depth = 0
    quote = ""
    for idx, ch in enumerate(text, 1):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                errors.append(f"{path.name}: kurung kurawal tutup berlebih pada offset {idx}")
                break
    if quote:
        errors.append(f"{path.name}: string kutip {quote!r} tidak ditutup")
    if depth != 0:
        errors.append(f"{path.name}: kurung kurawal tidak seimbang (sisa={depth})")
    if _DEAD_RE.search(text):
        m = _DEAD_RE.search(text)
        errors.append(f"{path.name}: masih mengandung selector purwarupa mati: {m.group(0) if m else ''}")
    return errors


def check_js_balance(path: Path) -> list[str]:
    """Pemeriksaan keseimbangan kurung `{}`, `()`, `[]` pada JS (sadar string, template literal & komentar)."""
    src = path.read_text(encoding="utf-8")
    stack: list[tuple[str, int]] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    i, n = 0, len(src)
    line = 1
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        # Komentar satu baris
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            end = src.find("\n", i + 2)
            if end < 0:
                break
            line += 1
            i = end + 1
            continue
        # Komentar blok
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            if end < 0:
                return [f"{path.name}: komentar /* tidak ditutup"]
            line += src[i : end + 2].count("\n")
            i = end + 2
            continue
        # String ' atau "
        if ch in ("'", '"'):
            q = ch
            i += 1
            while i < n:
                c = src[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n":
                    return [f"{path.name}:{line}: string {q} terpotong baris baru"]
                if c == q:
                    i += 1
                    break
                i += 1
            continue
        # Template literal `...${...}...`
        if ch == "`":
            i += 1
            while i < n:
                c = src[i]
                if c == "\\":
                    if i + 1 < n and src[i + 1] == "\n":
                        line += 1
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                if c == "$" and i + 1 < n and src[i + 1] == "{":
                    stack.append(("${", line))
                    i += 2
                    break
                if c == "`":
                    i += 1
                    break
                i += 1
            continue
        # Regex literal sederhana setelah operator/pemisah
        if ch == "/":
            prev = src[:i].rstrip()
            prev_ch = prev[-1] if prev else "("
            if prev_ch in "=(:,!&|?+;{}[~^":
                j = i + 1
                in_cls = False
                valid_re = False
                while j < n and src[j] != "\n":
                    rc = src[j]
                    if rc == "\\":
                        j += 2
                        continue
                    if rc == "[":
                        in_cls = True
                    elif rc == "]":
                        in_cls = False
                    elif rc == "/" and not in_cls:
                        valid_re = True
                        j += 1
                        while j < n and src[j].isalpha():
                            j += 1
                        break
                    j += 1
                if valid_re:
                    i = j
                    continue

        if ch in "{([":
            stack.append((ch, line))
        elif ch == "}" and stack and stack[-1][0] == "${":
            stack.pop()
            # Lanjutkan pembacaan sisa template literal setelah `}` dari `${...}`
            i += 1
            while i < n:
                c = src[i]
                if c == "\\":
                    if i + 1 < n and src[i + 1] == "\n":
                        line += 1
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                if c == "$" and i + 1 < n and src[i + 1] == "{":
                    stack.append(("${", line))
                    i += 2
                    break
                if c == "`":
                    i += 1
                    break
                i += 1
            continue
        elif ch in "})]":
            if not stack or stack[-1][0] != pairs[ch]:
                return [f"{path.name}:{line}: kurung tutup {ch!r} tidak cocok (stack={stack[-1] if stack else 'kosong'})"]
            stack.pop()
        i += 1

    if stack:
        top, ln = stack[-1]
        return [f"{path.name}:{ln}: kurung buka {top!r} belum ditutup"]
    return []


def check_frontend() -> list[str]:
    errors: list[str] = []
    if (PUBLIC_DIR / "merge.css").exists():
        errors.append("public/merge.css masih ada — harus dihapus setelah digabung ke css modular.")

    for p in CSS_SOURCES:
        if not p.is_file():
            errors.append(f"Berkas CSS hilang: {p}")
        else:
            errors.extend(check_css_balance(p))

    for p in [*JS_SOURCES, PUBLIC_DIR / "login.js"]:
        if not p.is_file():
            errors.append(f"Berkas JS hilang: {p}")
        else:
            errors.extend(check_js_balance(p))

    for html_name in ("index.html", "login.html"):
        html_path = PUBLIC_DIR / html_name
        html = html_path.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:href|src)="(/[^"#?]+)"', html):
            if ref.startswith("/api/"):
                continue
            asset = PUBLIC_DIR / ref.lstrip("/")
            if not asset.is_file():
                errors.append(f"{html_name}: referensi aset lokal tidak ditemukan: {ref}")
    return errors


def minify_css(text: str) -> str:
    clean = strip_css_comments(text)
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"\s*([{};:,>~])\s*", r"\1", clean)
    clean = clean.replace(";}", "}")
    return clean.strip()


def minify_js(text: str) -> str:
    """Minifikasi ringan aman berbasis baris (hapus komentar murni & spasi tepi tanpa merusak literal)."""
    out_lines: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if in_block:
            if "*/" in line:
                in_block = False
                line = line.split("*/", 1)[1].strip()
                if not line:
                    continue
            else:
                continue
        if line.startswith("/*"):
            if "*/" in line[2:]:
                line = line.split("*/", 1)[1].strip()
                if not line:
                    continue
            else:
                in_block = True
                continue
        if line.startswith("//"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def build_minified() -> dict[str, int]:
    dist = PUBLIC_DIR / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    raw_css = "\n".join(p.read_text(encoding="utf-8") for p in CSS_SOURCES)
    min_css = minify_css(raw_css)
    (dist / "bundle.min.css").write_text(min_css, encoding="utf-8")

    raw_js = "\n".join(p.read_text(encoding="utf-8") for p in JS_SOURCES)
    min_js = minify_js(raw_js)
    (dist / "bundle.min.js").write_text(min_js, encoding="utf-8")
    return {
        "css_raw_bytes": len(raw_css.encode("utf-8")),
        "css_min_bytes": len(min_css.encode("utf-8")),
        "js_raw_bytes": len(raw_js.encode("utf-8")),
        "js_min_bytes": len(min_js.encode("utf-8")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build, cek, dan minifikasi frontend CCTipi.")
    parser.add_argument("--split-legacy", action="store_true", help="Pecah styles.css + merge.css ke 3 berkas CSS")
    parser.add_argument("--check", action="store_true", help="Validasi referensi HTML dan keseimbangan CSS/JS")
    parser.add_argument("--minify", action="store_true", help="Bangun bundel terkompresi di public/dist/")
    args = parser.parse_args()

    if not (args.split_legacy or args.check or args.minify):
        args.check = True

    if args.split_legacy:
        stats = split_legacy_css()
        print(f"[split-legacy] selesai: {stats}")

    if args.minify:
        mstats = build_minified()
        print(f"[minify] selesai: {mstats}")

    if args.check:
        errs = check_frontend()
        if errs:
            for e in errs:
                print(f"[GAGAL] {e}", file=sys.stderr)
            return 1
        print("[check] OK — semua berkas HTML, CSS, dan JS valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

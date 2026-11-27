# CCTipi System — Multiview CCTV + AI + Cloud

Aplikasi pemantauan CCTV lokal berbasis web dengan multiview, AI Jepretan (YOLO11n), Live AI realtime, Rekaman Server, Perjelas Malam, notifikasi Telegram, ONVIF/RTSP, Tuya IoT Cloud, dan pengamanan sesi/RBAC.

> **Edisi Publik** — Repositori ini adalah edisi publik CCTipi System.
> Data kamera bawaan di `cameras.json` hanya berisi contoh wilayah **Semarang** (76 kamera publik);
> wilayah lain diambil langsung dari sumber publiknya masing-masing saat aplikasi berjalan.
> Tidak ada data pribadi, kredensial, token, atau kunci API yang disertakan di repositori ini —
> semua rahasia wajib diisi sendiri lewat variabel lingkungan atau pengaturan aplikasi.

---

## 1. Menjalankan Secara Lokal

Syarat minimum:
- **Python 3.10+** (fungsi inti berjalan murni dengan pustaka standar + `certifi` opsional).
- **`ffmpeg` & `ffprobe`** di `PATH` (diperlukan untuk jembatan kamera RTSP→HLS, Rekaman Server, AI Jepretan, dan Live AI).

### Mode Ringan (Tanpa AI)
```bash
pip install -r requirements.txt
python3 server.py
```

### Mode Lengkap (Dengan AI Jepretan & Live AI YOLO11n)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-ai.txt

# Unduh bobot resmi YOLO11n ONNX + verifikasi SHA-256 otomatis:
.venv/bin/python ai.py --download-model

.venv/bin/python server.py
```

Default server mendengarkan di **http://127.0.0.1:9999**.

### Keamanan Login Awal & Akses LAN (`HOST=0.0.0.0`)
- **Login awal:** `admin` / `cctipi123`.
- **Wajib ganti password saat login pertama:** Selama akun masih memakai password bawaan (`cctipi123`) atau password awal buatan admin (`mustChange=true`), seluruh endpoint operasional (`/api/cameras`, `/api/proxy`, `/api/sources`, dll.) diblokir (`HTTP 403`) sampai password diganti di **Pengaturan → Akun saya** (`/api/auth/password`).
- **Proteksi jaringan terbuka:** Server **menolak start** pada alamat non-loopback (`HOST=0.0.0.0` atau IP LAN) selama akun masih memakai password bawaan `cctipi123`. Ganti password terlebih dahulu lewat `127.0.0.1`, atau tetapkan `CCTIPI_PASS` yang kuat saat inisialisasi pertama:
  ```bash
  CCTIPI_PASS="GantiDenganPasswordKuat123!" HOST=0.0.0.0 PORT=9999 python3 server.py
  ```

---

## 2. Panduan Reverse Proxy HTTPS (Disarankan untuk LAN / Internet)

`ThreadingHTTPServer` Python dirancang untuk berada di belakang reverse proxy TLS ketika diakses di luar `localhost`. Saat dideploy di belakang HTTPS dengan `CCTIPI_TRUST_PROXY=1`, server otomatis mengaktifkan atribut cookie `Secure` dan header `Strict-Transport-Security` (HSTS).

### Opsi A — Caddy (`Caddyfile`, sertifikat TLS otomatis)
```caddyfile
cctv.contoh.id {
    encode zstd gzip

    # Matikan buffering untuk stream kontinu (MJPEG, Live AI, FLV, HLS)
    reverse_proxy 127.0.0.1:9999 {
        flush_interval -1
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-Host {host}
    }
}
```
Jalankan CCTipi di belakang Caddy:
```bash
CCTIPI_TRUST_PROXY=1 CCTIPI_PUBLIC_URL="https://cctv.contoh.id" HOST=127.0.0.1 PORT=9999 python3 server.py
```

### Opsi B — Nginx (`/etc/nginx/conf.d/cctipi.conf`)
```nginx
server {
    listen 443 ssl http2;
    server_name cctv.contoh.id;

    ssl_certificate     /etc/letsencrypt/live/cctv.contoh.id/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cctv.contoh.id/privkey.pem;

    client_max_body_size 10m;

    location / {
        proxy_pass         http://127.0.0.1:9999;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        # Timpa (jangan tambahkan) agar klien tidak bisa memalsukan IP lewat X-Forwarded-For
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Forwarded-Host  $host;

        # Penting agar MJPEG, Live AI (.mjpg), dan segmen HLS tidak tertahan buffer
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
    }
}
```

---

## 3. Menjalankan dengan Docker

```bash
# Mode standar (tanpa AI):
docker build -t cctipi:latest .

# Mode lengkap dengan YOLO11n ONNX terverifikasi SHA-256:
docker build --build-arg INSTALL_AI=1 -t cctipi:ai .

# Jalankan container (wajib isi CCTIPI_PASS yang kuat saat start pertama):
docker run -d --name cctipi \
  -p 127.0.0.1:9999:9999 \
  -e CCTIPI_PASS="PasswordKuatMinimal8Karakter" \
  -e CCTIPI_TRUST_PROXY=1 \
  -v "$(pwd)/data:/app/data" \
  cctipi:latest
```

---

## 4. Kebutuhan Sumber Daya & Kapasitas (AI, Live AI & Rekaman)

Bobot model resmi yang dikunci di `ai.py`:
- **Model:** `yolo11n.onnx` (`ultralytics/assets` rilis `v8.3.0`, ukuran `10.930.182` byte)
- **SHA-256:** `634279b40c07c6391472c51ad45b81ebc48706a9a1fe72dd3396322acd0c053b`
- Server memverifikasi SHA-256 sebelum memuat model ke `onnxruntime` sehingga berkas korup/termodifikasi ditolak otomatis.

| Fitur | Estimasi CPU (x86_64 4-core / ARM64) | Estimasi RAM Tambahan | Catatan Operasional |
|---|---|---|---|
| Server inti + Proxy HLS/MJPEG (8 penonton) | < 5% CPU | ~45–70 MB | Tanpa `ffmpeg`; koneksi upstream di-pool |
| Jembatan RTSP → HLS (copy H.264) | ~2–5% CPU / kamera | ~25–40 MB / kamera | Auto-reap setelah 60 dtk tanpa penonton |
| Jembatan RTSP → HLS (transcode H.265→H.264) | ~35–70% 1 core / kamera | ~90–140 MB / kamera | Otomatis aktif hanya bila stream sumber HEVC |
| Rekaman Server (`-c copy` MP4) | ~2–4% CPU / kamera | ~25–35 MB / kamera | ±180–240 MB disk/jam per kamera 640×360 |
| Worker AI Jepretan (interval 10 dtk) | ~10–25% 1 core saat pindai | ~180–260 MB (shared ONNX) | Berjalan bergantian 1 sesi ONNX |
| Live AI Realtime (pilot maks. 1 kamera) | ~60–120% CPU | ~150–220 MB | Auto-stop 10 menit atau 45 dtk tanpa penonton |

Jika batas proses `ffmpeg` atau slot penonton penuh, endpoint stream mengembalikan **`HTTP 503 Service Unavailable`** dengan pesan jelas (bukan menggantung/OOM). Pantau metrik aktual kapan saja lewat **`GET /api/health`** atau halaman **Health Monitor**.

---

## 5. Referensi Variabel Lingkungan & Berkas Konfigurasi

### Variabel Lingkungan
| Variabel | Default | Fungsi |
|---|---|---|
| `HOST`, `PORT` | `127.0.0.1`, `9999` | Alamat ikat dan port HTTP server. Non-loopback ditolak bila password bawaan masih aktif. |
| `CCTIPI_USER`, `CCTIPI_PASS` | `admin`, `cctipi123` | Akun awal saat `auth.json` belum ada. Bila `CCTIPI_PASS != cctipi123`, akun langsung siap pakai. |
| `CCTIPI_TRUST_PROXY` | `0` | Set `1` bila di belakang Caddy/Nginx lokal agar `X-Forwarded-For` & `X-Forwarded-Proto` dipercaya. IP klien diambil dari entri **paling kanan** `X-Forwarded-For` (entri kiri bisa dipalsukan klien). |
| `CCTIPI_TRUST_PROXY_HOPS` | `1` | Jumlah reverse proxy tepercaya berantai di depan CCTipi (mis. `2` untuk CDN → Nginx → CCTipi). |
| `CCTIPI_PBKDF2_ITERATIONS` | `600000` | Iterasi PBKDF2-HMAC-SHA256 hash password (min. `100000`). Hash lama otomatis di-upgrade saat login berhasil. |
| `CCTIPI_PUBLIC_URL` | *(kosong)* | URL publik (mis. `https://cctv.contoh.id`); bila diawali `https://`, cookie `Secure` & HSTS aktif otomatis. |
| `CCTIPI_SECURE_COOKIE` | *(otomatis)* | Set `1` untuk paksa cookie `Secure`, atau `0` untuk mematikan pada pengujian khusus. |
| `CCTIPI_ALLOW_PRIVATE_CAMERA_HOSTS` | `0` | Set `1` untuk mengizinkan IP kamera LAN privat RFC1918 (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`) pada kamera manual & ONVIF. Loopback (`127.0.0.0/8`) & link-local (`169.254.0.0/16`) selalu ditolak demi mencegah SSRF. Pemeriksaan dilakukan pada **IP hasil resolve DNS** di setiap koneksi & redirect (hostname seperti `127.0.0.1.nip.io` ikut ditolak). CGNAT `100.64.0.0/10` diperlakukan sebagai privat. |
| `CCTIPI_DIRECT_CORS_HOSTS` | *(kosong)* | Daftar host stream tambahan (pisah koma) yang mendukung CORS langsung dari browser. |
| `CCTIPI_KUDUS_API_KEY` | *(kosong)* | Kunci API publik CCTV Kudus (sumber Kudus nonaktif bila kosong). |
| `CCTIPI_TG_BOT_TOKEN`, `CCTIPI_TG_CHAT_ID` | *(kosong)* | Kredensial bot Telegram (mengesampingkan nilai di UI). |
| `CCTIPI_MODEL_SHA256`, `CCTIPI_MODEL_URL` | SHA-256 resmi YOLO11n | Override hash SHA-256 / URL unduhan model ONNX kustom. |
| `CCTIPI_MAX_FFMPEG_PROCS` | `6` | Batas maksimum proses `ffmpeg` serentak di seluruh fitur. |
| `CCTIPI_MAX_RTSP_BRIDGES` | `4` | Batas maksimum jembatan RTSP → HLS aktif bersamaan. |
| `CCTIPI_MAX_CONCURRENT_RECORDINGS` | `4` | Batas maksimum kamera yang direkam serentak oleh `recorder.py`. |
| `CCTIPI_MAX_VIEWERS_PER_CAMERA` | `6` | Batas penonton simultan per kamera di proxy/MJPEG/Live AI. |
| `CCTIPI_MAX_TOTAL_VIEWERS` | `24` | Batas total koneksi stream penonton aktif di server. |
| `CCTIPI_MIN_FREE_DISK_MB` | `256` | Sisa ruang disk minimum (MB) sebelum perekam menghentikan segmen baru & membersihkan kuota. |
| `CCTIPI_LIVEAI_AUTOSTOP_SECONDS` | `600` | Batas durasi maksimum sesi Live AI (detik). |
| `CCTIPI_LIVEAI_NO_VIEWER_SECONDS` | `45` | Waktu tunggu tanpa penonton sebelum Live AI menghentikan `ffmpeg` otomatis. |
| `CCTIPI_AUDIT_DAYS` | `180` | Masa simpan catatan `data/audit.db` (hari). |
| `CCTIPI_LOG_FORMAT` | `text` | Format log ke `stderr`: `text` (berstempel waktu ISO-8601) atau `json` (terstruktur per baris). |

### Berkas Runtime & Penyimpanan (`0o600`, diabaikan `.gitignore`)
- `auth.json` — Daftar akun pengguna, role RBAC, hash PBKDF2-HMAC-SHA256, dan status `mustChange`.
- `sources.json` & `manual_cameras.json` — Konfigurasi sumber daerah kustom dan kamera manual/ONVIF.
- `data/sessions.json` — Sesi login aktif (hash token SHA-256, kedaluwarsa 7 hari).
- `data/cameras_cache.json` — Cache kamera per wilayah (cadangan otomatis saat portal daerah gangguan/berubah HTML).
- `data/ai_settings.json`, `data/recorder_settings.json`, `data/monitor_settings.json`, `data/tuya_settings.json` — Pengaturan fitur.
- `data/audit.db`, `data/jepretan.db`, `data/rekaman.db` — Basis data SQLite WAL lokal untuk log audit, metadata jepretan AI, dan indeks rekaman MP4.

---

## 6. Struktur Proyek & Pengujian

```
cctipi-system/
├── server.py              # Adaptor HTTP utama, header keamanan/HTTPS & komposisi layanan
├── auth_routes.py         # Sesi, login/action rate-limiter, RBAC, kelola user/role, audit, prefs
├── camera_store.py        # Penyimpanan sumber/kamera manual, validasi SSRF allowlist & cache
├── camera_routes.py       # Endpoint HTTP sumber kamera, kamera manual, cek status & backup
├── proxy_routes.py        # Reverse proxy HLS/FLV/MJPEG, keep-alive pool & bridge RTSP→HLS
├── onvif_tuya_routes.py   # Penemuan profil/PTZ ONVIF SOAP & integrasi Tuya IoT Cloud
├── recording_routes.py    # Endpoint daftar/cari/unduh rekaman & pemutaran HTTP Range (206/416)
├── ai_routes.py           # Endpoint AI Jepretan, Zona Deteksi, Live AI & Alarm Monitor
├── system_health.py       # Validasi startup, ConcurrencyGuard, log terstruktur & /api/health
├── scrapers.py            # Parser HTMLParser & JSON seimbang untuk 10 wilayah publik
├── ai.py                  # Worker AI YOLO11n ONNX (verifikasi SHA-256) + Telegram
├── live_ai.py             # Live AI realtime MJPEG + watchdog auto-stop
├── recorder.py            # Perekam segmen/klip MP4 + pemulihan kuota & disk penuh
├── enhance.py             # Perjelas Malam (CLAHE + gamma adaptif + bilateral denoise)
├── mjpeg_hub.py           # Hub pembagi frame MJPEG hemat koneksi
├── monitor.py             # Pemantau kamera offline & ruang disk
├── tuya.py                # Klien Tuya OpenAPI
├── users.py               # Manajemen pengguna, role & password PBKDF2
├── audit.py               # Audit log SQLite dengan redaksi kredensial otomatis
├── storage_utils.py       # Penulisan atomik 0o600, closing_sqlite & redaksi rahasia
├── tools/build_frontend.py# Pemeriksa sintaks & pembuat bundel minifikasi CSS/JS
├── public/                # SPA Frontend modular (styles.css, css/*, js/*, app.js)
└── tests/                 # Pengujian otomatis (unit, scraper, keamanan, Range & uji beban)
```

Menjalankan seluruh pemeriksaan lokal (sama seperti GitHub Actions CI):
```bash
python3 tools/build_frontend.py --check --minify
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

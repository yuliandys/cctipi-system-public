# Patch Termux

## v1.0.3 — hasil pengecekan v1.0.2

1. **Isi zip kembali di dalam folder `CCtipi-sistem/`.** Zip v1.0.2 berisi file
   langsung di akar, sehingga `unzip -d ~` menebar ±25 file aplikasi ke folder
   home dan bercampur dengan proyek lain. Installer sekarang menolak dijalankan
   langsung dari `$HOME`.
2. **`tools/harvest-ca.py` lebih aman.** Intermediate yang diunduh lewat AIA
   (HTTP, tanpa enkripsi) sekarang hanya disimpan bila bukan self-signed **dan**
   rantai leaf → intermediate → root tepercaya (certifi) lolos tanpa
   *partial chain*. Sebelumnya file disimpan dulu baru diverifikasi, jadi CA
   palsu dari penyerang di jaringan bisa ikut dipercaya untuk semua koneksi
   HTTPS kamera.
3. Dua CA di `extras/ca/` sudah dicek: asli Sectigo, tersambung ke root
   USERTrust RSA / Sectigo Root R46 di certifi, berlaku sampai 2030/2036.

---

# Patch Termux — v1.0.2 (setelah v1.0.1)

Ringkasan perubahan pada paket ini dibanding `cctipi-v1.0.1-termux.zip`.
Semua perubahan hanya untuk Termux; tidak menyentuh logika aplikasi.

## 1. `tools/cctipi.sh` dan `install-termux.sh` — python yang benar

**Masalah.** Di Termux, `python`/`python3` di PATH bisa menunjuk ke venv lain
(mis. venv Hermes Agent). Venv itu tidak punya `cv2`/`onnxruntime` Termux, jadi
server berhenti dengan `ModuleNotFoundError: No module named 'cv2'` walaupun
paketnya sudah terpasang.

**Perbaikan.**

- `tools/cctipi.sh` memakai path absolut `${PREFIX}/bin/python3`.
- `tools/cctipi.sh` dan `install-termux.sh` melakukan
  `unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH` lebih dulu.
- `install-termux.sh` memakai variabel `$PY` untuk semua pemanggilan python.

**Kenapa perlu `unset LD_LIBRARY_PATH`.** Kalau shell induk mengekspor
`LD_LIBRARY_PATH` ke `libpython` versi lain, modul C python Termux gagal dimuat:

```
ImportError: dlopen failed: cannot locate symbol "_PyType_AllocNoTrack"
```

Gejalanya menyesatkan: `python3 -V` melaporkan versi lama, dan `import array`
gagal. Cek cepat:

```
python3 -V                          # versi salah
env -u LD_LIBRARY_PATH python3 -V   # versi benar
```

## 2. `scrapers.py` — sertifikat intermediate CA

**Masalah.** Sebagian server kamera hanya mengirim leaf certificate tanpa
intermediate. Browser menambalnya otomatis, Python/OpenSSL tidak. Akibatnya
stream gagal dengan:

```
SSL: CERTIFICATE_VERIFY_FAILED unable to get local issuer certificate
```

**Perbaikan.** `_ssl_ctx` sekarang memuat tambahan CA dari `extras/ca/*.pem`.
Verifikasi TLS tetap aktif — bukan `CERT_NONE`.

Isi `extras/ca/`:

- `SectigoRSADomainValidationSecureServerCA.pem`
- `SectigoPublicServerAuthenticationCADVR36.pem`

## 3. `tools/harvest-ca.py` — pengambil CA otomatis (baru)

Membaca ekstensi AIA dari leaf certificate setiap host kamera, mengunduh
intermediate-nya, memvalidasi PEM, lalu menyimpannya ke `extras/ca/`.

```
python3 tools/harvest-ca.py
```

Kriteria valid: hasil unduhan > 500 byte dan mengandung blok
`-----BEGIN CERTIFICATE-----`. File lebih kecil ditolak (sering berupa halaman
HTML error atau artifact padding).

## 4. `tools/cctipi.sh` — tambahan `restart`

Perintah `cctipi restart` menjalankan `stop` lalu `start` dengan jeda singkat,
supaya rate limiter login in-memory ikut ter-reset.

## Cara pasang

```
unzip cctipi-v1.0.3-termux.zip -d ~
cd ~/CCtipi-sistem && bash install-termux.sh     # atau: --lan untuk akses Wi-Fi
```

Installer aman dijalankan ulang: `auth.json`, `data/`, dan daftar kamera tidak
dihapus.

## Catatan

- `model.loaded` di `/api/ai/status` masih bisa tampil `false` walau model
  sebenarnya termuat. Ini bug tampilan v1.0.1, tidak memengaruhi deteksi.
- Key kamera untuk daftar AI berformat `u:<url>` atau `m:<manualId>`, bukan ID
  numerik.
- `data/`, `auth.json`, `sources.json`, `manual_cameras.json` sengaja tidak ikut
  di paket.
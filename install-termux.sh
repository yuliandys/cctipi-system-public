#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  CCTipi System — pemasang untuk Termux (Android)
#
#  Pakai:   bash install-termux.sh            # akses hanya dari HP ini
#           bash install-termux.sh --lan      # bisa dibuka dari PC/HP lain di Wi-Fi yang sama
#           bash install-termux.sh --no-ai    # lewati AI Jepretan (lebih cepat & ringan)
#
#  Aman dijalankan ulang (mis. setelah update): data & akun tidak dihapus.
# =====================================================================
set -u

# Kalau skrip ini dipanggil dari shell yang memakai venv python lain (mis. Hermes),
# `python` di PATH bisa menunjuk ke python asing tanpa modul Termux. Paksa punya Termux.
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH
PY="${PREFIX:-/data/data/com.termux/files/usr}/bin/python3"
[ -x "$PY" ] || PY=python

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HOME/.cctipi.env"
WANT_LAN=0
WANT_AI=1
for arg in "$@"; do
  case "$arg" in
    --lan) WANT_LAN=1 ;;
    --no-ai) WANT_AI=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
info() { printf '\033[36m•\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✘ %s\033[0m\n' "$*"; exit 1; }

# --- 0. Pemeriksaan dasar -------------------------------------------------
[ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ] || die "Skrip ini untuk Termux (variabel PREFIX tidak ada)."
[ -f "$APP_DIR/server.py" ] || die "server.py tidak ditemukan di $APP_DIR — jalankan dari folder CCtipi-sistem."
case "$APP_DIR" in
  /sdcard*|/storage*|"$HOME"/storage*)
    die "Folder ada di penyimpanan bersama ($APP_DIR). Penyimpanan ini bisa dibaca aplikasi lain dan izin file
   tidak berlaku. Pindahkan dulu:  mv \"$APP_DIR\" ~/CCtipi-sistem && cd ~/CCtipi-sistem && bash install-termux.sh" ;;
esac
if [ "$APP_DIR" = "$HOME" ]; then
  die "Aplikasi dipasang langsung di folder home ($HOME) dan akan bercampur dengan proyek lain
   (mis. folder data/ milik aplikasi lain ikut di-chmod). Pindahkan ke folder sendiri:
   unzip cctipi-v1.0.3-termux.zip -d ~ && cd ~/CCtipi-sistem && bash install-termux.sh"
fi
ok "Folder aplikasi: $APP_DIR"

# --- 1. Paket sistem -------------------------------------------------------
info "Memperbarui daftar paket & memasang Python + ffmpeg (bisa beberapa menit)…"
pkg update -y >/dev/null 2>&1 || warn "pkg update gagal — lanjut dengan daftar paket yang ada."
pkg install -y python ffmpeg procps curl >/dev/null 2>&1 || die "Gagal memasang python/ffmpeg. Cek koneksi internet lalu ulangi."
command -v ffprobe >/dev/null || warn "ffprobe tidak ada: kamera H.265 tetap dicoba, tapi deteksi codec lebih lambat."
"$PY" -m pip install --disable-pip-version-check -q certifi >/dev/null 2>&1 || warn "certifi gagal dipasang (opsional)."
ok "Python $("$PY" -V 2>&1 | cut -d' ' -f2) & $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"

# --- 2. AI Jepretan (opsional, tidak menghentikan instalasi bila gagal) ---
AI_OK=0
if [ "$WANT_AI" = 1 ]; then
  info "Memasang AI Jepretan (numpy, OpenCV, onnxruntime)…"
  pkg install -y python-numpy opencv-python >/dev/null 2>&1 || warn "numpy/OpenCV dari pkg gagal."
  if ! "$PY" -c "import onnxruntime" >/dev/null 2>&1; then
    "$PY" -m pip install -q onnxruntime >/dev/null 2>&1 \
      || { pkg install -y tur-repo >/dev/null 2>&1; pkg install -y python-onnxruntime >/dev/null 2>&1; } \
      || true
    "$PY" -c "import onnxruntime" >/dev/null 2>&1 \
      || "$PY" -m pip install -q onnxruntime --extra-index-url https://termux-user-repository.github.io/pypi/ >/dev/null 2>&1 \
      || true
  fi
  if "$PY" -c "import numpy, cv2, onnxruntime" >/dev/null 2>&1; then
    AI_OK=1; ok "AI Jepretan siap (onnxruntime $("$PY" -c 'import onnxruntime as o;print(o.__version__)'))."
  else
    warn "AI belum bisa dipasang di HP ini (onnxruntime tidak tersedia untuk Python Termux ini)."
    warn "Sistem tetap jalan penuh; hanya AI Jepretan & Live AI yang 'Tidak tersedia'."
  fi
  [ -f "$APP_DIR/models/yolo11n.onnx" ] || warn "models/yolo11n.onnx tidak ada — AI butuh berkas model ini."
else
  info "AI dilewati (--no-ai)."
fi

# --- 3. Pengaturan server ---------------------------------------------------
GEN_PASS=""
if [ "$WANT_LAN" = 1 ] && [ ! -f "$APP_DIR/auth.json" ]; then
  GEN_PASS="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(9))')"
fi
if [ ! -f "$ENV_FILE" ]; then
  {
    echo "# Pengaturan CCTipi System (dibaca oleh perintah 'cctipi')"
    if [ "$WANT_LAN" = 1 ]; then echo "HOST=0.0.0.0"; else echo "HOST=127.0.0.1"; fi
    echo "PORT=9999"
    [ -n "$GEN_PASS" ] && echo "CCTIPI_PASS=$GEN_PASS"
    echo "# Aktifkan bila diakses lewat HTTPS (Cloudflare Tunnel / reverse proxy):"
    echo "# CCTIPI_SECURE_COOKIE=1"
    echo "# CCTIPI_TRUST_PROXY=1"
  } > "$ENV_FILE"
  ok "Pengaturan dibuat: $ENV_FILE"
elif [ "$WANT_LAN" = 1 ]; then
  sed -i 's/^HOST=.*/HOST=0.0.0.0/' "$ENV_FILE"
  if [ -n "$GEN_PASS" ] && ! grep -q '^CCTIPI_PASS=' "$ENV_FILE"; then
    echo "CCTIPI_PASS=$GEN_PASS" >> "$ENV_FILE"
  fi
  ok "HOST diubah ke 0.0.0.0 (akses LAN)."
fi
chmod 600 "$ENV_FILE"

# --- 4. Data lama & izin berkas rahasia --------------------------------------
mkdir -p "$APP_DIR/data"
if [ -f "$APP_DIR/data/ngawasi.db" ] && [ ! -f "$APP_DIR/data/cctipi.db" ]; then
  for s in "" -wal -shm; do [ -f "$APP_DIR/data/ngawasi.db$s" ] && mv "$APP_DIR/data/ngawasi.db$s" "$APP_DIR/data/cctipi.db$s"; done
  ok "Database lama data/ngawasi.db dipindahkan ke data/cctipi.db (jepretan & rekaman tetap terbaca)."
fi
chmod 700 "$APP_DIR/data"
for f in auth.json manual_cameras.json sources.json; do [ -f "$APP_DIR/$f" ] && chmod 600 "$APP_DIR/$f"; done
rm -f "$APP_DIR/data/sessions.json"   # sesi dari perangkat lain tidak berlaku di sini

# --- 5. Perintah 'cctipi' & auto-start ----------------------------------------
sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/tools/cctipi.sh" > "$PREFIX/bin/cctipi"
chmod 755 "$PREFIX/bin/cctipi"
ok "Perintah terpasang: cctipi start | stop | restart | status | log"

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/cctipi-start.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Dijalankan aplikasi Termux:Boot setelah HP menyala.
termux-wake-lock
sleep 20
$PREFIX/bin/cctipi start
EOF
chmod 755 "$HOME/.termux/boot/cctipi-start.sh"
ok "Auto-start disiapkan (perlu aplikasi Termux:Boot dari F-Droid, buka sekali)."

# --- 6. Jalankan -------------------------------------------------------------
FRESH=0; [ -f "$APP_DIR/auth.json" ] || FRESH=1   # server membuat auth.json saat pertama jalan
"$PREFIX/bin/cctipi" restart

IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"
. "$ENV_FILE"
echo
echo "================================================================"
ok "CCTipi System terpasang."
echo "  Buka di HP ini      : http://127.0.0.1:${PORT}"
[ "$HOST" = "0.0.0.0" ] && echo "  Dari PC/HP lain     : http://${IP:-<IP-HP>}:${PORT}   (Wi-Fi yang sama)"
[ "$FRESH" = 1 ] && [ -n "$GEN_PASS" ] && echo "  Login pertama       : admin / $GEN_PASS  (tersimpan di ~/.cctipi.env)"
[ "$FRESH" = 1 ] && [ -z "$GEN_PASS" ] && echo "  Login pertama       : admin / cctipi123  → WAJIB langsung diganti"
echo
echo "  Agar tidak dimatikan Android (sekali saja):"
echo "   1. Pengaturan → Aplikasi → Termux → Baterai → Tidak dibatasi"
echo "   2. Android 14+: Opsi pengembang → 'Disable child process restrictions' ON"
echo "      Android 12–13: lewat ADB/wireless debugging:"
echo "      adb shell settings put global settings_enable_monitor_phantom_procs false"
echo "   3. Pasang Termux:Boot (F-Droid) & buka sekali agar server menyala setelah restart."
echo "================================================================"

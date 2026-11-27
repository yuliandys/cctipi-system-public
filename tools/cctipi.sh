#!/data/data/com.termux/files/usr/bin/bash
# Perintah kendali CCTipi System di Termux (dipasang ke $PREFIX/bin/cctipi
# oleh install-termux.sh). Pakai: cctipi start|stop|restart|status|log
APP_DIR="@APP_DIR@"
ENV_FILE="$HOME/.cctipi.env"
PID_FILE="$APP_DIR/data/server.pid"
LOG_FILE="$APP_DIR/data/server.log"

[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
: "${HOST:=127.0.0.1}" "${PORT:=9999}"

# Hermes memasang LD_LIBRARY_PATH yang menunjuk libpython miliknya sendiri
# (versi berbeda dari python Termux). Itu membuat modul C python Termux gagal
# dimuat ("cannot locate symbol _PyType_AllocNoTrack"). Bersihkan untuk server ini.
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
  if running; then echo "Sudah berjalan (PID $(cat "$PID_FILE")) di http://$HOST:$PORT"; return 0; fi
  mkdir -p "$APP_DIR/data"
  # log dipotong bila > 5 MB agar memori HP tidak penuh
  [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ] && mv -f "$LOG_FILE" "$LOG_FILE.1"
  command -v termux-wake-lock >/dev/null && termux-wake-lock
  cd "$APP_DIR" || exit 1
  nohup /data/data/com.termux/files/usr/bin/python3 server.py >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  chmod 600 "$PID_FILE" "$LOG_FILE" 2>/dev/null
  sleep 3
  if running; then echo "CCTipi System berjalan (PID $(cat "$PID_FILE")) → http://$HOST:$PORT"
  else echo "Gagal menyala. 20 baris log terakhir:"; tail -n 20 "$LOG_FILE"; return 1; fi
}

stop() {
  if ! running; then echo "Tidak berjalan."; rm -f "$PID_FILE"; return 0; fi
  pid="$(cat "$PID_FILE")"
  pkill -TERM -P "$pid" 2>/dev/null      # ffmpeg (RTSP/rekaman/Live AI) milik server
  kill -TERM "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
  running && kill -KILL "$pid" 2>/dev/null
  pkill -f "cctipi_rtsp_hls" 2>/dev/null  # sisa ffmpeg bila ada
  rm -f "$PID_FILE"
  echo "Dihentikan."
}

status() {
  if running; then
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" 2>/dev/null)"
    echo "Berjalan (PID $(cat "$PID_FILE")) · http://$HOST:$PORT · respons /login: ${code:-?}"
    n="$(pgrep -c ffmpeg 2>/dev/null)"; echo "ffmpeg aktif: ${n:-0}"
  else
    echo "Tidak berjalan. Jalankan: cctipi start"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  log) tail -n 100 -f "$LOG_FILE" ;;
  *) echo "Pakai: cctipi start|stop|restart|status|log"; exit 1 ;;
esac

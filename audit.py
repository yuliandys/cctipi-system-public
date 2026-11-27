"""Log aktivitas (audit log) CCTipi System — v6.8.

Mencatat siapa melakukan apa: login, perubahan pengaturan, hapus jepretan/
rekaman, kelola pengguna, alarm sistem, dll. Disimpan di data/audit.db
(SQLite WAL) dan dibersihkan otomatis setelah RETENTION_DAYS hari.
Kredensial/token selalu disamarkan sebelum disimpan.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from storage_utils import closing_sqlite, redact_sensitive_text

RETENTION_DAYS = int(os.environ.get("CCTIPI_AUDIT_DAYS", "180"))

# kode aksi -> label Indonesia (dipakai UI & ekspor CSV)
ACTIONS: dict[str, str] = {
    "auth.login": "Login",
    "auth.login_failed": "Login gagal",
    "auth.logout": "Logout",
    "auth.password": "Ganti akun/password sendiri",
    "user.create": "Tambah pengguna",
    "user.update": "Ubah pengguna",
    "user.delete": "Hapus pengguna",
    "role.save": "Simpan role & izin",
    "role.delete": "Hapus role",
    "jepretan.delete": "Hapus jepretan",
    "rekaman.delete": "Hapus rekaman",
    "ai.settings": "Ubah pengaturan AI",
    "ai.zone": "Ubah zona & jadwal kamera",
    "rekaman.settings": "Ubah pengaturan rekaman",
    "monitor.settings": "Ubah pengaturan alarm",
    "tuya.settings": "Ubah pengaturan Tuya",
    "liveai.start": "Mulai Live AI",
    "liveai.stop": "Hentikan Live AI",
    "camera.add": "Tambah kamera manual",
    "camera.delete": "Hapus kamera manual",
    "sources.save": "Simpan sumber stream",
    "sources.reset": "Reset sumber stream",
    "backup.import": "Import backup",
    "allowlist.host": "Host baru masuk allowlist stream",
    "alarm.offline": "Alarm: kamera offline",
    "alarm.online": "Alarm: kamera kembali online",
    "alarm.disk": "Alarm: penyimpanan hampir penuh",
    "alarm.disk_ok": "Alarm: penyimpanan kembali aman",
}


class AuditLog:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    username TEXT NOT NULL,
                    ip TEXT DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    ok INTEGER DEFAULT 1
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS audit_user ON audit(username)")
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    def _db(self):
        return closing_sqlite(self._path, timeout=10)

    def log(self, username: str, action: str, target: str = "", detail="",
            ip: str = "", ok: bool = True) -> None:
        """Catat satu kejadian dengan redaksi rahasia otomatis. Tidak pernah melempar error."""
        try:
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            safe_target = redact_sensitive_text(target or "")[:200]
            safe_detail = redact_sensitive_text(detail or "")[:1000]
            now = time.time()
            with self._lock, self._db() as conn:
                conn.execute(
                    "INSERT INTO audit (ts, date, username, ip, action, target, detail, ok) VALUES (?,?,?,?,?,?,?,?)",
                    (int(now), time.strftime("%Y-%m-%d", time.localtime(now)), (username or "-")[:64],
                     (ip or "")[:64], action[:40], safe_target, safe_detail, 1 if ok else 0),
                )
                if now - self._last_cleanup > 3600:
                    self._last_cleanup = now
                    conn.execute("DELETE FROM audit WHERE ts < ?", (int(now - RETENTION_DAYS * 86400),))
        except Exception as exc:  # log tidak boleh mengganggu fitur utama
            print(f"[audit] gagal mencatat {action}: {redact_sensitive_text(str(exc))}")

    def query(self, *, date: str = "", username: str = "", action: str = "",
              q: str = "", limit: int = 300) -> list[dict]:
        sql = "SELECT * FROM audit WHERE 1=1"
        args: list = []
        if date:
            sql += " AND date = ?"
            args.append(date)
        if username:
            sql += " AND username = ?"
            args.append(username)
        if action:
            if action.endswith("."):
                sql += " AND action LIKE ?"
                args.append(action + "%")
            else:
                sql += " AND action = ?"
                args.append(action)
        if q:
            sql += " AND (target LIKE ? OR detail LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(2000, max(1, int(limit))))
        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["ok"] = bool(item["ok"])
            item["label"] = ACTIONS.get(item["action"], item["action"])
            out.append(item)
        return out

    def users(self) -> list[str]:
        with self._db() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT username FROM audit ORDER BY username").fetchall()]

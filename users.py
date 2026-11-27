"""Pengguna, role, dan izin akses CCTipi System (v6.8).

Disimpan di auth.json (izin 0600) dengan format:

    {"version": 2,
     "users": [{"username", "passwordHash", "role", "regions", "disabled",
                "mustChange", "createdAt", "lastLogin"}],
     "roles": [{"id", "name", "permissions", "builtin"}]}

File auth.json lama ({"username", "passwordHash"}) dimigrasikan otomatis
menjadi satu user dengan role Admin — login lama tetap berlaku.

Aturan:
- Role "admin" bawaan selalu memegang semua izin dan tidak bisa diubah/dihapus.
- Harus selalu ada minimal satu Admin aktif.
- Akun bawaan dengan password default wajib ganti password pada login pertama.
- regions = [] berarti semua wilayah; selain itu hanya folder yang terdaftar.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from storage_utils import write_private

DEFAULT_PASSWORD = "cctipi123"
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
ROLE_ID_RE = re.compile(r"^[a-z0-9-]{2,32}$")

# (kunci, label, grup) — urutan = urutan di UI
PERMISSIONS: list[tuple[str, str, str]] = [
    ("jepretan.view", "Lihat Jepretan AI", "Jepretan AI"),
    ("jepretan.delete", "Hapus Jepretan", "Jepretan AI"),
    ("liveai.use", "Live AI realtime", "Pantau"),
    ("ptz.control", "Kendali PTZ kamera", "Pantau"),
    ("rekaman.view", "Lihat & putar Rekaman", "Rekaman"),
    ("rekaman.delete", "Hapus Rekaman", "Rekaman"),
    ("cameras.manage", "Kelola kamera manual & ONVIF", "Kamera"),
    ("settings.ai", "Pengaturan AI & Rekaman", "Pengaturan"),
    ("settings.system", "Sumber stream & Backup", "Pengaturan"),
    ("users.manage", "Kelola pengguna & role", "Pengaturan"),
    ("audit.view", "Lihat log aktivitas", "Pengaturan"),
]
PERMISSION_KEYS = [p[0] for p in PERMISSIONS]

DEFAULT_ROLES: list[dict] = [
    {"id": "admin", "name": "Admin", "builtin": True, "permissions": list(PERMISSION_KEYS)},
    {"id": "operator", "name": "Operator", "builtin": True, "permissions": [
        "jepretan.view", "jepretan.delete", "liveai.use", "ptz.control",
        "rekaman.view", "cameras.manage",
    ]},
    {"id": "viewer", "name": "Viewer", "builtin": True, "permissions": ["jepretan.view"]},
]


class UserError(ValueError):
    """Kesalahan validasi yang aman ditampilkan ke pengguna."""


LEGACY_PBKDF2_ITERATIONS = 100_000
try:
    # OWASP (2023+) menyarankan >= 600.000 iterasi untuk PBKDF2-HMAC-SHA256.
    # Perangkat lemah (Termux) bisa menurunkannya, tetapi tidak di bawah 100.000.
    PBKDF2_ITERATIONS = max(100_000, int(os.environ.get("CCTIPI_PBKDF2_ITERATIONS", "600000")))
except ValueError:
    PBKDF2_ITERATIONS = 600_000
_HASH_PREFIX = "pbkdf2_sha256"


def _pbkdf2(password: str, salt_val: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_val.encode("utf-8"), iterations).hex()


def hash_password(password: str, salt: str | None = None, iterations: int | None = None) -> str:
    """Format: pbkdf2_sha256$<iterasi>$<salt>$<hex>. Format lama "<salt>$<hex>" (100k) tetap bisa diverifikasi."""
    salt_val = salt or os.urandom(16).hex()
    iters = iterations or PBKDF2_ITERATIONS
    return f"{_HASH_PREFIX}${iters}${salt_val}${_pbkdf2(password, salt_val, iters)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) == 4 and parts[0] == _HASH_PREFIX:
            iters = int(parts[1])
            if not (1_000 <= iters <= 10_000_000):
                return False
            return hmac.compare_digest(_pbkdf2(password, parts[2], iters), parts[3])
        if len(parts) == 2:
            return hmac.compare_digest(_pbkdf2(password, parts[0], LEGACY_PBKDF2_ITERATIONS), parts[1])
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    parts = (stored_hash or "").split("$")
    return not (len(parts) == 4 and parts[0] == _HASH_PREFIX and parts[1].isdigit() and int(parts[1]) >= PBKDF2_ITERATIONS)


# hash tiruan agar waktu respons login sama walau username tidak ada
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _clean_regions(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        s = str(v).strip()[:64]
        if s and s not in out:
            out.append(s)
    return out[:100]


def _clean_permissions(value) -> list[str]:
    vals = value if isinstance(value, list) else []
    return [k for k in PERMISSION_KEYS if k in vals]


class UserStore:
    def __init__(self, path: Path, *, on_revoke: Callable[[str], None] | None = None):
        self._path = path
        self._lock = threading.RLock()
        self._cfg: dict | None = None
        self._default_pw: dict[str, bool] = {}
        self.on_revoke = on_revoke  # dipanggil saat sesi user harus dicabut

    # ---------- penyimpanan ----------
    def _default_cfg(self) -> dict:
        user = os.environ.get("CCTIPI_USER", "admin")
        pw = os.environ.get("CCTIPI_PASS", DEFAULT_PASSWORD)
        must_change = (pw == DEFAULT_PASSWORD)
        return {
            "version": 2,
            "users": [self._new_user(user, pw, "admin", must_change=must_change)],
            "roles": json.loads(json.dumps(DEFAULT_ROLES)),
        }

    @staticmethod
    def _new_user(username: str, password: str, role: str, *, regions=None,
                  must_change: bool = True, password_hash: str | None = None) -> dict:
        return {
            "username": username,
            "passwordHash": password_hash or hash_password(password),
            "role": role,
            "regions": _clean_regions(regions or []),
            "disabled": False,
            "mustChange": must_change,
            "createdAt": int(time.time()),
            "lastLogin": 0,
        }

    def _normalize(self, data: dict) -> dict:
        """Migrasi format lama + lengkapi role bawaan yang hilang."""
        if "users" not in data and data.get("username") and data.get("passwordHash"):
            pw_hash = str(data["passwordHash"])
            data = {
                "version": 2,
                "users": [self._new_user(str(data["username"]), "", "admin",
                                         must_change=self.is_default_password(pw_hash),
                                         password_hash=pw_hash)],
                "roles": [],
            }
        roles = [r for r in (data.get("roles") or []) if isinstance(r, dict) and r.get("id")]
        ids = {r["id"] for r in roles}
        for base in DEFAULT_ROLES:
            if base["id"] not in ids:
                roles.append(json.loads(json.dumps(base)))
        for r in roles:
            r["name"] = str(r.get("name") or r["id"])[:40]
            r["builtin"] = any(b["id"] == r["id"] for b in DEFAULT_ROLES)
            r["permissions"] = list(PERMISSION_KEYS) if r["id"] == "admin" else _clean_permissions(r.get("permissions"))
        users = []
        role_ids = {r["id"] for r in roles}
        for u in data.get("users") or []:
            if not isinstance(u, dict) or not u.get("username") or not u.get("passwordHash"):
                continue
            users.append({
                "username": str(u["username"]),
                "passwordHash": str(u["passwordHash"]),
                "role": u.get("role") if u.get("role") in role_ids else "viewer",
                "regions": _clean_regions(u.get("regions")),
                "disabled": bool(u.get("disabled")),
                "mustChange": bool(u.get("mustChange")),
                "createdAt": int(u.get("createdAt") or 0),
                "lastLogin": int(u.get("lastLogin") or 0),
            })
        if not users:
            return self._default_cfg()
        return {"version": 2, "users": users, "roles": roles}

    def _load_locked(self) -> dict:
        if self._cfg is not None:
            return self._cfg
        cfg = None
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = self._normalize(raw)
                    if raw.get("version") != 2:
                        write_private(self._path, json.dumps(cfg, indent=2))
            except Exception:
                cfg = None
        if cfg is None:
            cfg = self._default_cfg()
            write_private(self._path, json.dumps(cfg, indent=2))
        self._cfg = cfg
        return cfg

    def _save_locked(self, cfg: dict) -> None:
        write_private(self._path, json.dumps(cfg, indent=2))
        self._cfg = cfg
        # Cache dikunci oleh string hash (hasilnya tidak pernah berubah untuk hash
        # yang sama), jadi cukup buang entri hash yang sudah tidak dipakai — tidak
        # perlu menghitung ulang PBKDF2 semua user setiap kali auth.json disimpan.
        live = {u["passwordHash"] for u in cfg["users"]}
        self._default_pw = {h: v for h, v in self._default_pw.items() if h in live}

    def reload(self) -> None:
        with self._lock:
            self._cfg = None
            self._load_locked()

    def config(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._load_locked()))

    # ---------- pencarian ----------
    @staticmethod
    def _find(cfg: dict, username: str) -> dict | None:
        low = (username or "").lower()
        return next((u for u in cfg["users"] if u["username"].lower() == low), None)

    @staticmethod
    def _role(cfg: dict, role_id: str) -> dict:
        return next((r for r in cfg["roles"] if r["id"] == role_id), {"id": role_id, "name": role_id, "permissions": []})

    def _active_admins(self, cfg: dict) -> list[dict]:
        return [u for u in cfg["users"] if u["role"] == "admin" and not u["disabled"]]

    def is_default_password(self, stored: str) -> bool:
        if stored not in self._default_pw:
            self._default_pw[stored] = verify_password(DEFAULT_PASSWORD, stored)
        return self._default_pw[stored]

    def has_default_credentials(self) -> bool:
        """True bila masih ada akun aktif yang memakai password bawaan cctipi123."""
        with self._lock:
            cfg = self._load_locked()
            return any(
                not u["disabled"] and self.is_default_password(u["passwordHash"])
                for u in cfg["users"]
            )

    def context(self, username: str | None) -> dict | None:
        """Identitas efektif untuk satu permintaan (None = tidak sah/nonaktif)."""
        if not username:
            return None
        with self._lock:
            cfg = self._load_locked()
            u = self._find(cfg, username)
            if not u or u["disabled"]:
                return None
            role = self._role(cfg, u["role"])
            return {
                "username": u["username"],
                "role": role["id"],
                "roleName": role["name"],
                "permissions": set(role["permissions"]),
                "regions": list(u["regions"]) or None,  # None = semua wilayah
                "mustChange": bool(u["mustChange"]) or self.is_default_password(u["passwordHash"]),
            }

    # ---------- login ----------
    def authenticate(self, username: str, password: str) -> dict | None:
        with self._lock:
            cfg = self._load_locked()
            u = self._find(cfg, username)
            stored = u["passwordHash"] if u else _DUMMY_HASH
        ok = verify_password(password, stored)  # selalu dihitung: waktu seragam
        if not (u and ok) or u["disabled"]:
            return None
        with self._lock:
            cfg = self._load_locked()
            cur = self._find(cfg, u["username"])
            if cur:
                cur["lastLogin"] = int(time.time())
                if needs_rehash(cur["passwordHash"]):
                    # upgrade transparan hash lama (100k iterasi) ke parameter terbaru
                    cur["passwordHash"] = hash_password(password)
                try:
                    self._save_locked(cfg)
                except OSError:
                    pass
        return self.context(u["username"])

    # ---------- daftar publik (tanpa hash) ----------
    def public_users(self) -> list[dict]:
        with self._lock:
            cfg = self._load_locked()
            return [{
                "username": u["username"], "role": u["role"],
                "roleName": self._role(cfg, u["role"])["name"],
                "regions": list(u["regions"]), "disabled": u["disabled"],
                "mustChange": bool(u["mustChange"]) or self.is_default_password(u["passwordHash"]),
                "createdAt": u["createdAt"],
                "lastLogin": u["lastLogin"],
            } for u in cfg["users"]]

    def public_roles(self) -> list[dict]:
        with self._lock:
            cfg = self._load_locked()
            counts: dict[str, int] = {}
            for u in cfg["users"]:
                counts[u["role"]] = counts.get(u["role"], 0) + 1
            return [{**r, "userCount": counts.get(r["id"], 0)} for r in cfg["roles"]]

    # ---------- kelola user ----------
    def _check_password(self, pw: str) -> None:
        if len(pw) < MIN_PASSWORD_LEN:
            raise UserError(f"Password minimal {MIN_PASSWORD_LEN} karakter.")
        if len(pw) > 200:
            raise UserError("Password terlalu panjang.")
        if pw == DEFAULT_PASSWORD:
            raise UserError("Jangan memakai password bawaan. Pilih password lain.")

    # ---------- batas wewenang pengelola non-Admin ----------
    def _actor_limits(self, cfg: dict, actor: str | None) -> dict | None:
        """None = tanpa batas (Admin / pemanggil internal). Selain itu batas izin & wilayah pelaku."""
        if actor is None:
            return None
        a = self._find(cfg, actor)
        if not a or a["disabled"]:
            raise UserError("Akun pelaku tidak ditemukan atau nonaktif.")
        if a["role"] == "admin":
            return None
        return {
            "username": a["username"],
            "permissions": set(self._role(cfg, a["role"])["permissions"]),
            "regions": list(a["regions"]),  # [] = semua wilayah
        }

    def _assert_role_within(self, cfg: dict, role_id: str, limits: dict | None) -> None:
        if limits is None:
            return
        if role_id == "admin":
            raise UserError("Hanya Admin yang boleh memberi atau mengelola role Admin.")
        if not set(self._role(cfg, role_id)["permissions"]) <= limits["permissions"]:
            raise UserError("Tidak boleh memberi/mengelola role yang izinnya melebihi izin Anda sendiri.")

    @staticmethod
    def _assert_regions_within(regions: list[str], limits: dict | None) -> None:
        if limits is None or not limits["regions"]:
            return
        if not regions or not set(regions) <= set(limits["regions"]):
            raise UserError("Wilayah pengguna harus berada di dalam wilayah akun Anda sendiri.")

    def create_user(self, data: dict, actor: str | None = None) -> dict:
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        role = str(data.get("role") or "viewer")
        if not USERNAME_RE.match(username):
            raise UserError("Username 3–32 karakter: huruf, angka, titik, minus, atau garis bawah.")
        self._check_password(password)
        with self._lock:
            cfg = self._load_locked()
            if self._find(cfg, username):
                raise UserError(f"Username “{username}” sudah dipakai.")
            if not any(r["id"] == role for r in cfg["roles"]):
                raise UserError("Role tidak dikenal.")
            limits = self._actor_limits(cfg, actor)
            self._assert_role_within(cfg, role, limits)
            self._assert_regions_within(_clean_regions(data.get("regions") or []), limits)
            cfg = json.loads(json.dumps(cfg))
            cfg["users"].append(self._new_user(username, password, role, regions=data.get("regions"),
                                               must_change=bool(data.get("mustChange", True))))
            self._save_locked(cfg)
        return next(u for u in self.public_users() if u["username"] == username)

    def update_user(self, username: str, data: dict, actor: str) -> dict:
        revoke = False
        with self._lock:
            cfg = json.loads(json.dumps(self._load_locked()))
            u = self._find(cfg, username)
            if not u:
                raise UserError("Pengguna tidak ditemukan.")
            is_self = u["username"].lower() == (actor or "").lower()
            limits = self._actor_limits(cfg, actor)
            if limits is not None:
                if is_self:
                    raise UserError("Ubah akun sendiri lewat menu Akun saya.")
                self._assert_role_within(cfg, u["role"], limits)
                self._assert_regions_within(u["regions"], limits)
                if "regions" in data:
                    self._assert_regions_within(_clean_regions(data["regions"]), limits)
            if "role" in data:
                role = str(data["role"])
                if not any(r["id"] == role for r in cfg["roles"]):
                    raise UserError("Role tidak dikenal.")
                self._assert_role_within(cfg, role, limits)
                if is_self and role != u["role"]:
                    raise UserError("Tidak bisa mengubah role akun sendiri.")
                if role != u["role"]:
                    revoke = True
                u["role"] = role
            if "regions" in data:
                u["regions"] = _clean_regions(data["regions"])
            if "disabled" in data:
                if is_self and data["disabled"]:
                    raise UserError("Tidak bisa menonaktifkan akun sendiri.")
                u["disabled"] = bool(data["disabled"])
                revoke = revoke or u["disabled"]
            if data.get("password"):
                pw = str(data["password"])
                self._check_password(pw)
                u["passwordHash"] = hash_password(pw)
                u["mustChange"] = not is_self
                revoke = True
            if not self._active_admins(cfg):
                raise UserError("Minimal harus ada satu Admin aktif.")
            self._save_locked(cfg)
            name = u["username"]
        if revoke and self.on_revoke:
            self.on_revoke(name)
        return next(x for x in self.public_users() if x["username"] == name)

    def delete_user(self, username: str, actor: str) -> None:
        with self._lock:
            cfg = json.loads(json.dumps(self._load_locked()))
            u = self._find(cfg, username)
            if not u:
                raise UserError("Pengguna tidak ditemukan.")
            if u["username"].lower() == (actor or "").lower():
                raise UserError("Tidak bisa menghapus akun sendiri.")
            limits = self._actor_limits(cfg, actor)
            self._assert_role_within(cfg, u["role"], limits)
            self._assert_regions_within(u["regions"], limits)
            cfg["users"] = [x for x in cfg["users"] if x is not u]
            if not self._active_admins(cfg):
                raise UserError("Minimal harus ada satu Admin aktif.")
            self._save_locked(cfg)
            name = u["username"]
        if self.on_revoke:
            self.on_revoke(name)

    def change_own(self, username: str, current: str, new_username: str, new_password: str) -> str:
        """Ganti username/password milik sendiri. Mengembalikan username baru."""
        with self._lock:
            cfg = json.loads(json.dumps(self._load_locked()))
            u = self._find(cfg, username)
            if not u or not verify_password(current, u["passwordHash"]):
                raise PermissionError("Password saat ini salah.")
            new_username = (new_username or u["username"]).strip()
            if not USERNAME_RE.match(new_username):
                raise UserError("Username 3–32 karakter: huruf, angka, titik, minus, atau garis bawah.")
            other = self._find(cfg, new_username)
            if other and other is not u:
                raise UserError(f"Username “{new_username}” sudah dipakai.")
            self._check_password(new_password)
            old = u["username"]
            u["username"] = new_username
            u["passwordHash"] = hash_password(new_password)
            u["mustChange"] = False
            self._save_locked(cfg)
        if self.on_revoke:
            self.on_revoke(old)
            if new_username != old:
                self.on_revoke(new_username)
        return new_username

    # ---------- kelola role ----------
    def save_role(self, data: dict, actor: str | None = None) -> dict:
        name = str(data.get("name") or "").strip()[:40]
        role_id = str(data.get("id") or "").strip().lower()
        if not name:
            raise UserError("Nama role wajib diisi.")
        with self._lock:
            cfg = json.loads(json.dumps(self._load_locked()))
            if role_id == "admin":
                raise UserError("Role Admin bawaan selalu memiliki semua izin dan tidak bisa diubah.")
            limits = self._actor_limits(cfg, actor)
            existing = next((r for r in cfg["roles"] if r["id"] == role_id), None) if role_id else None
            if limits is not None:
                if existing is not None:
                    self._assert_role_within(cfg, existing["id"], limits)
                    actor_role = self._find(cfg, limits["username"])["role"]
                    if existing["id"] == actor_role:
                        raise UserError("Tidak bisa mengubah role yang sedang Anda pakai sendiri.")
                if not set(_clean_permissions(data.get("permissions"))) <= limits["permissions"]:
                    raise UserError("Tidak boleh memberi izin yang tidak Anda miliki sendiri.")
            if existing is None:
                base = role_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28] or "role"
                if not ROLE_ID_RE.match(base):
                    base = "role"
                rid, n = base, 2
                while any(r["id"] == rid for r in cfg["roles"]):
                    rid, n = f"{base}-{n}", n + 1
                existing = {"id": rid, "name": name, "builtin": False, "permissions": []}
                cfg["roles"].append(existing)
            existing["name"] = name
            existing["permissions"] = _clean_permissions(data.get("permissions"))
            self._save_locked(cfg)
            rid = existing["id"]
        return next(r for r in self.public_roles() if r["id"] == rid)

    def delete_role(self, role_id: str, actor: str | None = None) -> None:
        with self._lock:
            cfg = json.loads(json.dumps(self._load_locked()))
            role = next((r for r in cfg["roles"] if r["id"] == role_id), None)
            if not role:
                raise UserError("Role tidak ditemukan.")
            self._assert_role_within(cfg, role_id, self._actor_limits(cfg, actor))
            if role.get("builtin"):
                raise UserError("Role bawaan tidak bisa dihapus.")
            used = [u["username"] for u in cfg["users"] if u["role"] == role_id]
            if used:
                raise UserError(f"Role masih dipakai {len(used)} pengguna. Pindahkan dulu ke role lain.")
            cfg["roles"] = [r for r in cfg["roles"] if r["id"] != role_id]
            self._save_locked(cfg)

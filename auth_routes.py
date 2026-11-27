#!/usr/bin/env python3
"""Rute & logika domain autentikasi, sesi, rate-limit, RBAC, audit, dan preferensi."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from pathlib import Path

import audit as _audit
import users as _users
from storage_utils import write_private
from system_health import log_event

SESSION_COOKIE_NAME = "cctipi_session"
SESSION_TTL_SECONDS = 86400 * 7  # 7 hari


def _srv():
    return sys.modules.get("server")


class SessionStore:
    """Sesi sisi-server: cookie berisi token acak 256-bit, server hanya menyimpan SHA-256-nya."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = self._load()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        now = time.time()
        if not isinstance(raw, dict):
            return {}
        return {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and float(v.get("exp", 0)) > now and v.get("u")
        }

    def _save_locked(self) -> None:
        now = time.time()
        self._sessions = {k: v for k, v in self._sessions.items() if v["exp"] > now}
        write_private(self._path, json.dumps(self._sessions))

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[self._digest(token)] = {
                "u": username,
                "exp": int(time.time()) + SESSION_TTL_SECONDS,
                "created": int(time.time()),
            }
            self._save_locked()
        return token

    def verify(self, token: str) -> str | None:
        if not token or len(token) > 200:
            return None
        with self._lock:
            entry = self._sessions.get(self._digest(token))
            if not entry:
                return None
            if entry["exp"] <= time.time():
                self._sessions.pop(self._digest(token), None)
                return None
            return entry["u"]

    def revoke(self, token: str) -> None:
        with self._lock:
            if self._sessions.pop(self._digest(token or ""), None) is not None:
                self._save_locked()

    def revoke_user(self, username: str) -> None:
        """Cabut semua sesi milik satu user (dinonaktifkan/dihapus/ganti password)."""
        low = (username or "").lower()
        with self._lock:
            before = len(self._sessions)
            self._sessions = {k: v for k, v in self._sessions.items() if str(v.get("u", "")).lower() != low}
            if len(self._sessions) != before:
                self._save_locked()

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions = {}
            self._save_locked()


class LoginLimiter:
    """Batasi tebak-password: 5 gagal dalam 15 menit -> kunci berlipat (30 dtk..15 mnt).

    Kunci ``user:<nama>`` (lintas-IP) memakai ambang jauh lebih tinggi agar satu
    penyerang tidak bisa mengunci akun Admin hanya dengan 5 tebakan salah; tebakan
    dari satu IP tetap terkunci lewat kunci ``ip:`` / ``userip:`` (ambang 5).
    """

    MAX_FAILS = 5
    USER_MAX_FAILS = 30
    WINDOW = 15 * 60
    BASE_LOCK = 30
    MAX_LOCK = 15 * 60
    MAX_TRACKED_KEYS = 5000

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _threshold(self, key: str) -> int:
        return self.USER_MAX_FAILS if key.startswith("user:") else self.MAX_FAILS

    def _prune_locked(self, now: float) -> None:
        if len(self._fails) + len(self._locked_until) <= self.MAX_TRACKED_KEYS:
            return
        self._fails = {k: h for k, h in self._fails.items() if h and now - h[-1] < self.WINDOW}
        self._locked_until = {k: t for k, t in self._locked_until.items() if t > now}

    def retry_after(self, keys: list[str]) -> int:
        now = time.time()
        with self._lock:
            wait = max((self._locked_until.get(k, 0) - now for k in keys), default=0)
        return max(0, int(wait + 0.999))

    def fail(self, keys: list[str]) -> None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            for k in keys:
                hist = [t for t in self._fails.get(k, []) if now - t < self.WINDOW]
                hist.append(now)
                self._fails[k] = hist[-100:]
                extra = len(hist) - self._threshold(k)
                if extra >= 0:
                    self._locked_until[k] = now + min(self.MAX_LOCK, self.BASE_LOCK * (2 ** min(extra, 10)))

    def success(self, keys: list[str]) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
                self._locked_until.pop(k, None)


class SlidingWindowLimiter:
    """Pembatas laju permintaan (sliding window) untuk endpoint sensitif selain login."""

    MAX_TRACKED_KEYS = 5000
    _PRUNE_AGE = 3600

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check_and_record(self, keys: list[str], max_calls: int, window_seconds: float) -> int:
        """Kembalikan 0 bila diizinkan (dan dicatat), atau retry_after (detik) bila ditolak."""
        now = time.time()
        with self._lock:
            if len(self._hits) > self.MAX_TRACKED_KEYS:
                self._hits = {k: h for k, h in self._hits.items() if h and now - h[-1] < self._PRUNE_AGE}
            wait = 0.0
            trimmed: dict[str, list[float]] = {}
            for k in keys:
                hist = [t for t in self._hits.get(k, []) if now - t < window_seconds]
                trimmed[k] = hist
                if len(hist) >= max_calls:
                    oldest = hist[len(hist) - max_calls]
                    wait = max(wait, window_seconds - (now - oldest))
            if wait > 0:
                return max(1, int(wait + 0.999))
            for k, hist in trimmed.items():
                hist.append(now)
                self._hits[k] = hist
        return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Aturan rate-limit endpoint sensitif: (bucket, maks_permintaan, jendela_detik)
def rate_limit_rule_for(method: str, path: str) -> tuple[str, int, int] | None:
    if method == "POST" and path == "/api/auth/password":
        return ("pwchange", 10, 60)
    if method in ("POST", "DELETE") and (
        path in ("/api/users", "/api/roles")
        or path.startswith(("/api/users/", "/api/roles/"))
    ):
        return ("users_admin", 20, 60)
    if method == "POST" and path in ("/api/onvif/ptz", "/api/tuya/ptz", "/api/onvif/discover"):
        return ("ptz", 60, 60)
    if method == "DELETE" and (
        path == "/api/manual-cameras"
        or path.startswith(("/api/jepretan/", "/api/rekaman/"))
    ):
        return ("resource_delete", 30, 60)
    if method == "POST" and path in ("/api/sources", "/api/sources/reset", "/api/backup/import", "/api/manual-cameras"):
        return ("config_mutate", 15, 60)
    if method == "POST" and path == "/api/check-status":
        return ("probe", 30, 60)
    return None


_AUDIT_EXACT = {
    ("POST", "/api/auth/password"): "auth.password",
    ("POST", "/api/users"): "user.create",
    ("POST", "/api/roles"): "role.save",
    ("POST", "/api/rekaman/settings"): "rekaman.settings",
    ("POST", "/api/ai/settings"): "ai.settings",
    ("POST", "/api/monitor/settings"): "monitor.settings",
    ("POST", "/api/tuya/settings"): "tuya.settings",
    ("POST", "/api/sources"): "sources.save",
    ("POST", "/api/sources/reset"): "sources.reset",
    ("POST", "/api/manual-cameras"): "camera.add",
    ("POST", "/api/backup/import"): "backup.import",
    ("DELETE", "/api/manual-cameras"): "camera.delete",
}
_AUDIT_PREFIX = [
    ("POST", "/api/users/", "user.update"),
    ("POST", "/api/ai/zones/", "ai.zone"),
    ("DELETE", "/api/users/", "user.delete"),
    ("DELETE", "/api/roles/", "role.delete"),
    ("DELETE", "/api/rekaman/", "rekaman.delete"),
    ("DELETE", "/api/jepretan/", "jepretan.delete"),
]

_PERM_EXACT: dict[tuple[str, str], str | None] = {
    ("GET", "/api/rekaman"): "rekaman.view",
    ("GET", "/api/jepretan"): "jepretan.view",
    ("GET", "/api/sources"): "settings.system",
    ("GET", "/api/users"): "users.manage",
    ("GET", "/api/mjpeg-hub"): "settings.system",
    ("GET", "/api/audit"): "audit.view",
    ("GET", "/api/rekaman/find"): "rekaman.view",
    ("GET", "/api/ai/zones"): "settings.ai",
    ("GET", "/api/ai/snapshot"): "settings.ai",
    ("POST", "/api/monitor/settings"): "settings.ai",
    ("POST", "/api/rekaman/settings"): "settings.ai",
    ("POST", "/api/ai/settings"): "settings.ai",
    ("POST", "/api/sources"): "settings.system",
    ("POST", "/api/sources/reset"): "settings.system",
    ("POST", "/api/backup/import"): "settings.system",
    ("POST", "/api/manual-cameras"): "cameras.manage",
    ("POST", "/api/onvif/discover"): "cameras.manage",
    ("POST", "/api/onvif/ptz"): "ptz.control",
    ("GET", "/api/tuya/settings"): "settings.system",
    ("POST", "/api/tuya/settings"): "settings.system",
    ("POST", "/api/tuya/test"): "settings.system",
    ("POST", "/api/tuya/ptz"): "ptz.control",
    ("POST", "/api/users"): "users.manage",
    ("POST", "/api/roles"): "users.manage",
    ("DELETE", "/api/manual-cameras"): "cameras.manage",
}
_PERM_PREFIX: list[tuple[str, str, str]] = [
    ("GET", "/api/rekaman/", "rekaman.view"),
    ("GET", "/api/jepretan/", "jepretan.view"),
    ("GET", "/api/live-ai/", "liveai.use"),
    ("POST", "/api/live-ai/", "liveai.use"),
    ("POST", "/api/users/", "users.manage"),
    ("POST", "/api/ai/zones/", "settings.ai"),
    ("POST", "/api/roles/", "users.manage"),
    ("DELETE", "/api/rekaman/", "rekaman.delete"),
    ("DELETE", "/api/jepretan/", "jepretan.delete"),
    ("DELETE", "/api/users/", "users.manage"),
    ("DELETE", "/api/roles/", "users.manage"),
]
_PERM_FREE = {"/api/rekaman/status", "/api/rekaman/settings", "/api/live-ai/status"}


def permission_for(method: str, path: str) -> str | None:
    if (method, path) in _PERM_EXACT:
        return _PERM_EXACT[(method, path)]
    if path in _PERM_FREE:
        return None
    for m, prefix, perm in _PERM_PREFIX:
        if m == method and path.startswith(prefix):
            return perm
    return None


def audit_action_for(method: str, path: str) -> str | None:
    if (method, path) in _AUDIT_EXACT:
        return _AUDIT_EXACT[(method, path)]
    if method == "POST" and path.startswith("/api/live-ai/"):
        if path.endswith("/start"):
            return "liveai.start"
        if path.endswith("/stop"):
            return "liveai.stop"
    for m, prefix, action in _AUDIT_PREFIX:
        if m == method and path.startswith(prefix):
            return action
    return None


def prefs_path(username: str, prefs_dir: Path | None = None) -> Path:
    base = prefs_dir if prefs_dir is not None else _srv().PREFS_DIR
    digest = hashlib.sha256((username or "").lower().encode("utf-8")).hexdigest()[:32]
    return base / f"{digest}.json"


def settings_diff(before: dict, after: dict) -> str:
    """Ringkas perubahan pengaturan untuk log aktivitas."""
    def fmt(v):
        if isinstance(v, bool):
            return "on" if v else "off"
        if isinstance(v, (list, dict)):
            return f"{len(v)} item"
        return str(v)

    parts = []
    skip = {"catalog", "telegram_token_set", "telegram_configured", "telegram_env", "enhance_available"}
    for k in sorted(set(before) | set(after)):
        if k in skip:
            continue
        a, b = before.get(k), after.get(k)
        if a != b:
            if isinstance(a, dict) and isinstance(b, dict):
                changed = [x for x in set(a) | set(b) if a.get(x) != b.get(x)]
                parts.append(f"{k}: {', '.join(f'{x} {fmt(b.get(x))}' for x in sorted(changed)[:6])}")
            else:
                parts.append(f"{k}: {fmt(a)} → {fmt(b)}")
    return "; ".join(parts)[:900] or "tanpa perubahan"


def _tail(path: str) -> str:
    return urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])


# ---------- HTTP Route Handlers ----------

def handle_auth_me(handler) -> None:
    ctx = handler.get_ctx()
    info = {"authenticated": bool(ctx), "username": ctx["username"] if ctx else None}
    if ctx:
        info.update({
            "defaultPassword": ctx["mustChange"],
            "mustChange": ctx["mustChange"],
            "minPasswordLength": _users.MIN_PASSWORD_LEN,
            "role": ctx["role"],
            "roleName": ctx["roleName"],
            "permissions": sorted(ctx["permissions"]),
            "regions": ctx["regions"] or [],
        })
    handler._send_json(info)


def handle_login(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body(limit=10_000)
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
    except Exception as exc:
        log_event("WARNING", "auth", "Payload login tidak valid", ip=handler.client_ip(), error=str(exc))
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Permintaan login tidak valid.")
        return

    ip = handler.client_ip()
    keys = [f"ip:{ip}", f"userip:{username.lower()[:64]}|{ip}", f"user:{username.lower()[:64]}"]
    wait = srv.LOGIN_LIMITER.retry_after(keys)
    if wait:
        handler.send_rate_limited(wait)
        return

    ctx = srv.USERS.authenticate(username, password)
    if not ctx:
        srv.LOGIN_LIMITER.fail(keys)
        handler.audit("auth.login_failed", username[:64], ok=False, username=username[:64] or "-")
        log_event("WARNING", "auth", "Login gagal", username=username[:40], ip=handler.client_ip())
        handler.send_json_error(HTTPStatus.UNAUTHORIZED, "Username atau password salah.")
        return

    srv.LOGIN_LIMITER.success(keys)
    token = srv.SESSIONS.create(ctx["username"])
    handler.audit("auth.login", username=ctx["username"])
    resp_body = json.dumps({
        "ok": True,
        "username": ctx["username"],
        "role": ctx["role"],
        "defaultPassword": ctx["mustChange"],
        "mustChange": ctx["mustChange"],
    }).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_cors_headers()
    handler.send_header("Set-Cookie", handler.session_cookie_header(token, SESSION_TTL_SECONDS))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(resp_body)))
    handler.end_headers()
    handler.wfile.write(resp_body)


def handle_logout(handler) -> None:
    srv = _srv()
    ctx = handler.get_ctx()
    if ctx:
        handler.audit("auth.logout", username=ctx["username"])
    srv.SESSIONS.revoke(handler.session_token())
    resp_body = json.dumps({"ok": True}).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_cors_headers()
    handler.send_header("Set-Cookie", handler.session_cookie_header("", 0))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(resp_body)))
    handler.end_headers()
    handler.wfile.write(resp_body)


def handle_change_password(handler) -> None:
    srv = _srv()
    try:
        body = handler.read_json_body(limit=10_000)
    except Exception:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Permintaan tidak valid.")
        return
    new_user = str(body.get("username") or "").strip()
    new_pass = str(body.get("password") or "")
    current = str(body.get("currentPassword") or "")
    keys = [f"ip:{handler.client_ip()}", f"pwchange:{handler.ctx['username'].lower()}"]
    wait = srv.LOGIN_LIMITER.retry_after(keys)
    if wait:
        handler.send_rate_limited(wait)
        return
    try:
        new_user = srv.USERS.change_own(handler.ctx["username"], current, new_user, new_pass)
    except PermissionError as exc:
        srv.LOGIN_LIMITER.fail(keys)
        handler.send_json_error(HTTPStatus.FORBIDDEN, str(exc))
        return
    except _users.UserError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
        return
    except OSError as exc:
        log_event("ERROR", "auth", "Gagal menulis auth.json", error=str(exc))
        handler.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Gagal menyimpan kredensial di server.")
        return

    srv.LOGIN_LIMITER.success(keys)
    old_user = handler.ctx["username"]
    if new_user.lower() != old_user.lower():
        try:
            prefs_path(old_user).replace(prefs_path(new_user))
        except OSError:
            pass
        handler._audit_detail = f"username {old_user} → {new_user}"
    token = srv.SESSIONS.create(new_user)
    resp_body = json.dumps({"ok": True, "username": new_user}).encode("utf-8")
    handler._status = int(HTTPStatus.OK)
    handler._audit_after()
    handler.send_response(HTTPStatus.OK)
    handler.send_cors_headers()
    handler.send_header("Set-Cookie", handler.session_cookie_header(token, SESSION_TTL_SECONDS))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(resp_body)))
    handler.end_headers()
    handler.wfile.write(resp_body)


def handle_audit_list(handler, query: str) -> None:
    srv = _srv()
    params = urllib.parse.parse_qs(query)
    get_p = lambda k: params.get(k, [""])[0].strip()  # noqa: E731
    try:
        limit = int(get_p("limit") or 300)
    except ValueError:
        limit = 300
    handler._send_json({
        "items": srv.AUDIT.query(
            date=get_p("date"), username=get_p("user"),
            action=get_p("action"), q=get_p("q"), limit=limit,
        ),
        "actions": [{"key": k, "label": v} for k, v in _audit.ACTIONS.items()],
        "users": srv.AUDIT.users(),
        "retentionDays": _audit.RETENTION_DAYS,
    })


def handle_users_list(handler) -> None:
    srv = _srv()
    folders: list[str] = []
    for c in srv.get_cameras().get("cameras", []):
        f = c.get("folder") or ""
        if f and f not in folders:
            folders.append(f)
    handler._send_json({
        "users": srv.USERS.public_users(),
        "roles": srv.USERS.public_roles(),
        "permissions": [{"key": k, "label": lbl, "group": g} for k, lbl, g in _users.PERMISSIONS],
        "regions": folders,
        "me": handler.ctx["username"],
    })


def _users_call(handler, fn) -> None:
    try:
        result = fn()
    except _users.UserError as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
        return
    except ValueError:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, "Permintaan tidak valid.")
        return
    except OSError as exc:
        log_event("ERROR", "auth", "Gagal menyimpan data pengguna", error=str(exc))
        handler.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Gagal menyimpan data pengguna di server.")
        return
    handler._send_json({"ok": True, **(result or {})})


def handle_user_create(handler) -> None:
    srv = _srv()

    def run():
        body = handler.read_json_body(limit=20_000)
        user = srv.USERS.create_user(body, actor=handler.ctx["username"])
        handler._audit_target = user["username"]
        handler._audit_detail = f"role {user['roleName']} · wilayah {', '.join(user['regions']) or 'semua'}"
        log_event("INFO", "auth", "User dibuat", actor=handler.ctx["username"], user=user["username"], role=user["role"])
        return {"user": user}

    _users_call(handler, run)


def handle_user_update(handler, path: str) -> None:
    srv = _srv()
    name = _tail(path)

    def run():
        body = handler.read_json_body(limit=20_000)
        user = srv.USERS.update_user(name, body, handler.ctx["username"])
        bits = []
        if "role" in body:
            bits.append(f"role {user['roleName']}")
        if "regions" in body:
            bits.append(f"wilayah {', '.join(user['regions']) or 'semua'}")
        if "disabled" in body:
            bits.append("nonaktif" if user["disabled"] else "aktif")
        if body.get("password"):
            bits.append("password direset")
        handler._audit_detail = " · ".join(bits)
        log_event("INFO", "auth", "User diubah", actor=handler.ctx["username"], user=name)
        return {"user": user}

    _users_call(handler, run)


def handle_user_delete(handler, path: str) -> None:
    srv = _srv()
    name = _tail(path)

    def run():
        srv.USERS.delete_user(name, handler.ctx["username"])
        try:
            prefs_path(name).unlink()
        except OSError:
            pass
        log_event("INFO", "auth", "User dihapus", actor=handler.ctx["username"], user=name)
        return {}

    _users_call(handler, run)


def handle_role_save(handler) -> None:
    srv = _srv()

    def run():
        role = srv.USERS.save_role(handler.read_json_body(limit=20_000), actor=handler.ctx["username"])
        handler._audit_target = role["name"]
        handler._audit_detail = ", ".join(role["permissions"]) or "tanpa izin"
        return {"role": role}

    _users_call(handler, run)


def handle_role_delete(handler, path: str) -> None:
    srv = _srv()
    role_id = _tail(path)
    _users_call(handler, lambda: (srv.USERS.delete_role(role_id, actor=handler.ctx["username"]), {})[1])


def handle_prefs_get(handler) -> None:
    try:
        prefs = json.loads(prefs_path(handler.ctx["username"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = None
    handler._send_json({"prefs": prefs})


def handle_prefs_post(handler) -> None:
    srv = _srv()
    try:
        prefs = handler.read_json_body(limit=500_000).get("prefs")
        if not isinstance(prefs, dict):
            raise ValueError("prefs harus objek")
        srv.PREFS_DIR.mkdir(parents=True, exist_ok=True)
        write_private(prefs_path(handler.ctx["username"]), json.dumps(prefs, ensure_ascii=False))
        handler._send_json({"ok": True})
    except (ValueError, OSError) as exc:
        handler.send_json_error(HTTPStatus.BAD_REQUEST, f"Preferensi tidak valid: {exc}")

"""
server.py — веб-сервер для дашборду
Запуск: python3 server.py  →  http://localhost:8000
"""

import http.server
import json
import os
import hashlib
import base64
import asyncio
import subprocess
import sys
import re
import html
import threading
import time
import secrets
import urllib.request
import urllib.parse
from urllib.parse import urlparse

from config import (
    SOURCES_FILE, SETTINGS_FILE, DATA_FILE, READ_FILE,
    LISTENER_FILE, SESSION_FILE,
    DEFAULT_SOURCES, DEFAULT_SETTINGS, APP_VERSION
)
from storage import Storage
from utils import RetryConfig, retry_call, setup_logging, env_secret

PORT = 8000
LOGGER = setup_logging(os.getenv("NEWSMONITOR_LOG_LEVEL", "INFO"))
STORAGE = Storage()
AUTH_USER = os.getenv("NEWSMONITOR_AUTH_USER", "").strip()
AUTH_PASS = os.getenv("NEWSMONITOR_AUTH_PASS", "").strip()

# ── Стан fetcher-а ────────────────────────────────────────────────────────────
_fetcher_lock   = threading.Lock()
_fetcher_status = {
    "running":       False,
    "started_at":    None,
    "finished_at":   None,
    "error":         None,
    "next_fetch_at": None,
}

_auto_timer:   threading.Timer | None = None
_digest_timer: threading.Timer | None = None

# ── Стан авторизації Telegram ─────────────────────────────────────────────────
# Тримаємо TelegramClient між кроками send_code → sign_in
_tg_auth: dict = {
    "client":   None,
    "phone":    None,
    "phone_code_hash": None,
    "loop":     None,
}
_tg_auth_lock = threading.Lock()
_admin_sessions: dict[str, float] = {}
SESSION_TTL_SECONDS = 60 * 60 * 12


# ── Fetcher ───────────────────────────────────────────────────────────────────

def _run_fetcher_process():
    with _fetcher_lock:
        if _fetcher_status["running"]:
            return
        _fetcher_status["running"]     = True
        _fetcher_status["started_at"]  = time.time()
        _fetcher_status["finished_at"] = None
        _fetcher_status["error"]       = None

    def _do():
        try:
            result = subprocess.run(
                [sys.executable, "fetcher.py"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                _fetcher_status["error"] = (
                    result.stderr.strip() or result.stdout.strip() or
                    f"Fetcher exited with code {result.returncode}"
                )
                LOGGER.error(_fetcher_status["error"])
        except Exception as e:
            _fetcher_status["error"] = str(e)
        finally:
            with _fetcher_lock:
                _fetcher_status["running"]     = False
                _fetcher_status["finished_at"] = time.time()

    threading.Thread(target=_do, daemon=True).start()


def _schedule_auto_fetch(interval_minutes: int):
    global _auto_timer
    if _auto_timer:
        _auto_timer.cancel()
        _auto_timer = None
    if interval_minutes > 0:
        next_at = time.time() + interval_minutes * 60
        _fetcher_status["next_fetch_at"] = next_at
        _auto_timer = threading.Timer(
            interval_minutes * 60, _auto_fetch_tick, args=[interval_minutes]
        )
        _auto_timer.daemon = True
        _auto_timer.start()
        print(f"  [AUTO] Збір RSS через {interval_minutes} хв")
    else:
        _fetcher_status["next_fetch_at"] = None


def _auto_fetch_tick(interval_minutes: int):
    print(f"  [AUTO] Збір о {time.strftime('%H:%M:%S')}")
    _run_fetcher_process()
    _schedule_auto_fetch(interval_minutes)


def _schedule_digest(digest_time: str, enabled: bool):
    global _digest_timer
    if _digest_timer:
        _digest_timer.cancel()
        _digest_timer = None
    if not enabled or not digest_time:
        return
    try:
        h, m   = map(int, digest_time.split(":"))
        now    = time.localtime()
        target = time.mktime(time.struct_time((
            now.tm_year, now.tm_mon, now.tm_mday,
            h, m, 0, now.tm_wday, now.tm_yday, now.tm_isdst
        )))
        if target <= time.time():
            target += 86400
        _digest_timer = threading.Timer(
            target - time.time(), _digest_tick, args=[digest_time]
        )
        _digest_timer.daemon = True
        _digest_timer.start()
        print(f"  [DIGEST] Заплановано на {digest_time}")
    except Exception as e:
        print(f"  [DIGEST] Помилка: {e}")


def _digest_tick(digest_time: str):
    s = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    if s.get("bot_token") and s.get("bot_chat_id"):
        _send_digest(s["bot_token"], s["bot_chat_id"],
                     max(1, int(s.get("digest_count", 5))))
    _schedule_digest(digest_time, True)


def _send_digest(bot_token: str, chat_id: str, count: int):
    try:
        data = {"items": STORAGE.load_items()}
        top = sorted(data.get("items", []),
                     key=lambda x: x.get("importance", 0), reverse=True)[:count]
        if not top:
            return
        lines = [f"<b>📰 Дайджест — топ {len(top)} новин</b>\n"]
        for i, item in enumerate(top, 1):
            line = f"{i}. <b>{item.get('title','')}</b> [{item.get('source','')} | {item.get('importance',5)}/10]"
            if item.get("url"):
                line += f"\n   <a href=\"{item['url']}\">Читати</a>"
            lines.append(line)
        _send_bot_message(bot_token, chat_id, "\n\n".join(lines))
        print(f"  [DIGEST] Надіслано {len(top)} новин")
    except Exception as e:
        print(f"  [DIGEST] {e}")


def _send_bot_message(bot_token: str, chat_id: str, text: str) -> bool:
    try:
        def _send():
            url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("ok", False)

        return retry_call(
            _send,
            RetryConfig(attempts=4, base_delay=1.0, max_delay=6.0, jitter=0.3),
            LOGGER,
            "telegram_bot_send",
        )
    except Exception as e:
        LOGGER.error(f"  [BOT] {e}")
        return False


# ── Утиліти ──────────────────────────────────────────────────────────────────

def load_json(path: str, default) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(default, dict):
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
            return data
        except (json.JSONDecodeError, OSError):
            pass
    write_json(path, default)
    return dict(default)

def write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_read_ids() -> set:
    ids = STORAGE.load_read_ids()
    if ids:
        return ids
    if not os.path.exists(READ_FILE):
        return set()
    try:
        with open(READ_FILE, "r", encoding="utf-8") as f:
            ids = set(json.load(f))
        STORAGE.save_read_ids(ids)
        return ids
    except Exception:
        return set()

def save_read_ids(ids: set) -> None:
    STORAGE.save_read_ids(ids)
    write_json(READ_FILE, sorted(ids))

def resolve_settings_with_env(settings: dict) -> dict:
    merged = dict(settings)
    merged["anthropic_api_key"] = env_secret("NEWSMONITOR_ANTHROPIC_API_KEY", merged.get("anthropic_api_key", ""))
    merged["telegram_api_hash"] = env_secret("NEWSMONITOR_TELEGRAM_API_HASH", merged.get("telegram_api_hash", ""))
    merged["bot_token"] = env_secret("NEWSMONITOR_BOT_TOKEN", merged.get("bot_token", ""))
    return merged

def get_listener_status() -> dict:
    if not os.path.exists(LISTENER_FILE):
        return {"status": "stopped", "updated_at": None, "error": ""}
    try:
        with open(LISTENER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        upd = data.get("updated_at", 0)
        if data.get("status") == "running" and time.time() - upd > 60:
            data["status"] = "unknown"
        return data
    except Exception:
        return {"status": "error", "updated_at": None, "error": "Cannot read status"}

def _normalize_tg_username(url_or_name: str) -> str:
    raw = (url_or_name or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        return raw[1:].lower()
    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.netloc or "").lower()
        if host.endswith("t.me") or host.endswith("telegram.me"):
            parts = [p for p in parsed.path.split("/") if p]
            if parts and parts[0] == "s":
                parts = parts[1:]
            if parts:
                return parts[0].lstrip("@").lower()
    return raw.rstrip("/").split("/")[-1].lstrip("@").lower()

def get_listener_diagnostics() -> dict:
    status = get_listener_status()
    diag = status.get("diagnostics") if isinstance(status, dict) else {}
    if not isinstance(diag, dict):
        diag = {}
    bound = diag.get("bound_channels", [])
    unbound = diag.get("unbound_channels", [])
    last_by_source = diag.get("last_message_by_source", {})
    if not isinstance(bound, list):
        bound = []
    if not isinstance(unbound, list):
        unbound = []
    if not isinstance(last_by_source, dict):
        last_by_source = {}

    bound_by_source = {str(x.get("source_id")): x for x in bound if isinstance(x, dict)}
    unbound_by_source = {str(x.get("source_id")): x for x in unbound if isinstance(x, dict)}

    sources = load_json(SOURCES_FILE, DEFAULT_SOURCES).get("telegram", [])
    items = []
    for src in sources:
        src_id = str(src.get("id", ""))
        items.append({
            "id": src_id,
            "name": src.get("name", ""),
            "url": src.get("url", ""),
            "enabled": bool(src.get("enabled", True)),
            "ai_enabled": bool(src.get("ai_enabled", True)),
            "username": _normalize_tg_username(str(src.get("url", ""))),
            "bound": src_id in bound_by_source,
            "binding": bound_by_source.get(src_id) or unbound_by_source.get(src_id) or {},
            "last_message": last_by_source.get(src_id),
        })

    return {
        "ok": True,
        "status": status.get("status", "unknown"),
        "updated_at": status.get("updated_at"),
        "sources": items,
        "bound_channels": bound,
        "unbound_channels": unbound,
    }

def load_sources_with_defaults() -> dict:
    sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
    changed = False
    for t in ("rss", "telegram"):
        for s in sources.get(t, []):
            if "ai_enabled" not in s:
                s["ai_enabled"] = True
                changed = True
    if changed:
        write_json(SOURCES_FILE, sources)
    return sources


def detect_telegram_channel_name(username: str) -> str:
    """Пробує підтягнути назву Telegram-каналу з публічної сторінки t.me."""
    if not username:
        return ""
    try:
        url = f"https://t.me/{username}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (NewsMonitor)"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'<meta\\s+property=\"og:title\"\\s+content=\"([^\"]+)\"', body, re.IGNORECASE)
        if m:
            title = html.unescape(m.group(1)).strip()
            if title and title.lower() != "telegram":
                return title
    except Exception:
        pass
    return ""


# ── Авторизація Telegram ──────────────────────────────────────────────────────

def _tg_auth_send_code(phone: str, api_id: int, api_hash: str) -> dict:
    """Крок 1: надсилає код підтвердження на номер телефону."""
    from telethon.sync import TelegramClient as SyncClient
    try:
        # закриваємо попередній клієнт якщо є
        _cleanup_tg_auth()

        client = SyncClient(SESSION_FILE, api_id, api_hash)
        client.connect()

        if client.is_user_authorized():
            client.disconnect()
            return {"ok": True, "already_authorized": True}

        result = client.send_code_request(phone)
        with _tg_auth_lock:
            _tg_auth["client"]          = client
            _tg_auth["phone"]           = phone
            _tg_auth["phone_code_hash"] = result.phone_code_hash
        return {"ok": True, "already_authorized": False}
    except Exception as e:
        _cleanup_tg_auth()
        return {"ok": False, "error": str(e)}


def _tg_auth_sign_in(code: str, password: str = "") -> dict:
    """Крок 2: підтверджує код (і пароль 2FA якщо є)."""
    from telethon.errors import (
        PhoneCodeInvalidError, PhoneCodeExpiredError,
        SessionPasswordNeededError
    )
    with _tg_auth_lock:
        client          = _tg_auth.get("client")
        phone           = _tg_auth.get("phone")
        phone_code_hash = _tg_auth.get("phone_code_hash")

    if not client or not phone:
        return {"ok": False, "error": "Спочатку надішліть код (крок 1)"}

    try:
        client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        client.disconnect()
        _cleanup_tg_auth()
        return {"ok": True}
    except SessionPasswordNeededError:
        if not password:
            return {"ok": False, "need_password": True}
        try:
            client.sign_in(password=password)
            client.disconnect()
            _cleanup_tg_auth()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        return {"ok": False, "error": "Невірний або прострочений код"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _cleanup_tg_auth():
    with _tg_auth_lock:
        client = _tg_auth.get("client")
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        _tg_auth["client"]          = None
        _tg_auth["phone"]           = None
        _tg_auth["phone_code_hash"] = None


def _tg_auth_logout() -> dict:
    """Видаляє сесію Telegram."""
    _cleanup_tg_auth()
    session_path = SESSION_FILE + ".session"
    if os.path.exists(session_path):
        try:
            os.remove(session_path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True}


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(http.server.SimpleHTTPRequestHandler):
    def _auth_required(self) -> bool:
        return bool(AUTH_USER and AUTH_PASS)

    def _authorized(self) -> bool:
        if not self._auth_required():
            return True
        # session-cookie auth
        token = self._get_cookie("nm_admin")
        if token:
            exp = _admin_sessions.get(token)
            if exp and exp > time.time():
                return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            user, pwd = raw.split(":", 1)
        except Exception:
            return False
        return user == AUTH_USER and pwd == AUTH_PASS

    def _deny_auth(self):
        accept = (self.headers.get("Accept", "") or "").lower()
        if "text/html" in accept:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        self.send_json(
            {"error": "auth_required"},
            401,
            extra_headers=[("WWW-Authenticate", 'Basic realm="NewsMonitor"')],
        )

    def _get_cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            p = part.strip()
            if p.startswith(name + "="):
                return p.split("=", 1)[1]
        return ""

    def _create_admin_session(self) -> str:
        token = secrets.token_urlsafe(24)
        _admin_sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def _clear_admin_session(self):
        token = self._get_cookie("nm_admin")
        if token and token in _admin_sessions:
            _admin_sessions.pop(token, None)

    def _require_admin(self) -> bool:
        if not self._auth_required():
            return True
        if self._authorized():
            return True
        self._deny_auth()
        return False


    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._deny_auth()
            return
        p = urlparse(self.path).path
        admin_only = {
            "/api/settings",
            "/api/sources",
            "/api/refresh",
            "/api/tg/session",
            "/api/listener/diagnostics",
        }
        if p in admin_only and not self._require_admin():
            return
        routes = {
            "/api/news":            self._serve_news,
            "/api/sources":         lambda: self.send_json(load_sources_with_defaults()),
            "/api/settings":        self._serve_settings,
            "/api/dashboard/config": self._serve_dashboard_config,
            "/api/me":              lambda: self.send_json({"admin": self._authorized() if self._auth_required() else True}),
            "/api/version":         lambda: self.send_json({"version": APP_VERSION}),
            "/api/status":          lambda: self.send_json(dict(_fetcher_status)),
            "/api/health":          self._serve_health,
            "/api/listener/status": lambda: self.send_json(get_listener_status()),
            "/api/listener/diagnostics": lambda: self.send_json(get_listener_diagnostics()),
            "/api/refresh":         self._start_fetcher,
            "/api/tg/session":      self._tg_session_status,
        }
        if p in routes:
            routes[p]()
        else:
            if p in ("/", ""):
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        if not self._authorized():
            self._deny_auth()
            return
        p    = urlparse(self.path).path
        body = self._read_body()
        admin_only = {
            "/api/sources",
            "/api/sources/toggle",
            "/api/sources/ai_toggle",
            "/api/sources/rename",
            "/api/settings",
            "/api/news/read",
            "/api/news/unread",
            "/api/news/clear_read",
            "/api/news/send",
            "/api/tg/send_code",
            "/api/tg/sign_in",
            "/api/tg/logout",
        }
        if p in admin_only and not self._require_admin():
            return
        routes = {
            "/api/sources":          lambda: self._add_source(body),
            "/api/sources/toggle":   lambda: self._toggle_source(body),
            "/api/sources/ai_toggle": lambda: self._toggle_source_ai(body),
            "/api/sources/rename":   lambda: self._rename_source(body),
            "/api/settings":         lambda: self._save_settings(body),
            "/api/news/read":        lambda: self._mark_read(body),
            "/api/news/unread":      lambda: self._mark_unread(body),
            "/api/news/clear_read":  lambda: (save_read_ids(set()), self.send_json({"ok": True})),
            "/api/news/send":        lambda: self._send_news(body),
            "/api/tg/send_code":     lambda: self._tg_send_code(body),
            "/api/tg/sign_in":       lambda: self._tg_sign_in(body),
            "/api/tg/logout":        lambda: self.send_json(_tg_auth_logout()),
            "/api/login":            lambda: self._login(body),
            "/api/logout":           self._logout,
        }
        if p in routes:
            routes[p]()
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        if not self._authorized():
            self._deny_auth()
            return
        p    = urlparse(self.path).path
        if p == "/api/sources" and not self._require_admin():
            return
        body = self._read_body()
        if p == "/api/sources":
            self._delete_source(body)
        else:
            self.send_json({"error": "Not found"}, 404)

    # ── GET ───────────────────────────────────────────────────────────────────

    def _serve_news(self):
        try:
            new_count = int(STORAGE.get_kv("new_count", 0) or 0)
            payload = STORAGE.export_news_payload(
                ai_enabled=bool(resolve_settings_with_env(load_json(SETTINGS_FILE, DEFAULT_SETTINGS)).get("ai_enabled", False)),
                new_count=max(0, new_count),
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except OSError as e:
            self.send_json({"error": str(e)}, 500)

    def _serve_settings(self):
        s    = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        s    = resolve_settings_with_env(s)
        safe = {k: v for k, v in s.items()
                if k not in ("anthropic_api_key", "telegram_api_hash", "bot_token")}
        safe["has_anthropic_key"] = bool(s.get("anthropic_api_key"))
        safe["has_telegram_hash"] = bool(s.get("telegram_api_hash"))
        safe["has_bot_token"]     = bool(s.get("bot_token"))
        safe["auth_enabled"]      = self._auth_required()
        self.send_json(safe)

    def _serve_dashboard_config(self):
        s = resolve_settings_with_env(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
        self.send_json({
            "categories": s.get("categories", []),
            "keywords":   s.get("keywords", []),
        })

    def _login(self, body: dict):
        if not self._auth_required():
            self.send_json({"ok": True, "admin": True})
            return
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", "")).strip()
        if username == AUTH_USER and password == AUTH_PASS:
            token = self._create_admin_session()
            self.send_json(
                {"ok": True, "admin": True},
                extra_headers=[("Set-Cookie", f"nm_admin={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}")],
            )
            return
        self.send_json({"ok": False, "error": "Невірний логін або пароль"}, 401)

    def _logout(self):
        self._clear_admin_session()
        self.send_json(
            {"ok": True},
            extra_headers=[("Set-Cookie", "nm_admin=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")],
        )

    def _serve_health(self):
        self.send_json({
            "ok": True,
            "ts": time.time(),
            "fetcher_running": _fetcher_status["running"],
            "listener": get_listener_status().get("status", "unknown"),
            "db_path": STORAGE.path,
        })

    def _tg_session_status(self):
        has_session = os.path.exists(SESSION_FILE + ".session")
        if not has_session:
            self.send_json({"authorized": False}); return
        # перевіряємо чи сесія дійсна
        s = resolve_settings_with_env(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
        api_id   = int(s.get("telegram_api_id",   0) or 0)
        api_hash = s.get("telegram_api_hash", "")
        if not api_id or not api_hash:
            self.send_json({"authorized": False, "error": "Не вказано API ID/Hash"}); return
        try:
            from telethon.sync import TelegramClient as SyncClient
            client = SyncClient(SESSION_FILE, api_id, api_hash)
            client.connect()
            authorized = client.is_user_authorized()
            me = None
            if authorized:
                try:
                    user = client.get_me()
                    me   = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
                except Exception:
                    pass
            client.disconnect()
            self.send_json({"authorized": authorized, "name": me})
        except Exception as e:
            self.send_json({"authorized": False, "error": str(e)})

    def _start_fetcher(self):
        with _fetcher_lock:
            if _fetcher_status["running"]:
                self.send_json({"status": "already_running"}); return
        _run_fetcher_process()
        self.send_json({"status": "started"})

    # ── POST ──────────────────────────────────────────────────────────────────

    def _tg_send_code(self, body: dict):
        phone    = str(body.get("phone", "")).strip()
        s        = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        api_id   = int(s.get("telegram_api_id",   0) or 0)
        api_hash = s.get("telegram_api_hash", "")
        if not phone:
            self.send_json({"ok": False, "error": "Вкажіть номер телефону"}); return
        if not api_id or not api_hash:
            self.send_json({"ok": False, "error": "Спочатку вкажіть Telegram API ID та Hash"}); return

        def do():
            result = _tg_auth_send_code(phone, api_id, api_hash)
            return result

        result = do()
        self.send_json(result)

    def _tg_sign_in(self, body: dict):
        code     = str(body.get("code", "")).strip()
        password = str(body.get("password", "")).strip()
        if not code:
            self.send_json({"ok": False, "error": "Вкажіть код підтвердження"}); return
        result = _tg_auth_sign_in(code, password)
        self.send_json(result)

    def _add_source(self, body: dict):
        src_type = body.get("type", "")
        name     = str(body.get("name", "")).strip()
        url      = str(body.get("url",  "")).strip()
        if src_type not in ("rss", "telegram") or not url:
            self.send_json({"error": "Заповніть всі поля"}, 400); return
        if src_type == "rss" and not name:
            self.send_json({"error": "Для RSS вкажіть назву"}, 400); return
        if src_type == "telegram":
            if url.startswith("@"):
                url = "https://t.me/" + url[1:]
            elif not url.startswith("http"):
                url = "https://t.me/" + url
            src_id = url.rstrip("/").split("/")[-1].lstrip("@").lower()
            if not name:
                auto_name = detect_telegram_channel_name(src_id)
                name = auto_name or src_id
        else:
            src_id = "rss_" + hashlib.md5(url.encode()).hexdigest()[:8]
        sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        if any(s["id"] == src_id for s in sources.get(src_type, [])):
            self.send_json({"error": f"Джерело вже існує (id: {src_id})"}, 409); return
        new_src = {"id": src_id, "name": name, "url": url, "enabled": True, "ai_enabled": True}
        sources.setdefault(src_type, []).append(new_src)
        write_json(SOURCES_FILE, sources)
        print(f"  [API] Додано {src_type}: {src_id}")
        self.send_json({"ok": True, "source": new_src})

    def _toggle_source(self, body: dict):
        src_type = body.get("type", "")
        src_id   = body.get("id",   "")
        if not src_type or not src_id:
            self.send_json({"error": "Невірні параметри"}, 400); return
        sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        for s in sources.get(src_type, []):
            if s["id"] == src_id:
                s["enabled"] = not s["enabled"]
                write_json(SOURCES_FILE, sources)
                self.send_json({"ok": True, "enabled": s["enabled"]}); return
        self.send_json({"error": "Не знайдено"}, 404)

    def _toggle_source_ai(self, body: dict):
        src_type = body.get("type", "")
        src_id   = body.get("id",   "")
        if not src_type or not src_id:
            self.send_json({"error": "Невірні параметри"}, 400); return
        sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        for s in sources.get(src_type, []):
            if s["id"] == src_id:
                s["ai_enabled"] = not bool(s.get("ai_enabled", True))
                write_json(SOURCES_FILE, sources)
                self.send_json({"ok": True, "ai_enabled": s["ai_enabled"]}); return
        self.send_json({"error": "Не знайдено"}, 404)

    def _rename_source(self, body: dict):
        src_type = str(body.get("type", "")).strip()
        src_id   = str(body.get("id", "")).strip()
        name     = str(body.get("name", "")).strip()
        if src_type not in ("rss", "telegram") or not src_id or not name:
            self.send_json({"error": "Невірні параметри"}, 400); return
        sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        for s in sources.get(src_type, []):
            if s["id"] == src_id:
                s["name"] = name
                write_json(SOURCES_FILE, sources)
                self.send_json({"ok": True, "name": name}); return
        self.send_json({"error": "Не знайдено"}, 404)

    def _delete_source(self, body: dict):
        src_type = body.get("type", "")
        src_id   = body.get("id",   "")
        if not src_type or not src_id:
            self.send_json({"error": "Невірні параметри"}, 400); return
        sources  = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        original = sources.get(src_type, [])
        filtered = [s for s in original if s["id"] != src_id]
        if len(filtered) == len(original):
            self.send_json({"error": "Не знайдено"}, 404); return
        sources[src_type] = filtered
        write_json(SOURCES_FILE, sources)
        STORAGE.delete_items_by_source_id(src_id)
        self.send_json({"ok": True})

    def _save_settings(self, body: dict):
        settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)

        # булеві
        for k in ("ai_enabled", "digest_enabled", "listener_enabled"):
            if k in body:
                settings[k] = bool(body[k])

        # числові
        for k in ("rss_depth", "auto_fetch_interval",
                  "keep_days", "max_items", "digest_count"):
            if k in body:
                try:
                    settings[k] = max(0, int(body[k]))
                except (ValueError, TypeError):
                    pass

        if "telegram_api_id" in body:
            try:
                settings["telegram_api_id"] = int(body["telegram_api_id"])
            except (ValueError, TypeError):
                pass

        # рядкові
        for k in ("ai_model", "digest_time", "bot_chat_id", "importance_priorities"):
            if k in body:
                settings[k] = str(body[k])

        # секрети — зберігаємо тільки якщо не порожні
        for k in ("anthropic_api_key", "telegram_api_hash", "bot_token"):
            val = str(body.get(k, "")).strip()
            if val:
                settings[k] = val

        # категорії
        if "categories" in body and isinstance(body["categories"], list):
            cats = []
            for c in body["categories"]:
                cid   = str(c.get("id",    "")).strip()
                cname = str(c.get("name",  "")).strip()
                color = str(c.get("color", "#888888")).strip()
                if cid and cname:
                    cats.append({"id": cid, "name": cname, "color": color})
            settings["categories"] = cats  # зберігаємо навіть порожній список

        # ключові слова
        if "keywords" in body and isinstance(body["keywords"], list):
            kws = []
            for kw in body["keywords"]:
                phrase = str(kw.get("phrase", "")).strip()
                urgent = bool(kw.get("urgent", False))
                if phrase:
                    kws.append({"id": phrase, "phrase": phrase, "urgent": urgent})
            settings["keywords"] = kws

        write_json(SETTINGS_FILE, settings)
        _schedule_auto_fetch(settings.get("auto_fetch_interval", 0))
        _schedule_digest(settings.get("digest_time", "09:00"),
                         settings.get("digest_enabled", False))
        self.send_json({"ok": True})

    def _send_news(self, body: dict):
        item_id = str(body.get("id", "")).strip()
        if not item_id:
            self.send_json({"error": "Немає id"}, 400); return
        s = resolve_settings_with_env(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
        bot_token   = s.get("bot_token", "")
        bot_chat_id = s.get("bot_chat_id", "")
        if not bot_token or not bot_chat_id:
            self.send_json({"error": "Налаштуйте Bot Token і Chat ID"}, 400); return
        data = {"items": STORAGE.load_items()}
        item = next((it for it in data.get("items", []) if it["id"] == item_id), None)
        if not item:
            self.send_json({"error": "Новину не знайдено"}, 404); return
        imp  = item.get("importance", 5)
        lines = [f"📤 <b>{item['title']}</b>", ""]
        if item.get("summary"):
            lines.append(item["summary"])
            lines.append("")
        lines.append(f"Джерело: {item['source']} | {imp}/10")
        if item.get("url"):
            lines.append(f"<a href=\"{item['url']}\">Читати →</a>")
        ok = _send_bot_message(bot_token, bot_chat_id, "\n".join(lines))
        self.send_json({"ok": True} if ok else
                       {"error": "Не вдалося надіслати. Перевірте токен і chat_id"})

    def _mark_read(self, body: dict):
        item_id = str(body.get("id", "")).strip()
        if not item_id:
            self.send_json({"error": "Немає id"}, 400); return
        ids = load_read_ids(); ids.add(item_id)
        save_read_ids(ids); self.send_json({"ok": True})

    def _mark_unread(self, body: dict):
        item_id = str(body.get("id", "")).strip()
        if not item_id:
            self.send_json({"error": "Немає id"}, 400); return
        ids = load_read_ids(); ids.discard(item_id)
        save_read_ids(ids); self.send_json({"ok": True})

    # ── Відповіді ─────────────────────────────────────────────────────────────

    def send_json(self, data, code: int = 200, extra_headers: list[tuple[str, str]] | None = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)

    interval = settings.get("auto_fetch_interval", 0)
    if interval > 0:
        print(f"Автозбір: кожні {interval} хв")
        _schedule_auto_fetch(interval)

    if settings.get("digest_enabled"):
        _schedule_digest(settings.get("digest_time", "09:00"), True)

    print(f"Сервер: http://localhost:{PORT}")
    print("Ctrl+C — зупинити\n")
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()

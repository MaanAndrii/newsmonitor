"""
listener.py — слухач Telegram каналів в реальному часі
Запуск: python3 listener.py
Потребує попередньої авторизації через веб-інтерфейс (Налаштування → Авторизація Telegram)
"""

import asyncio
import json
import os
import hashlib
import re
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.utils import get_peer_id

from config import (
    SOURCES_FILE, SETTINGS_FILE, DATA_FILE, SESSION_FILE,
    SEEN_FILE, LISTENER_FILE, LOCK_FILE,
    DEFAULT_SOURCES, DEFAULT_SETTINGS, DEFAULT_AI_MODEL,
    IMPORTANCE_CRITERIA
)
from storage import Storage
from utils import RetryConfig, retry_call, setup_logging, env_secret

LOGGER = setup_logging(os.getenv("NEWSMONITOR_LOG_LEVEL", "INFO"))
STORAGE = Storage()


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
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [WARN] {path}: {e}")
    _write_json(path, default)
    return dict(default) if isinstance(default, dict) else default

def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_seen_ids() -> set:
    ids = STORAGE.load_seen_ids()
    if ids:
        return ids
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            ids = set(json.load(f))
        STORAGE.save_seen_ids(ids)
        return ids
    except Exception:
        return set()

def save_seen_ids(ids: set) -> None:
    STORAGE.save_seen_ids(ids)
    _write_json(SEEN_FILE, list(ids)[-10000:])

def write_status(status: str, error: str = "", extra: dict | None = None) -> None:
    """Записує статус слухача — server.py читає кожні 5 сек."""
    payload = {
        "status":     status,
        "updated_at": time.time(),
        "error":      error,
        "pid":        os.getpid(),
    }
    if extra:
        payload.update(extra)
    _write_json(LISTENER_FILE, payload)

def _normalize_channel_username(url_or_name: str) -> str:
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


# ── Ключові слова ─────────────────────────────────────────────────────────────

def match_keywords(text: str, keywords: list) -> list:
    if not keywords:
        return []
    text_lower = text.lower()
    return [
        kw["phrase"] for kw in keywords
        if kw.get("phrase", "").strip().lower() in text_lower
    ]


# ── Telegram Bot ──────────────────────────────────────────────────────────────

def send_bot_message(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id or not text:
        return False
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


# ── AI аналіз одного повідомлення ────────────────────────────────────────────

def analyze_single(item: dict, api_key: str, categories: list,
                   model: str, priorities: str) -> dict:
    """Аналізує одне повідомлення через Claude."""
    if not categories:
        return {}

    client   = Anthropic(api_key=api_key)
    cat_list = ", ".join(f'"{c["id"]}" ({c["name"]})' for c in categories)
    cat_ids  = [c["id"] for c in categories]

    priorities_block = (
        f"\nПерсональні пріоритети редакції:\n{priorities.strip()}\n"
        if priorities.strip() else ""
    )

    prompt = (
        "Проаналізуй новину та поверни JSON об'єкт.\n\n"
        f"Джерело: {item['source']}\n"
        f"Заголовок: {item['title']}\n"
        f"Текст: {item.get('text', '')[:600]}\n\n"
        f"{IMPORTANCE_CRITERIA}\n"
        f"{priorities_block}\n"
        "Поверни:\n"
        '{'
        f'"category":одне з [{cat_list}],'
        '"importance":1-10,"is_duplicate":false}\n\n'
        "ТІЛЬКИ JSON об'єкт, без пояснень та markdown."
    )

    response = retry_call(
        lambda: client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        ),
        RetryConfig(attempts=3, base_delay=1.5, max_delay=8.0, jitter=0.3),
        LOGGER,
        "anthropic_single_analyze",
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    result = json.loads(raw)

    if result.get("category") not in cat_ids:
        result["category"] = cat_ids[0]
    return result


# ── Запис новини в news_data.json ─────────────────────────────────────────────

_data_lock = threading.Lock()

def append_item(item: dict, keep_days: int, max_items: int) -> None:
    """Атомарно додає одну новину у SQLite + синхронізує legacy JSON."""
    with _data_lock:
        existing_ids = {x["id"] for x in STORAGE.load_items()}
        if item["id"] in existing_ids:
            return
        STORAGE.append_item(item)
        result = STORAGE.cleanup(keep_days, max_items)
        prev_new = int(STORAGE.get_kv("new_count", 0) or 0)
        STORAGE.set_kv("new_count", prev_new + 1)
        _write_json(DATA_FILE, {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total":      len(result),
            "new_count":  prev_new + 1,
            "items":      result,
        })


# ── Головний слухач ───────────────────────────────────────────────────────────

async def run_listener():
    diagnostics = {
        "bound_channels": [],
        "unbound_channels": [],
        "last_message_by_source": {},
    }

    def publish_status(status: str, error: str = ""):
        write_status(status, error, {"diagnostics": diagnostics})

    publish_status("starting")

    # Перевіряємо наявність сесії
    if not os.path.exists(SESSION_FILE + ".session"):
        msg = "Сесія відсутня. Авторизуйтесь через Налаштування → Авторизація Telegram"
        print(f"[LISTENER] {msg}")
        publish_status("error", msg)
        return

    settings    = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources     = load_json(SOURCES_FILE,  DEFAULT_SOURCES)

    api_id      = int(settings.get("telegram_api_id",   0) or 0)
    api_hash    = settings.get("telegram_api_hash", "")
    ai_enabled  = bool(settings.get("ai_enabled", False))
    ai_model    = settings.get("ai_model", DEFAULT_AI_MODEL)
    api_key     = settings.get("anthropic_api_key", "")
    categories  = settings.get("categories", [])
    keywords    = settings.get("keywords", [])
    bot_token   = settings.get("bot_token", "")
    bot_chat_id = settings.get("bot_chat_id", "")
    priorities  = settings.get("importance_priorities", "")
    keep_days   = max(1, int(settings.get("keep_days", 14)))
    max_items   = max(10, int(settings.get("max_items", 500)))
    api_hash    = env_secret("NEWSMONITOR_TELEGRAM_API_HASH", api_hash)
    api_key     = env_secret("NEWSMONITOR_ANTHROPIC_API_KEY", api_key)
    bot_token   = env_secret("NEWSMONITOR_BOT_TOKEN", bot_token)

    if not api_id or not api_hash:
        msg = "Не вказано Telegram API ID / Hash. Налаштуйте в інтерфейсі."
        print(f"[LISTENER] {msg}")
        publish_status("error", msg)
        return

    tg_channels = [s for s in sources.get("telegram", []) if s.get("enabled", True)]
    if not tg_channels:
        msg = "Немає активних Telegram каналів."
        print(f"[LISTENER] {msg}")
        publish_status("stopped", msg)
        return

    channel_map = {}
    for ch in tg_channels:
        username = _normalize_channel_username(ch.get("url", ""))
        if username:
            channel_map[username] = ch

    seen_ids = load_seen_ids()
    channel_peer_map: dict[str, dict] = {}

    print(f"[LISTENER] Канали: {', '.join(channel_map.keys())}")
    print(f"[LISTENER] AI: {'увімк' if ai_enabled else 'вимк'} | "
          f"Ключових слів: {len(keywords)}")

    client = TelegramClient(SESSION_FILE, api_id, api_hash)

    async def refresh_channel_bindings() -> list[str]:
        """Резолвить канали в peer-id та намагається доєднатись до публічних."""
        channel_peer_map.clear()
        unavailable = []
        bound = []
        unbound = []
        for username, ch in channel_map.items():
            try:
                try:
                    await client(JoinChannelRequest(username))
                except UserAlreadyParticipantError:
                    pass
                except Exception:
                    pass
                entity = await client.get_entity(username)
                peer_id = str(get_peer_id(entity))
                channel_peer_map[peer_id] = ch
                bound.append({
                    "source_id": ch.get("id", username),
                    "name": ch.get("name", username),
                    "username": username,
                    "peer_id": peer_id,
                })
            except Exception as e:
                err = str(e)
                unavailable.append(f"@{username}: {err}")
                unbound.append({
                    "source_id": ch.get("id", username),
                    "name": ch.get("name", username),
                    "username": username,
                    "error": err,
                })
        diagnostics["bound_channels"] = bound
        diagnostics["unbound_channels"] = unbound
        return unavailable

    @client.on(events.NewMessage)
    async def on_new_message(event):
        msg = event.message
        if not msg.text or len(msg.text.strip()) < 10:
            return

        try:
            chat     = await event.get_chat()
            username = (getattr(chat, "username", "") or "").lower()
        except Exception:
            username = ""

        chat_peer = str(getattr(event, "chat_id", "") or "")
        ch_info  = channel_map.get(username) or channel_peer_map.get(chat_peer, {})
        if not ch_info:
            return
        src_name = ch_info.get("name", username)
        src_id   = ch_info.get("id", username)
        source_ai_enabled = bool(ch_info.get("ai_enabled", True))

        channel_key = username or src_id
        item_id = hashlib.md5(f"{channel_key}_{msg.id}".encode()).hexdigest()[:12]
        if item_id in seen_ids:
            return

        item = {
            "id":               item_id,
            "source":           src_name,
            "source_id":        src_id,
            "type":             "telegram",
            "title":            msg.text[:120].replace("\n", " ").strip(),
            "text":             msg.text[:600].strip(),
            "url":              f"https://t.me/{channel_key}/{msg.id}",
            "time":             datetime.now(timezone.utc).isoformat(),
            "summary":          "",
            "category":         categories[0]["id"] if categories else "",
            "importance":       5,
            "is_duplicate":     False,
            "matched_keywords": [],
        }
        diagnostics["last_message_by_source"][src_id] = {
            "time": item["time"],
            "title": item["title"],
            "message_id": msg.id,
            "username": username,
        }

        print(f"  [+] {src_name}: {item['title'][:70]}...")

        # Ключові слова
        if keywords:
            full_text = item["title"] + " " + item["text"]
            matched   = match_keywords(full_text, keywords)
            item["matched_keywords"] = matched
            if matched:
                urgent_map = {kw["phrase"].lower(): kw.get("urgent", False)
                              for kw in keywords}
                is_urgent  = any(urgent_map.get(kw.lower(), False) for kw in matched)
                prefix     = "⚠️ ТЕРМІНОВА НОВИНА" if is_urgent else "🔔 Ключове слово"
                kw_str     = ", ".join(matched)
                lines = [
                    f"{prefix}: <i>{kw_str}</i>", "",
                    f"<b>{item['title']}</b>",
                    item["text"][:300],
                    f"\nДжерело: {src_name}",
                ]
                if item["url"]:
                    lines.append(f"<a href=\"{item['url']}\">Читати →</a>")
                if bot_token and bot_chat_id:
                    send_bot_message(bot_token, bot_chat_id, "\n".join(lines))
                    print(f"    [BOT] {kw_str}")

        # AI аналіз
        if source_ai_enabled and ai_enabled and api_key and categories:
            try:
                result = analyze_single(item, api_key, categories, ai_model, priorities)
                if result:
                    item.update({
                        "category":     result.get("category", item["category"]),
                        "importance":   int(result.get("importance", 5)),
                        "is_duplicate": bool(result.get("is_duplicate", False)),
                    })
                print(f"    [AI] {item['category']} | {item['importance']}/10")
            except Exception as e:
                print(f"    [AI] Помилка: {e}")

        # Зберігаємо
        fresh = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        append_item(item,
                    max(1, int(fresh.get("keep_days", keep_days))),
                    max(10, int(fresh.get("max_items", max_items))))
        seen_ids.add(item_id)
        save_seen_ids(seen_ids)

    # Підключення з автоперепідключенням
    retry_delay = 5
    while True:
        try:
            await client.connect()

            # Перевіряємо авторизацію без інтерактивного вводу
            if not await client.is_user_authorized():
                msg = "Сесія недійсна. Авторизуйтесь через інтерфейс."
                print(f"[LISTENER] {msg}")
                publish_status("error", msg)
                break

            unavailable = await refresh_channel_bindings()
            if unavailable:
                short = "; ".join(unavailable[:3])
                if len(unavailable) > 3:
                    short += f"; ... (+{len(unavailable)-3})"
                print(f"[LISTENER] Недоступні канали: {short}")

            publish_status("running")
            print(f"[LISTENER] Запущено. Слухаємо {len(channel_map)} каналів\n")
            retry_delay = 5

            # Фоновий таск — оновлює updated_at кожні 10 сек
            # щоб server.py знав що слухач живий
            async def heartbeat():
                while True:
                    await asyncio.sleep(10)
                    publish_status("running")

            hb_task = asyncio.ensure_future(heartbeat())
            try:
                await client.run_until_disconnected()
            finally:
                hb_task.cancel()

        except FloodWaitError as e:
            print(f"[LISTENER] FloodWait {e.seconds}с...")
            publish_status("reconnecting", f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except (ConnectionError, OSError) as e:
            print(f"[LISTENER] З'єднання: {e} | retry {retry_delay}с")
            publish_status("reconnecting", str(e))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

        except asyncio.CancelledError:
            print("\n[LISTENER] Зупинено")
            publish_status("stopped")
            break

        except Exception as e:
            print(f"[LISTENER] Помилка: {e} | retry {retry_delay}с")
            publish_status("error", str(e))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)
        else:
            publish_status("reconnecting", "відключено")
            await asyncio.sleep(5)

    try:
        await client.disconnect()
    except Exception:
        pass
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Захист від подвійного запуску
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            print(f"[LISTENER] Вже запущено (PID {pid}). Виходимо.")
            exit(0)
        except (ProcessLookupError, ValueError, OSError):
            pass

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        asyncio.run(run_listener())
    except KeyboardInterrupt:
        write_status("stopped", extra={"diagnostics": {}})
    finally:
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass

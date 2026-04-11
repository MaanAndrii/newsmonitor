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
from io_utils import load_json, write_json
from storage import Storage
from utils import RetryConfig, retry_call, env_secret, setup_logging

STORAGE = Storage()
LOGGER = setup_logging("newsmonitor.listener")
DEFAULT_CATEGORY = {"id": "other", "name": "Інше", "color": "#888888"}


# ── Утиліти ──────────────────────────────────────────────────────────────────

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
    write_json(SEEN_FILE, list(ids)[-10000:])

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
    write_json(LISTENER_FILE, payload)

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
            "telegram_bot_send",
        )
    except Exception as e:
        LOGGER.exception("[BOT] Помилка відправки")
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
        "anthropic_single_analyze",
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("AI response must be an object")
    importance = result.get("importance", 5)
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        importance = 5
    result["importance"] = min(10, max(1, importance))
    result["is_duplicate"] = bool(result.get("is_duplicate", False))

    if result.get("category") not in cat_ids:
        result["category"] = cat_ids[0]
    return result


def normalize_categories(categories: list) -> list:
    valid = []
    for c in categories or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id", "")).strip()
        name = str(c.get("name", "")).strip()
        color = str(c.get("color", "#888888")).strip() or "#888888"
        if cid and name:
            valid.append({"id": cid, "name": name, "color": color})
    if not valid:
        return [dict(DEFAULT_CATEGORY)]
    return valid


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
        if os.getenv("NEWSMONITOR_WRITE_LEGACY_JSON", "").strip().lower() in {"1", "true", "yes", "on"}:
            write_json(DATA_FILE, {
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
        LOGGER.error("[LISTENER] %s", msg)
        publish_status("error", msg)
        return

    settings    = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources     = load_json(SOURCES_FILE,  DEFAULT_SOURCES)

    api_id      = int(settings.get("telegram_api_id",   0) or 0)
    api_hash    = ""
    ai_enabled  = bool(settings.get("ai_enabled", False))
    ai_model    = settings.get("ai_model", DEFAULT_AI_MODEL)
    api_key     = settings.get("anthropic_api_key", "")
    categories  = normalize_categories(settings.get("categories", []))
    keywords    = settings.get("keywords", [])
    bot_token   = settings.get("bot_token", "")
    priorities  = settings.get("importance_priorities", "")
    keep_days   = max(1, int(settings.get("keep_days", 14)))
    max_items   = max(10, int(settings.get("max_items", 500)))
    api_hash    = (
        env_secret("NEWSMONITOR_TELEGRAM_API_HASH")
        or env_secret("TELEGRAM_API_HASH")
        or api_hash
    )
    api_key     = (
        env_secret("NEWSMONITOR_ANTHROPIC_API_KEY")
        or env_secret("ANTHROPIC_API_KEY")
        or api_key
    )
    bot_token   = (
        env_secret("NEWSMONITOR_BOT_TOKEN")
        or env_secret("BOT_TOKEN")
        or bot_token
    )
    if not api_id:
        env_api_id = os.getenv("NEWSMONITOR_TELEGRAM_API_ID", "").strip() or os.getenv("TELEGRAM_API_ID", "").strip()
        if env_api_id:
            try:
                api_id = int(env_api_id)
            except ValueError:
                pass

    while not api_id or not api_hash:
        msg = "Не вказано Telegram API ID / Hash. Налаштуйте в інтерфейсі."
        LOGGER.error("[LISTENER] %s", msg)
        publish_status("error", msg)
        await asyncio.sleep(15)
        settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        api_id = int(settings.get("telegram_api_id", 0) or 0)
        if not api_id:
            env_api_id = os.getenv("NEWSMONITOR_TELEGRAM_API_ID", "").strip() or os.getenv("TELEGRAM_API_ID", "").strip()
            if env_api_id:
                try:
                    api_id = int(env_api_id)
                except ValueError:
                    pass
        api_hash = (
            env_secret("NEWSMONITOR_TELEGRAM_API_HASH")
            or env_secret("TELEGRAM_API_HASH")
            or settings.get("telegram_api_hash", "")
        )

    tg_channels = [s for s in sources.get("telegram", []) if s.get("enabled", True)]
    while not tg_channels:
        msg = "Немає активних Telegram каналів."
        LOGGER.warning("[LISTENER] %s", msg)
        publish_status("stopped", msg)
        await asyncio.sleep(20)
        sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
        tg_channels = [s for s in sources.get("telegram", []) if s.get("enabled", True)]

    channel_map = {}
    for ch in tg_channels:
        username = _normalize_channel_username(ch.get("url", ""))
        if username:
            channel_map[username] = ch

    seen_ids = load_seen_ids()
    channel_peer_map: dict[str, dict] = {}

    LOGGER.info("[LISTENER] Канали: %s", ", ".join(channel_map.keys()))
    LOGGER.info("[LISTENER] AI: %s | Ключових слів: %s",
                "увімк" if ai_enabled else "вимк", len(keywords))

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

        LOGGER.info("[LISTENER] [+] %s: %s...", src_name, item["title"][:70])

        # Rule-based сповіщення
        rules = STORAGE.list_notification_rules()
        event_rules = [r for r in rules if r.get("enabled") and r.get("type") in {"keyword_hit", "importance_hit", "source_hit"}]

        # Ключові слова
        if keywords:
            full_text = item["title"] + " " + item["text"]
            matched   = match_keywords(full_text, keywords)
            item["matched_keywords"] = matched
            if matched and event_rules:
                urgent_map = {kw["phrase"].lower(): kw.get("urgent", False)
                              for kw in keywords}
                sendable_map = {kw["phrase"].lower(): kw.get("to_telegram", True)
                                for kw in keywords}
                matched_sendable = [kw for kw in matched if sendable_map.get(kw.lower(), False)]
                is_urgent  = any(urgent_map.get(kw.lower(), False) for kw in matched)
                for rule in event_rules:
                    rtype = rule.get("type")
                    target_chat_id = str(rule.get("target_chat_id", "")).strip()
                    if not target_chat_id:
                        continue
                    params = rule.get("params", {}) if isinstance(rule.get("params"), dict) else {}
                    send_it = False
                    title = ""
                    if rtype == "keyword_hit":
                        allowed = {str(x).lower() for x in (params.get("keywords") or [])}
                        hit = [kw for kw in matched_sendable if kw.lower() in allowed]
                        if hit:
                            send_it = True
                            prefix = "⚠️ ТЕРМІНОВА НОВИНА" if is_urgent else "🔔 Ключове слово"
                            title = f"{prefix}: {', '.join(hit)}"
                    elif rtype == "importance_hit":
                        min_imp = int(params.get("min_importance", 8) or 8)
                        if int(item.get("importance", 5) or 5) >= min_imp:
                            send_it = True
                            title = f"🔥 Важлива новина ({item.get('importance', 5)}/10)"
                    elif rtype == "source_hit":
                        src_ids = {str(x) for x in (params.get("source_ids") or [])}
                        if src_id in src_ids:
                            send_it = True
                            title = f"📡 Джерело: {src_name}"
                    if not send_it:
                        continue
                    lines = [f"<b>{title}</b>", "", f"<b>{item['title']}</b>", item["text"][:300], f"\nДжерело: {src_name}"]
                    if item["url"]:
                        lines.append(f"<a href=\"{item['url']}\">Читати →</a>")
                    if bot_token:
                        send_bot_message(bot_token, target_chat_id, "\n".join(lines))

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
                LOGGER.info("[LISTENER][AI] %s | %s/10", item["category"], item["importance"])
            except Exception as e:
                LOGGER.warning("[LISTENER][AI] Помилка: %s", e)

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
                LOGGER.error("[LISTENER] %s", msg)
                publish_status("error", msg)
                break

            unavailable = await refresh_channel_bindings()
            if unavailable:
                short = "; ".join(unavailable[:3])
                if len(unavailable) > 3:
                    short += f"; ... (+{len(unavailable)-3})"
                LOGGER.warning("[LISTENER] Недоступні канали: %s", short)

            publish_status("running")
            LOGGER.info("[LISTENER] Запущено. Слухаємо %s каналів", len(channel_map))
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
            LOGGER.warning("[LISTENER] FloodWait %sс...", e.seconds)
            publish_status("reconnecting", f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except (ConnectionError, OSError) as e:
            LOGGER.warning("[LISTENER] З'єднання: %s | retry %sс", e, retry_delay)
            publish_status("reconnecting", str(e))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

        except asyncio.CancelledError:
            LOGGER.info("[LISTENER] Зупинено")
            publish_status("stopped")
            break

        except Exception as e:
            LOGGER.warning("[LISTENER] Помилка: %s | retry %sс", e, retry_delay)
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
            LOGGER.info("[LISTENER] Вже запущено (PID %s). Виходимо.", pid)
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

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
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from config import (
    SOURCES_FILE, SETTINGS_FILE, DATA_FILE, SESSION_FILE,
    SEEN_FILE, LISTENER_FILE, LOCK_FILE,
    DEFAULT_SOURCES, DEFAULT_SETTINGS, DEFAULT_AI_MODEL,
    IMPORTANCE_CRITERIA
)


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
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen_ids(ids: set) -> None:
    lst = list(ids)
    if len(lst) > 10000:
        lst = lst[-10000:]
    _write_json(SEEN_FILE, lst)

def write_status(status: str, error: str = "") -> None:
    """Записує статус слухача — server.py читає кожні 5 сек."""
    _write_json(LISTENER_FILE, {
        "status":     status,
        "updated_at": time.time(),
        "error":      error,
        "pid":        os.getpid(),
    })


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
    except Exception as e:
        print(f"  [BOT] {e}")
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
        '{"summary":"підсумок 1-2 реченнями українською",'
        f'"category":одне з [{cat_list}],'
        '"importance":1-10,"is_duplicate":false}\n\n'
        "ТІЛЬКИ JSON об'єкт, без пояснень та markdown."
    )

    response = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
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
    """Атомарно додає одну новину до news_data.json."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

    with _data_lock:
        existing = []
        meta     = {}
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                existing = data.get("items", [])
                meta     = {k: v for k, v in data.items() if k != "items"}
            except Exception:
                pass

        # перевіряємо дубль
        if any(it["id"] == item["id"] for it in existing):
            return

        existing.insert(0, item)

        # очищення
        result = []
        for it in existing:
            try:
                t = it.get("time", "")
                if t.endswith("Z"):
                    t = t[:-1] + "+00:00"
                elif "+" not in t and len(t) > 10 and t.count("-") <= 2:
                    t += "+00:00"
                if datetime.fromisoformat(t) >= cutoff:
                    result.append(it)
            except Exception:
                result.append(it)

        result = result[:max_items]

        meta.update({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total":      len(result),
            "new_count":  meta.get("new_count", 0) + 1,
            "items":      result,
        })
        _write_json(DATA_FILE, meta)


# ── Головний слухач ───────────────────────────────────────────────────────────

async def run_listener():
    write_status("starting")

    # Перевіряємо наявність сесії
    if not os.path.exists(SESSION_FILE + ".session"):
        msg = "Сесія відсутня. Авторизуйтесь через Налаштування → Авторизація Telegram"
        print(f"[LISTENER] {msg}")
        write_status("error", msg)
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

    if not api_id or not api_hash:
        msg = "Не вказано Telegram API ID / Hash. Налаштуйте в інтерфейсі."
        print(f"[LISTENER] {msg}")
        write_status("error", msg)
        return

    tg_channels = [s for s in sources.get("telegram", []) if s.get("enabled", True)]
    if not tg_channels:
        msg = "Немає активних Telegram каналів."
        print(f"[LISTENER] {msg}")
        write_status("stopped", msg)
        return

    channel_map = {}
    for ch in tg_channels:
        username = ch["url"].rstrip("/").split("/")[-1].lstrip("@").lower()
        channel_map[username] = ch

    seen_ids = load_seen_ids()

    print(f"[LISTENER] Канали: {', '.join(channel_map.keys())}")
    print(f"[LISTENER] AI: {'увімк' if ai_enabled else 'вимк'} | "
          f"Ключових слів: {len(keywords)}")

    client = TelegramClient(SESSION_FILE, api_id, api_hash)

    @client.on(events.NewMessage(chats=list(channel_map.keys())))
    async def on_new_message(event):
        msg = event.message
        if not msg.text or len(msg.text.strip()) < 10:
            return

        try:
            chat     = await event.get_chat()
            username = (getattr(chat, "username", "") or "").lower()
        except Exception:
            username = ""

        ch_info  = channel_map.get(username, {})
        src_name = ch_info.get("name", username)
        src_id   = ch_info.get("id", username)

        item_id = hashlib.md5(f"{username}_{msg.id}".encode()).hexdigest()[:12]
        if item_id in seen_ids:
            return

        item = {
            "id":               item_id,
            "source":           src_name,
            "source_id":        src_id,
            "type":             "telegram",
            "title":            msg.text[:120].replace("\n", " ").strip(),
            "text":             msg.text[:600].strip(),
            "url":              f"https://t.me/{username}/{msg.id}",
            "time":             datetime.now(timezone.utc).isoformat(),
            "summary":          "",
            "category":         categories[0]["id"] if categories else "",
            "importance":       5,
            "is_duplicate":     False,
            "matched_keywords": [],
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
        if ai_enabled and api_key and categories:
            try:
                result = analyze_single(item, api_key, categories, ai_model, priorities)
                if result:
                    item.update({
                        "summary":      result.get("summary", ""),
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
                write_status("error", msg)
                break

            write_status("running")
            print(f"[LISTENER] Запущено. Слухаємо {len(channel_map)} каналів\n")
            retry_delay = 5

            # Фоновий таск — оновлює updated_at кожні 10 сек
            # щоб server.py знав що слухач живий
            async def heartbeat():
                while True:
                    await asyncio.sleep(10)
                    write_status("running")

            hb_task = asyncio.ensure_future(heartbeat())
            try:
                await client.run_until_disconnected()
            finally:
                hb_task.cancel()

        except FloodWaitError as e:
            print(f"[LISTENER] FloodWait {e.seconds}с...")
            write_status("reconnecting", f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except (ConnectionError, OSError) as e:
            print(f"[LISTENER] З'єднання: {e} | retry {retry_delay}с")
            write_status("reconnecting", str(e))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

        except asyncio.CancelledError:
            print("\n[LISTENER] Зупинено")
            write_status("stopped")
            break

        except Exception as e:
            print(f"[LISTENER] Помилка: {e} | retry {retry_delay}с")
            write_status("error", str(e))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)
        else:
            write_status("reconnecting", "відключено")
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
        write_status("stopped")
    finally:
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass

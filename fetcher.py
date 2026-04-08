"""
fetcher.py — збирає новини з RSS та Telegram, аналізує через Claude API
Запуск: python3 fetcher.py
"""

import asyncio
import json
import os
import hashlib
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from telethon import TelegramClient
import feedparser

from config import (
    SOURCES_FILE, SETTINGS_FILE, DATA_FILE, SESSION_FILE,
    SEEN_FILE, DEFAULT_SOURCES, DEFAULT_SETTINGS, DEFAULT_AI_MODEL,
    IMPORTANCE_CRITERIA
)


# ── Утиліти ──────────────────────────────────────────────────────────────────

def load_json(path: str, default) -> dict:
    """Завантажує JSON файл. При помилці повертає default."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # міграція: додаємо відсутні ключі зі збереженням наявних
            if isinstance(default, dict):
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [WARN] Не вдалося прочитати {path}: {e}")
    write_json(path, default)
    return dict(default) if isinstance(default, dict) else default

def write_json(path: str, data) -> None:
    """Атомарний запис JSON через тимчасовий файл."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── Seen IDs ─────────────────────────────────────────────────────────────────

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
    write_json(SEEN_FILE, lst)


# ── Очищення старих новин ─────────────────────────────────────────────────────

def parse_time(t: str):
    """Парсить ISO datetime рядок з або без timezone."""
    if not t:
        return None
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        elif "+" not in t and len(t) > 10 and t.count("-") <= 2:
            t += "+00:00"
        return datetime.fromisoformat(t)
    except Exception:
        return None

def cleanup_old_items(items: list, keep_days: int, max_items: int) -> list:
    """Видаляє новини старіші за keep_days і обрізає до max_items."""
    if not items:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    before = len(items)
    result = []
    for item in items:
        t = parse_time(item.get("time", ""))
        if t is None or t >= cutoff:
            result.append(item)
    result = result[:max_items]
    removed = before - len(result)
    if removed > 0:
        print(f"  [CLEAN] Видалено {removed} старих | залишилось: {len(result)}")
    return result


# ── Ключові слова ─────────────────────────────────────────────────────────────

def match_keywords(text: str, keywords: list) -> list:
    """Повертає список фраз що знайдені в тексті."""
    if not keywords:
        return []
    text_lower = text.lower()
    return [
        kw["phrase"] for kw in keywords
        if kw.get("phrase", "").strip().lower() in text_lower
    ]


# ── Telegram Bot ──────────────────────────────────────────────────────────────

def send_bot_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Надсилає повідомлення через Telegram Bot API."""
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
        print(f"  [BOT] Помилка відправки: {e}")
        return False

def notify_keywords(new_items: list, keywords: list,
                    bot_token: str, chat_id: str) -> int:
    """Надсилає сповіщення про новини з ключовими словами."""
    if not bot_token or not chat_id or not keywords:
        return 0
    urgent_map = {kw["phrase"].lower(): kw.get("urgent", False) for kw in keywords}
    sent = 0
    for item in new_items:
        matched = item.get("matched_keywords", [])
        if not matched:
            continue
        is_urgent = any(urgent_map.get(kw.lower(), False) for kw in matched)
        prefix    = "⚠️ ТЕРМІНОВА НОВИНА" if is_urgent else "🔔 Ключове слово"
        kw_str    = ", ".join(matched)
        lines = [f"{prefix}: <i>{kw_str}</i>", "", f"<b>{item['title']}</b>"]
        if item.get("summary"):
            lines.append(item["summary"])
        lines.append(f"\nДжерело: {item['source']} | {item.get('importance', 5)}/10")
        if item.get("url"):
            lines.append(f"<a href=\"{item['url']}\">Читати →</a>")
        if send_bot_message(bot_token, chat_id, "\n".join(lines)):
            sent += 1
    return sent


# ── AI аналіз ────────────────────────────────────────────────────────────────

def analyze_batch(items: list, api_key: str, categories: list,
                  model: str, priorities: str) -> list:
    """Пакетний аналіз новин через Claude."""
    if not categories:
        raise ValueError("Немає категорій для аналізу. Додайте категорії в налаштуваннях.")

    client   = Anthropic(api_key=api_key)
    cat_list = ", ".join(f'"{c["id"]}" ({c["name"]})' for c in categories)
    cat_ids  = [c["id"] for c in categories]

    priorities_block = (
        f"\nПерсональні пріоритети редакції:\n{priorities.strip()}\n"
        if priorities.strip() else ""
    )

    items_text = "\n\n".join(
        f"[{i+1}] Джерело: {it['source']}\n"
        f"Заголовок: {it['title']}\n"
        f"Текст: {it.get('text', '')[:600]}"
        for i, it in enumerate(items)
    )

    prompt = (
        "Проаналізуй українські новини та поверни JSON масив.\n\n"
        f"Новини:\n{items_text}\n\n"
        f"{IMPORTANCE_CRITERIA}\n"
        f"{priorities_block}\n"
        "Для кожної новини поверни об'єкт:\n"
        '{"index":N,"summary":"підсумок 1-2 реченнями українською",'
        f'"category":одне з [{cat_list}],'
        '"importance":1-10,"is_duplicate":true/false}\n\n'
        "ТІЛЬКИ JSON масив, без пояснень та markdown."
    )

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    results = json.loads(raw)

    # валідуємо category
    for r in results:
        if r.get("category") not in cat_ids:
            r["category"] = cat_ids[0]
    return results


# ── RSS ──────────────────────────────────────────────────────────────────────

def fetch_rss(sources: list, depth: int) -> list:
    """Збирає новини з RSS джерел."""
    results = []
    for src in sources:
        if not src.get("enabled", True):
            print(f"  [RSS] Пропущено: {src['name']} (вимкнено)")
            continue
        print(f"  [RSS] {src['name']} ...", end=" ", flush=True)
        try:
            feed  = feedparser.parse(src["url"])
            count = 0
            for entry in feed.entries[:depth]:
                raw_text = entry.get("summary") or entry.get("description") or ""
                text     = re.sub(r"<[^>]+>", " ", raw_text).strip()
                link     = entry.get("link", "")
                item_id  = hashlib.md5(
                    (link or entry.get("title", "")).encode()
                ).hexdigest()[:12]
                results.append({
                    "id":        item_id,
                    "source":    src["name"],
                    "source_id": src["id"],
                    "type":      "rss",
                    "title":     entry.get("title", "").strip(),
                    "text":      text,
                    "url":       link,
                    "time":      entry.get("published",
                                 datetime.now(timezone.utc).isoformat()),
                })
                count += 1
            print(f"{count} новин")
        except Exception as e:
            print(f"помилка: {e}")
    return results


# ── Telegram (пакетний режим) ─────────────────────────────────────────────────

async def fetch_telegram(sources: list, depth: int,
                         api_id: int, api_hash: str) -> list:
    """Збирає повідомлення з Telegram каналів пакетно."""
    results = []
    enabled = [s for s in sources if s.get("enabled", True)]
    if not enabled:
        print("  [TG] Немає активних каналів")
        return results
    if not api_id or not api_hash:
        print("  [TG] Не налаштовано Telegram API — пропускаємо")
        return results
    if not os.path.exists(SESSION_FILE + ".session"):
        print("  [TG] Немає сесії — авторизуйтесь через інтерфейс")
        return results

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    try:
        # connect=False щоб не запитувати авторизацію якщо сесія протухла
        await client.connect()
        if not await client.is_user_authorized():
            print("  [TG] Сесія недійсна — авторизуйтесь через інтерфейс")
            return results

        for ch in enabled:
            username = ch["url"].rstrip("/").split("/")[-1].lstrip("@")
            print(f"  [TG] @{username} ({ch['name']}) ...", end=" ", flush=True)
            count = 0
            try:
                async for msg in client.iter_messages(username, limit=depth):
                    if not msg.text or len(msg.text.strip()) < 10:
                        continue
                    item_id = hashlib.md5(
                        f"{username}_{msg.id}".encode()
                    ).hexdigest()[:12]
                    results.append({
                        "id":        item_id,
                        "source":    ch["name"],
                        "source_id": ch["id"],
                        "type":      "telegram",
                        "title":     msg.text[:120].replace("\n", " ").strip(),
                        "text":      msg.text[:600].strip(),
                        "url":       f"https://t.me/{username}/{msg.id}",
                        "time":      msg.date.isoformat(),
                    })
                    count += 1
                print(f"{count} повідомлень")
            except Exception as e:
                print(f"помилка: {e}")
    except Exception as e:
        print(f"  [TG] Помилка підключення: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return results


# ── Головна логіка ────────────────────────────────────────────────────────────

async def run():
    print("=" * 48)
    print(f"  News Monitor Fetcher — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print("=" * 48)

    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources  = load_json(SOURCES_FILE,  DEFAULT_SOURCES)

    ai_enabled       = bool(settings.get("ai_enabled", False))
    ai_model         = settings.get("ai_model", DEFAULT_AI_MODEL)
    rss_depth        = max(1, int(settings.get("rss_depth", 10)))
    tg_depth         = max(1, int(settings.get("tg_depth",  10)))
    api_key          = settings.get("anthropic_api_key", "")
    tg_api_id        = int(settings.get("telegram_api_id", 0) or 0)
    tg_api_hash      = settings.get("telegram_api_hash", "")
    categories       = settings.get("categories", [])
    keywords         = settings.get("keywords", [])
    keep_days        = max(1, int(settings.get("keep_days", 14)))
    max_items        = max(10, int(settings.get("max_items", 500)))
    bot_token        = settings.get("bot_token", "")
    bot_chat_id      = settings.get("bot_chat_id", "")
    priorities       = settings.get("importance_priorities", "")
    listener_enabled = bool(settings.get("listener_enabled", False))

    print(f"  AI: {'увімк' if ai_enabled else 'вимк'} | модель: {ai_model}")
    print(f"  RSS глибина: {rss_depth} | TG глибина: {tg_depth}")
    print(f"  Категорій: {len(categories)} | Ключових слів: {len(keywords)}")
    print(f"  Слухач: {'увімк — TG не збираємо' if listener_enabled else 'вимк'}\n")

    # ── Завантажуємо попередній стан ─────────────────────────────────────────
    seen_ids     = load_seen_ids()
    prev_analysis = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
            for it in prev_data.get("items", []):
                prev_analysis[it["id"]] = {
                    "summary":      it.get("summary", ""),
                    "category":     it.get("category", ""),
                    "importance":   it.get("importance", 5),
                    "is_duplicate": it.get("is_duplicate", False),
                    "matched_keywords": it.get("matched_keywords", []),
                }
            print(f"[0] Відомих новин: {len(seen_ids)} | Збережений аналіз: {len(prev_analysis)}")
        except Exception as e:
            print(f"[0] Попередній аналіз недоступний: {e}")

    # ── Збираємо новини ──────────────────────────────────────────────────────
    print("\n[1/4] Збір новин")
    rss_items = fetch_rss(sources.get("rss", []), rss_depth)

    tg_items = []
    if listener_enabled:
        print("  [TG] Слухач активний — пропускаємо пакетний збір")
    else:
        tg_items = await fetch_telegram(
            sources.get("telegram", []), tg_depth, tg_api_id, tg_api_hash
        )

    all_items = rss_items + tg_items
    print(f"\n  Зібрано: {len(rss_items)} RSS + {len(tg_items)} TG = {len(all_items)}")

    if not all_items:
        print("\nНемає новин для збереження.")
        return

    # ── Нові vs відомі ───────────────────────────────────────────────────────
    new_items = [it for it in all_items if it["id"] not in seen_ids]
    old_items = [it for it in all_items if it["id"] in     seen_ids]
    print(f"  Нових: {len(new_items)} | Відомих: {len(old_items)}")

    # Відновлюємо збережений аналіз для відомих новин
    for item in old_items:
        saved = prev_analysis.get(item["id"])
        if saved:
            item.update(saved)

    # ── Ключові слова ────────────────────────────────────────────────────────
    if keywords:
        kw_hits = 0
        for item in all_items:
            full_text = item["title"] + " " + item.get("text", "")
            matched   = match_keywords(full_text, keywords)
            item["matched_keywords"] = matched
            if matched:
                kw_hits += 1
        if kw_hits:
            print(f"  Ключових слів: знайдено в {kw_hits} новинах")
    else:
        for item in all_items:
            item.setdefault("matched_keywords", [])

    # ── AI аналіз (тільки нові) ──────────────────────────────────────────────
    print()
    if new_items and ai_enabled and api_key and categories:
        print(f"[2/4] AI аналіз: {len(new_items)} нових новин | {ai_model}")
        BATCH = 15
        for i in range(0, len(new_items), BATCH):
            batch = new_items[i : i + BATCH]
            end   = min(i + BATCH, len(new_items))
            print(f"  [{i+1}–{end}/{len(new_items)}] ...", end=" ", flush=True)
            try:
                analyses = analyze_batch(batch, api_key, categories, ai_model, priorities)
                for ai_res in analyses:
                    idx = i + ai_res["index"] - 1
                    if 0 <= idx < len(new_items):
                        new_items[idx].update({
                            "summary":      ai_res.get("summary", ""),
                            "category":     ai_res.get("category", categories[0]["id"]),
                            "importance":   int(ai_res.get("importance", 5)),
                            "is_duplicate": bool(ai_res.get("is_duplicate", False)),
                        })
                print("OK")
            except Exception as e:
                print(f"помилка: {e}")
        print()
    else:
        if not new_items:
            print("[2/4] AI пропущено — немає нових новин")
        elif not ai_enabled:
            print("[2/4] AI пропущено — вимкнено в налаштуваннях")
        elif not api_key:
            print("[2/4] AI пропущено — не вказано Anthropic API ключ")
        elif not categories:
            print("[2/4] AI пропущено — не налаштовано категорії")
        print()

    # Дефолтні значення для нових без аналізу
    default_cat = categories[0]["id"] if categories else ""
    for item in new_items:
        item.setdefault("summary",      "")
        item.setdefault("category",     default_cat)
        item.setdefault("importance",   5)
        item.setdefault("is_duplicate", False)
        item.setdefault("matched_keywords", [])
    for item in old_items:
        item.setdefault("summary",      "")
        item.setdefault("category",     default_cat)
        item.setdefault("importance",   5)
        item.setdefault("is_duplicate", False)
        item.setdefault("matched_keywords", [])

    # ── Bot — сповіщення ─────────────────────────────────────────────────────
    if bot_token and bot_chat_id and keywords and new_items:
        kw_new = [it for it in new_items if it.get("matched_keywords")]
        if kw_new:
            print("[BOT] Надсилаємо сповіщення...")
            sent = notify_keywords(kw_new, keywords, bot_token, bot_chat_id)
            print(f"  Надіслано: {sent}")
            print()

    # ── Seen IDs ─────────────────────────────────────────────────────────────
    save_seen_ids(seen_ids | {it["id"] for it in all_items})

    # ── Об'єднання з новинами від слухача ────────────────────────────────────
    print("[3/4] Збереження")
    if listener_enabled and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            fetcher_ids   = {it["id"] for it in all_items}
            listener_only = [it for it in existing.get("items", [])
                             if it["id"] not in fetcher_ids]
            if listener_only:
                print(f"  Додаємо {len(listener_only)} новин від слухача")
                all_items = all_items + listener_only
        except Exception as e:
            print(f"  [WARN] Об'єднання з слухачем: {e}")

    all_items.sort(key=lambda x: x.get("time", ""), reverse=True)
    all_items = cleanup_old_items(all_items, keep_days, max_items)

    high    = sum(1 for x in all_items if x.get("importance", 0) >= 8)
    dups    = sum(1 for x in all_items if x.get("is_duplicate"))
    kw_hits = sum(1 for x in all_items if x.get("matched_keywords"))

    write_json(DATA_FILE, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ai_enabled": ai_enabled,
        "total":      len(all_items),
        "new_count":  len(new_items),
        "items":      all_items,
    })

    print(f"\n  Готово: {len(all_items)} новин | нових: {len(new_items)} | "
          f"важливих: {high} | дублів: {dups} | ключових слів: {kw_hits}")


if __name__ == "__main__":
    asyncio.run(run())

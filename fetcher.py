"""
fetcher.py — пакетний збір RSS новин та AI-аналіз
Запуск: python3 fetcher.py
"""

import asyncio
import json
import os
import hashlib
import re
import urllib.request
import urllib.parse
import email.utils
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
import feedparser

from config import (
    SOURCES_FILE, SETTINGS_FILE, DATA_FILE,
    SEEN_FILE, DEFAULT_SOURCES, DEFAULT_SETTINGS, DEFAULT_AI_MODEL,
    IMPORTANCE_CRITERIA
)
from io_utils import load_json, write_json
from storage import Storage
from utils import RetryConfig, retry_call, env_secret, setup_logging

STORAGE = Storage()
LOGGER = setup_logging("newsmonitor.fetcher")
DEFAULT_CATEGORY = {"id": "other", "name": "Інше", "color": "#888888"}


# ── Seen IDs ─────────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    ids = STORAGE.load_seen_ids()
    if ids:
        return ids
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                ids = set(json.load(f))
            STORAGE.save_seen_ids(ids)
            return ids
        except Exception:
            return set()
    return set()

def save_seen_ids(ids: set) -> None:
    STORAGE.save_seen_ids(ids)
    # legacy compatibility
    write_json(SEEN_FILE, list(ids)[-10000:])


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
        LOGGER.info("[CLEAN] Видалено %s старих | залишилось: %s", removed, len(result))
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
            RetryConfig(attempts=4, base_delay=1.0, max_delay=6.0, jitter=0.4),
            "telegram_bot_send",
        )
    except Exception as e:
        LOGGER.exception("[BOT] Помилка відправки")
        return False

def notify_keywords(new_items: list, keywords: list,
                    bot_token: str, chat_id: str) -> int:
    """Надсилає сповіщення про новини з ключовими словами."""
    if not bot_token or not chat_id or not keywords:
        return 0
    urgent_map = {kw["phrase"].lower(): kw.get("urgent", False) for kw in keywords}
    sendable   = {kw["phrase"].lower() for kw in keywords if kw.get("to_telegram", True)}
    if not sendable:
        return 0
    sent = 0
    for item in new_items:
        matched = item.get("matched_keywords", [])
        if not matched:
            continue
        matched_sendable = [kw for kw in matched if kw.lower() in sendable]
        if not matched_sendable:
            continue
        is_urgent = any(urgent_map.get(kw.lower(), False) for kw in matched)
        prefix    = "⚠️ ТЕРМІНОВА НОВИНА" if is_urgent else "🔔 Ключове слово"
        kw_str    = ", ".join(matched_sendable)
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
        '{"index":N,'
        f'"category":одне з [{cat_list}],'
        '"importance":1-10,"is_duplicate":true/false}\n\n'
        "ТІЛЬКИ JSON масив, без пояснень та markdown."
    )

    response = retry_call(
        lambda: client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        ),
        RetryConfig(attempts=3, base_delay=1.5, max_delay=8.0, jitter=0.3),
        "anthropic_batch_analyze",
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    results = json.loads(raw)
    if not isinstance(results, list):
        raise ValueError("AI response must be a JSON list")
    validated = []
    for idx, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        source_index = int(item.get("index", idx))
        importance = item.get("importance", 5)
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 5
        importance = min(10, max(1, importance))
        validated.append({
            "index": source_index,
            "category": item.get("category"),
            "importance": importance,
            "is_duplicate": bool(item.get("is_duplicate", False)),
        })

    # валідуємо category
    for r in validated:
        if r.get("category") not in cat_ids:
            r["category"] = cat_ids[0]
    return validated


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


# ── RSS ──────────────────────────────────────────────────────────────────────

def fetch_rss(sources: list, depth: int) -> list:
    """Збирає новини з RSS джерел."""
    results = []
    for src in sources:
        if not src.get("enabled", True):
            LOGGER.info("[RSS] Пропущено: %s (вимкнено)", src["name"])
            continue
        LOGGER.info("[RSS] %s ...", src["name"])
        try:
            feed  = retry_call(
                lambda: feedparser.parse(src["url"]),
                RetryConfig(attempts=3, base_delay=1.0, max_delay=5.0, jitter=0.2),
                    f"rss_fetch:{src['id']}",
            )
            count = 0
            for entry in feed.entries[:depth]:
                raw_text = entry.get("summary") or entry.get("description") or ""
                text     = re.sub(r"<[^>]+>", " ", raw_text).strip()
                link     = entry.get("link", "")
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    ts = datetime(*published[:6], tzinfo=timezone.utc).isoformat()
                else:
                    raw_dt = entry.get("published") or entry.get("updated") or ""
                    try:
                        dt = email.utils.parsedate_to_datetime(raw_dt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        ts = dt.astimezone(timezone.utc).isoformat()
                    except Exception:
                        ts = datetime.now(timezone.utc).isoformat()
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
                    "time":      ts,
                })
                count += 1
            LOGGER.info("[RSS] %s: %s новин", src["name"], count)
        except Exception as e:
            LOGGER.warning("[RSS] %s: помилка: %s", src.get("name", "unknown"), e)
    return results


# ── Головна логіка ────────────────────────────────────────────────────────────

async def run():
    LOGGER.info("=" * 48)
    LOGGER.info("News Monitor Fetcher — %s", datetime.now().strftime('%d.%m.%Y %H:%M:%S'))
    LOGGER.info("=" * 48)

    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources  = load_json(SOURCES_FILE,  DEFAULT_SOURCES)

    ai_enabled       = bool(settings.get("ai_enabled", False))
    ai_model         = settings.get("ai_model", DEFAULT_AI_MODEL)
    rss_depth        = max(1, int(settings.get("rss_depth", 10)))
    api_key          = settings.get("anthropic_api_key", "")
    categories       = normalize_categories(settings.get("categories", []))
    keywords         = settings.get("keywords", [])
    keep_days        = max(1, int(settings.get("keep_days", 14)))
    max_items        = max(10, int(settings.get("max_items", 500)))
    bot_token        = ""
    bot_chat_id      = settings.get("bot_chat_id", "")
    priorities       = settings.get("importance_priorities", "")
    api_key          = (
        env_secret("NEWSMONITOR_ANTHROPIC_API_KEY")
        or env_secret("ANTHROPIC_API_KEY")
        or api_key
    )
    bot_token        = (
        env_secret("NEWSMONITOR_BOT_TOKEN")
        or env_secret("BOT_TOKEN")
        or bot_token
    )
    source_ai_enabled = {}
    for src in (sources.get("rss", []) + sources.get("telegram", [])):
        source_ai_enabled[src.get("id")] = bool(src.get("ai_enabled", True))

    LOGGER.info("AI: %s | модель: %s", "увімк" if ai_enabled else "вимк", ai_model)
    LOGGER.info("RSS глибина: %s", rss_depth)
    LOGGER.info("Категорій: %s | Ключових слів: %s", len(categories), len(keywords))
    LOGGER.info("Telegram пакетний збір: вимкнено (працює тільки listener)")

    # ── Завантажуємо попередній стан ─────────────────────────────────────────
    seen_ids     = load_seen_ids()
    prev_analysis = {}
    try:
        for it in STORAGE.load_items():
            prev_analysis[it["id"]] = {
                "summary":      it.get("summary", ""),
                "category":     it.get("category", ""),
                "importance":   it.get("importance", 5),
                "is_duplicate": it.get("is_duplicate", False),
                "matched_keywords": it.get("matched_keywords", []),
            }
        LOGGER.info("[0] Відомих новин: %s | Збережений аналіз: %s", len(seen_ids), len(prev_analysis))
    except Exception as e:
        LOGGER.warning("[0] Попередній аналіз недоступний: %s", e)

    # ── Збираємо новини ──────────────────────────────────────────────────────
    LOGGER.info("[1/4] Збір новин")
    rss_items = fetch_rss(sources.get("rss", []), rss_depth)
    all_items = list(rss_items)
    LOGGER.info("Зібрано RSS: %s", len(rss_items))

    if not all_items:
        LOGGER.info("Немає новин для збереження.")
        return

    # ── Нові vs відомі ───────────────────────────────────────────────────────
    new_items = [it for it in all_items if it["id"] not in seen_ids]
    old_items = [it for it in all_items if it["id"] in     seen_ids]
    LOGGER.info("Нових: %s | Відомих: %s", len(new_items), len(old_items))

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
            LOGGER.info("Ключових слів знайдено в %s новинах", kw_hits)
    else:
        for item in all_items:
            item.setdefault("matched_keywords", [])

    # ── AI аналіз (тільки нові) ──────────────────────────────────────────────
    ai_candidates = [it for it in new_items if source_ai_enabled.get(it.get("source_id"), True)]
    if ai_candidates and ai_enabled and api_key and categories:
        LOGGER.info("[2/4] AI аналіз: %s нових новин | %s", len(ai_candidates), ai_model)
        BATCH = 15
        for i in range(0, len(ai_candidates), BATCH):
            batch = ai_candidates[i : i + BATCH]
            end   = min(i + BATCH, len(ai_candidates))
            LOGGER.info("  [%s–%s/%s] ...", i + 1, end, len(ai_candidates))
            try:
                analyses = analyze_batch(batch, api_key, categories, ai_model, priorities)
                for ai_res in analyses:
                    idx = ai_res["index"] - 1
                    if 0 <= idx < len(batch):
                        batch[idx].update({
                            "category":     ai_res.get("category", categories[0]["id"]),
                            "importance":   int(ai_res.get("importance", 5)),
                            "is_duplicate": bool(ai_res.get("is_duplicate", False)),
                        })
                LOGGER.info("  batch OK")
            except Exception as e:
                LOGGER.warning("  помилка batch AI: %s", e)
    else:
        if not new_items:
            LOGGER.info("[2/4] AI пропущено — немає нових новин")
        elif not ai_enabled:
            LOGGER.info("[2/4] AI пропущено — вимкнено в налаштуваннях")
        elif not api_key:
            LOGGER.info("[2/4] AI пропущено — не вказано Anthropic API ключ")
        elif not categories:
            LOGGER.info("[2/4] AI пропущено — не налаштовано категорії")

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
            LOGGER.info("[BOT] Надсилаємо сповіщення...")
            sent = notify_keywords(kw_new, keywords, bot_token, bot_chat_id)
            LOGGER.info("[BOT] Надіслано: %s", sent)

    # ── Seen IDs ─────────────────────────────────────────────────────────────
    save_seen_ids(seen_ids | {it["id"] for it in all_items})

    # ── Об'єднання з новинами від слухача ────────────────────────────────────
    LOGGER.info("[3/4] Збереження")
    try:
        existing = {"items": STORAGE.load_items()}
        fetcher_ids   = {it["id"] for it in all_items}
        listener_only = [it for it in existing.get("items", [])
                         if it["id"] not in fetcher_ids]
        if listener_only:
            LOGGER.info("Додаємо %s новин від слухача", len(listener_only))
            all_items = all_items + listener_only
    except Exception as e:
        LOGGER.warning("Об'єднання з слухачем: %s", e)

    all_items.sort(key=lambda x: x.get("time", ""), reverse=True)
    all_items = cleanup_old_items(all_items, keep_days, max_items)

    high    = sum(1 for x in all_items if x.get("importance", 0) >= 8)
    dups    = sum(1 for x in all_items if x.get("is_duplicate"))
    kw_hits = sum(1 for x in all_items if x.get("matched_keywords"))

    STORAGE.upsert_items(all_items)
    all_items = STORAGE.cleanup(keep_days, max_items)
    STORAGE.set_kv("new_count", len(new_items))
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ai_enabled": ai_enabled,
        "total":      len(all_items),
        "new_count":  len(new_items),
        "items":      all_items,
    }
    if os.getenv("NEWSMONITOR_WRITE_LEGACY_JSON", "").strip().lower() in {"1", "true", "yes", "on"}:
        write_json(DATA_FILE, payload)

    LOGGER.info("Готово: %s новин | нових: %s | важливих: %s | дублів: %s | ключових слів: %s",
                len(all_items), len(new_items), high, dups, kw_hits)


if __name__ == "__main__":
    asyncio.run(run())

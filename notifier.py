import asyncio

import fetcher
from config import DEFAULT_SETTINGS, SETTINGS_FILE
from io_utils import load_json
from storage import Storage
from utils import env_secret, setup_logging

LOGGER = setup_logging("newsmonitor.notifier")
STORAGE = Storage()


def _rule_matches(item: dict, rule: dict) -> tuple[bool, str]:
    rtype = str(rule.get("type", ""))
    params = rule.get("params", {}) if isinstance(rule.get("params"), dict) else {}
    item_keywords = {str(x).strip().lower() for x in (item.get("matched_keywords") or [])}
    item_importance = int(item.get("importance", 5) or 5)
    item_source = str(item.get("source_id", "")).strip().lower()
    if rtype == "keyword_hit":
        kws = {str(x).strip().lower() for x in (params.get("keywords") or [])}
        hit = sorted(item_keywords & kws)
        return (bool(hit), f"🔔 Ключові слова: {', '.join(hit)}" if hit else "")
    if rtype == "importance_hit":
        try:
            min_imp = int(params.get("min_importance", 8) or 8)
        except Exception:
            min_imp = 8
        ok = item_importance >= min_imp
        return (ok, f"🔥 Важлива новина ({item_importance}/10)" if ok else "")
    if rtype == "source_hit":
        src_ids = {str(x).strip().lower() for x in (params.get("source_ids") or [])}
        ok = bool(item_source and item_source in src_ids)
        return (ok, f"📡 Джерело: {item.get('source', '')}" if ok else "")
    return (False, "")


async def run():
    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    bot_token = (
        env_secret("NEWSMONITOR_BOT_TOKEN")
        or env_secret("BOT_TOKEN")
        or settings.get("bot_token", "")
    )
    if not bot_token:
        LOGGER.info("[NOTIFIER] bot token missing")
        return

    rules = [r for r in STORAGE.list_notification_rules() if r.get("enabled") and r.get("type") in {"keyword_hit", "importance_hit", "source_hit"}]
    if not rules:
        return

    items = STORAGE.load_items()
    changed = False
    sent = 0
    for item in items:
        if not item.get("analyzed_at"):
            continue
        notified = set(item.get("notified_rule_ids") or [])
        for rule in rules:
            rid = str(rule.get("id", ""))
            if not rid or rid in notified:
                continue
            target_chat_id = str(rule.get("target_chat_id", "")).strip()
            if not target_chat_id:
                continue
            ok, title = _rule_matches(item, rule)
            if not ok:
                continue
            lines = [f"<b>{title}</b>", "", f"<b>{item.get('title','')}</b>"]
            if item.get("summary"):
                lines.append(item["summary"])
            lines.append(f"\nДжерело: {item.get('source', '')} | {int(item.get('importance', 5) or 5)}/10")
            if item.get("url"):
                lines.append(f"<a href=\"{item['url']}\">Читати →</a>")
            if fetcher.send_bot_message(bot_token, target_chat_id, "\n".join(lines)):
                notified.add(rid)
                sent += 1
                changed = True
        if changed:
            item["notified_rule_ids"] = list(notified)

    if changed:
        STORAGE.upsert_items(items)
    LOGGER.info("[NOTIFIER] sent: %s", sent)


if __name__ == "__main__":
    asyncio.run(run())

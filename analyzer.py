import asyncio
from datetime import datetime, timezone

import fetcher
from config import DEFAULT_SETTINGS, DEFAULT_SOURCES, SETTINGS_FILE, SOURCES_FILE
from io_utils import load_json
from storage import Storage
from utils import env_secret, setup_logging

LOGGER = setup_logging("newsmonitor.analyzer")
STORAGE = Storage()


async def run():
    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
    ai_enabled = bool(settings.get("ai_enabled", False))
    ai_model = settings.get("ai_model", fetcher.DEFAULT_AI_MODEL)
    api_key = (
        env_secret("NEWSMONITOR_ANTHROPIC_API_KEY")
        or env_secret("ANTHROPIC_API_KEY")
        or settings.get("anthropic_api_key", "")
    )
    categories = fetcher.normalize_categories(settings.get("categories", []))
    keywords = settings.get("keywords", [])
    priorities = settings.get("importance_priorities", "")

    source_ai_enabled = {}
    for src in (sources.get("rss", []) + sources.get("telegram", [])):
        source_ai_enabled[src.get("id")] = bool(src.get("ai_enabled", True))

    items = STORAGE.load_items()
    if not items:
        return

    # Keywords stage
    if keywords:
        for item in items:
            full_text = f"{item.get('title','')} {item.get('text','')}"
            item["matched_keywords"] = fetcher.match_keywords(full_text, keywords)
    else:
        for item in items:
            item.setdefault("matched_keywords", [])

    default_cat = categories[0]["id"] if categories else ""
    for item in items:
        item.setdefault("summary", "")
        item.setdefault("category", default_cat)
        item.setdefault("importance", 5)
        item.setdefault("is_duplicate", False)

    candidates = [
        it for it in items
        if source_ai_enabled.get(it.get("source_id"), True)
        and not it.get("analyzed_at")
    ]
    if ai_enabled and api_key and categories and candidates:
        BATCH = 15
        for i in range(0, len(candidates), BATCH):
            batch = candidates[i:i + BATCH]
            try:
                result = fetcher.analyze_batch(batch, api_key, categories, ai_model, priorities)
                for item, analyzed in zip(batch, result):
                    item["summary"] = analyzed.get("summary", item.get("summary", ""))
                    item["category"] = analyzed.get("category", item.get("category", default_cat))
                    item["importance"] = int(analyzed.get("importance", item.get("importance", 5)) or 5)
                    item["is_duplicate"] = bool(analyzed.get("is_duplicate", item.get("is_duplicate", False)))
                    item["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                LOGGER.warning("[ANALYZER] batch failed: %s", e)
    else:
        for item in candidates:
            item["analyzed_at"] = datetime.now(timezone.utc).isoformat()

    STORAGE.upsert_items(items)
    LOGGER.info("[ANALYZER] items: %s | candidates: %s", len(items), len(candidates))


if __name__ == "__main__":
    asyncio.run(run())

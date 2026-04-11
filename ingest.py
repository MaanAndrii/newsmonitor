import asyncio
from datetime import datetime, timezone

from config import DEFAULT_SETTINGS, DEFAULT_SOURCES, SETTINGS_FILE, SOURCES_FILE
from io_utils import load_json
from storage import Storage
from utils import setup_logging
import fetcher

LOGGER = setup_logging("newsmonitor.ingest")
STORAGE = Storage()


async def run():
    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    sources = load_json(SOURCES_FILE, DEFAULT_SOURCES)
    rss_depth = max(1, int(settings.get("rss_depth", 10)))
    keep_days = max(1, int(settings.get("keep_days", 14)))
    max_items = max(10, int(settings.get("max_items", 500)))

    rss_items = fetcher.fetch_rss(sources.get("rss", []), rss_depth)
    existing = STORAGE.load_items()
    by_id = {it["id"]: it for it in existing}

    new_count = 0
    for it in rss_items:
        prev = by_id.get(it["id"])
        if not prev:
            new_count += 1
            by_id[it["id"]] = {
                **it,
                "summary": "",
                "category": "",
                "importance": 5,
                "is_duplicate": False,
                "matched_keywords": [],
            }
        else:
            # Оновлюємо “сирі” поля, enrichment поля залишає analyzer
            prev.update({
                "title": it.get("title", prev.get("title", "")),
                "text": it.get("text", prev.get("text", "")),
                "url": it.get("url", prev.get("url", "")),
                "time": it.get("time", prev.get("time", "")),
                "source": it.get("source", prev.get("source", "")),
                "source_id": it.get("source_id", prev.get("source_id", "")),
                "type": it.get("type", prev.get("type", "rss")),
            })

    all_items = list(by_id.values())
    all_items.sort(key=lambda x: x.get("time", ""), reverse=True)
    all_items = fetcher.cleanup_old_items(all_items, keep_days, max_items)
    STORAGE.upsert_items(all_items)
    STORAGE.set_kv("new_count", int(new_count))
    STORAGE.set_kv("updated_at", datetime.now(timezone.utc).isoformat())
    LOGGER.info("[INGEST] RSS: %s | total: %s | new: %s", len(rss_items), len(all_items), new_count)


if __name__ == "__main__":
    asyncio.run(run())

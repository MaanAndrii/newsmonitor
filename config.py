"""
config.py — шляхи до файлів та значення за замовчуванням
Всі налаштування порожні — заповнюються через інтерфейс при першому запуску
"""

SOURCES_FILE  = "sources.json"
SETTINGS_FILE = "settings.json"
DATA_FILE     = "news_data.json"
READ_FILE     = "read_items.json"
SEEN_FILE     = "seen_ids.json"
SESSION_FILE  = "tg_session"
LISTENER_FILE = "listener_status.json"
LOCK_FILE     = "listener.lock"
DB_FILE       = "newsmonitor.db"
APP_VERSION   = "3.3"

# Доступні моделі Claude
AI_MODELS = [
    {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5  — швидко і дешево (за замовчуванням)"},
    {"id": "claude-sonnet-4-6",         "name": "Sonnet 4.6 — оптимальна якість"},
    {"id": "claude-opus-4-6",           "name": "Opus 4.6   — найвища якість"},
]
DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"

# Критерії важливості — передаються Claude в промпті
IMPORTANCE_CRITERIA = """Критерії оцінки важливості (1-10):
9-10 — надзвичайна подія: жертви, катастрофа, рішення вищого керівництва країни, оголошення війни/миру
7-8  — важлива подія: значне рішення влади, резонансний злочин, суттєві економічні або безпекові зміни
5-6  — стандартна новина: регіональні події, планові засідання, статистика, коментарі чиновників
3-4  — другорядна новина: анонси заходів, незначні призначення, культурні події
1-2  — низька важливість: прес-релізи, реклама, вітання, технічні оголошення"""

# Порожні структури за замовчуванням
DEFAULT_SOURCES = {
    "rss":      [],
    "telegram": []
}

DEFAULT_SETTINGS = {
    # API ключі — заповнюються через інтерфейс
    "anthropic_api_key": "",
    "telegram_api_id":   0,
    "telegram_api_hash": "",
    # AI
    "ai_enabled":             False,
    "ai_model":               DEFAULT_AI_MODEL,
    "importance_priorities":  "",
    # Збір
    "rss_depth":              10,
    "auto_fetch_interval":    0,
    "listener_enabled":       False,
    # Зберігання
    "keep_days":              14,
    "max_items":              500,
    # Telegram Bot
    "bot_token":              "",
    "bot_chat_id":            "",
    # Дайджест
    "digest_enabled":         False,
    "digest_time":            "09:00",
    "digest_count":           5,
    # Категорії та ключові слова — порожні, налаштовуються вручну
    "categories":             [],
    "keywords":               [],
    # Security
    "web_auth_enabled":       False,
}

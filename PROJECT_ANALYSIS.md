# Ретельний аналіз проєкту `newsmonitor`

## 1. Що це за система
`newsmonitor` — локальний Python-сервіс для редакційного моніторингу новин із двох потоків:
- **batch-потік** RSS через `fetcher.py`;
- **real-time потік** Telegram через `listener.py`.

Усе керується через web-інтерфейс (`index.html`) і API в `server.py`. Дані зберігаються в SQLite (`newsmonitor.db`) через thin data-layer `storage.py` з частковою legacy-сумісністю (JSON-файли стану/експорту).

## 2. Архітектура та відповідальність модулів

### `server.py` (HTTP API + orchestration)
- Піднімає `ThreadingHTTPServer` на порту `8000`.
- Обслуговує UI та API (`/api/news`, `/api/settings`, `/api/sources`, `/api/health`, `/api/refresh`, `/api/listener/*`, Telegram auth endpoints).
- Запускає `fetcher.py` у фоновому потоці через `subprocess.run` і тримає статус виконання.
- Планує авто-збір (`threading.Timer`) та щоденний digest.
- Працює зі сховищем через `Storage`.

### `fetcher.py` (batch RSS pipeline)
- Читає `sources.json` + `settings.json`.
- Забирає RSS feed-и (`feedparser`), нормалізує запис новин.
- Проводить keyword-matching.
- Опційно робить batch AI-класифікацію (Anthropic Claude) з категоріями, важливістю і дубляжем.
- Оновлює SQLite + формує legacy-`news_data.json`.

### `listener.py` (Telegram real-time pipeline)
- Працює через Telethon, вимагає Telegram session-файл (`tg_session.session`).
- Підписується на `events.NewMessage`, відсікає короткі/нерелевантні повідомлення.
- Для кожного повідомлення: дедуп, keyword matching, опційно AI single-item аналіз, запис у SQLite.
- Пише heartbeat і diagnostics у `listener_status.json`.

### `storage.py` (SQLite layer)
- Таблиці: `kv`, `news_items`, `seen_ids`, `read_ids`.
- Налаштування SQLite для edge-hosting (WAL + `synchronous=NORMAL`).
- Upsert і cleanup новин, експорт payload для API.

### `utils.py`
- JSON-formatter для логів.
- Універсальний `retry_call` з backoff/jitter.
- `env_secret` для пріоритету секретів із env.

### `index.html`
- Великий single-file frontend без framework.
- Викликає API серверу; підтримує dashboard, фільтри, керування джерелами й налаштуваннями.

## 3. Потік даних (end-to-end)
1. Користувач додає RSS/Telegram джерела та налаштування через UI.
2. `server.py` зберігає конфіг (`sources.json`, `settings.json`) і/або запускає `fetcher.py`.
3. `fetcher.py` тягне RSS, класифікує, пише в SQLite, оновлює `seen_ids`, експортує `news_data.json`.
4. `listener.py` паралельно ловить Telegram повідомлення в реальному часі і також апдейтить SQLite.
5. `GET /api/news` віддає клієнту агрегований payload зі сховища (`Storage.export_news_payload`).

## 4. Сильні сторони
- **Дуже практична архітектура для self-hosted сценарію**: без важкого стеку, швидкий старт.
- **Надійніша персистентність** завдяки SQLite (краще за попередній pure-JSON підхід).
- **Є retry/backoff** для зовнішніх інтеграцій (Telegram Bot API, RSS/AI виклики).
- **Розділення batch та real-time** дає гнучкість по навантаженню й затримці.
- **Діагностика listener-а** (bound/unbound channels, last message per source) — корисно для підтримки.

## 5. Виявлені технічні ризики

### 5.1 Безпека
- У `server.py` метод `Handler._auth_required()` жорстко повертає `False`, тому адмін-auth фактично відключений (навіть якщо в налаштуваннях/README є очікування захисту).
- Частина секретів все ще може жити в `settings.json` (env має пріоритет, але plaintext storage лишається).
- Немає вбудованого TLS/rate-limit/CORS-hardening (для публічної мережі це ризик).

### 5.2 Узгодженість даних
- `fetcher.py` і `listener.py` одночасно працюють із SQLite + legacy JSON файлами (`news_data.json`, `seen_ids.json`).
- У listener є лише process-local lock (`threading.Lock`) для append, але між двома різними процесами немає єдиного high-level coordination для legacy JSON snapshots.

### 5.3 Операційна стабільність
- Логи частково через `print`, частково через logger — ускладнює централізований моніторинг.
- `subprocess.run(fetcher.py)` запускається у thread; при частих викликах важливо контролювати перекриття (базовий guard є через `_fetcher_status["running"]`).
- AI-відповідь парситься як JSON без schema-validation (ризик падінь на malformed output).

### 5.4 Тестування
- Поточне покриття мінімальне: є тести тільки для storage cleanup/sorting і retry/logger.
- Немає інтеграційних тестів для API, fetcher pipeline, listener status lifecycle.

## 6. Технічний борг / місця для рефакторингу
- Дублювання утиліт `load_json`/`write_json` у трьох модулях (`server.py`, `fetcher.py`, `listener.py`).
- Змішування відповідальностей: `server.py` містить і HTTP layer, і scheduling, і Telegram auth flow.
- Великий monolithic `index.html` (складніше підтримувати та тестувати frontend).

## 7. Пріоритетний план покращень

### P0 (критично)
1. Реально увімкнути web-auth (`_auth_required`) і покрити це тестом.
2. Прибрати збереження секретів у файлі або зашифрувати/винести в env-only політику.
3. Додати чітку стратегію синхронізації для legacy JSON snapshots або повністю перейти на SQLite-only API output.

### P1 (важливо)
1. Винести загальні IO/JSON helpers у спільний модуль.
2. Додати валідацію AI JSON (pydantic/jsonschema) + fallback на дефолтні значення.
3. Додати API integration tests (мінімум `/api/news`, `/api/settings`, `/api/refresh`, `/api/health`).

### P2 (поліпшення)
1. Розбити frontend на модулі (навіть без framework).
2. Уніфікувати логування (без `print`, тільки structured logs).
3. Додати метрики/health деталізацію (latency, кількість подій, останні помилки по каналах).

## 8. Підсумок
Проєкт уже має сильну прикладну цінність: працює локально, підтримує RSS + Telegram у двох режимах (batch/real-time), має AI-класифікацію і зручний UI. Ключовий next step для production-ready стану — **посилення auth/security**, **прибрання неоднозначності з legacy JSON-шаром**, і **системне розширення тестів**.

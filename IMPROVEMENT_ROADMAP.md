# Дорожня карта покращень `newsmonitor`

> Мета: перевести проєкт із стану «працюючий MVP» у «стабільний self-hosted production» без різкого переписування всієї системи.

## 0) Ключові проблеми, які закриває roadmap
- Веб-авторизація фактично вимкнена.
- Змішане зберігання SQLite + legacy JSON ускладнює консистентність.
- Мінімальне автоматичне тестування.
- Частково неуніфіковане логування та обробка помилок AI-відповідей.

---

## Фаза 1 (Тиждень 1): Security baseline (P0)

### 1.1 Увімкнути і перевірити адмін-авторизацію
**Задачі**
- Реалізувати реальну умову в `Handler._auth_required()` на базі env/налаштувань.
- Заблокувати всі admin endpoints без сесії.
- Додати unit/integration тести для `401/200` сценаріїв.

**Критерії готовності (DoD)**
- Анонімний доступ до admin API повертає `401`.
- Після login доступ дозволений, після logout — знову `401`.
- Тести проходять локально й у CI.

### 1.2 Політика секретів
**Задачі**
- Перевести ключі (`Anthropic`, `Telegram API hash`, `Bot token`) у env-only режим.
- У UI лишити тільки стани `has_*`, без повернення секретних значень.
- Описати процедуру ротації секретів у README.

**DoD**
- `settings.json` більше не містить секретів.
- Документація містить інструкцію ротації без даунтайму.

### 1.3 Мінімальний hardening HTTP
**Задачі**
- Додати security headers (мінімум `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`).
- Для продакшн запуску описати reverse proxy (TLS + базові rate limits).

**DoD**
- Заголовки присутні у відповідях.
- README має робочий приклад конфігурації проксі.

---

## Фаза 2 (Тиждень 2): Data consistency & reliability (P0/P1)

### 2.1 Єдине джерело істини: SQLite-first
**Задачі**
- Прибрати критичну залежність рантайму від `news_data.json`.
- Залишити JSON тільки як опціональний export/debug snapshot.
- Формалізувати контракт `Storage.export_news_payload()`.

**DoD**
- UI/API працює без читання legacy JSON.
- Немає race-sensitive місць, які ламають консистентність між процесами.

### 2.2 Уніфікація доступу до файлів/JSON
**Задачі**
- Винести `load_json/write_json` в один модуль (наприклад `io_utils.py`).
- Замінити дублікати в `server.py`, `fetcher.py`, `listener.py`.

**DoD**
- Один набір helper-ів для JSON IO.
- Менше дубльованого коду, простіший review.

### 2.3 Надійність AI парсингу
**Задачі**
- Додати schema validation (jsonschema/pydantic) для відповідей Claude.
- На invalid JSON застосовувати fallback (default category/importance + лог помилки).

**DoD**
- Некоректна AI-відповідь не валить pipeline.
- Логи чітко пояснюють причину fallback.

---

## Фаза 3 (Тиждень 3): Test coverage & CI (P1)

### 3.1 API smoke/integration тести
**Мінімум**
- `GET /api/health`, `GET /api/news`, `GET /api/settings`.
- `POST /api/settings` (валідація типів/полів).
- auth сценарії для admin endpoints.

### 3.2 Pipeline тести
**Мінімум**
- RSS parsing + dedup + cleanup.
- Listener append flow (з mock Telethon event).
- Retry/backoff перевірки на тимчасових помилках зовнішніх API.

### 3.3 CI
**Задачі**
- Автозапуск `python -m unittest discover -s tests` на PR.
- Додати lint (мінімально `ruff` або `flake8`).

**DoD**
- Кожен PR має зелений тестовий прогін.
- Базові регресії ловляться до merge.

---

## Фаза 4 (Тиждень 4): Observability & operations (P2)

### 4.1 Уніфіковане structured logging
**Задачі**
- Прибрати більшість `print`, перейти на logger у `fetcher.py`/`listener.py`.
- Додати `request_id`/операційні поля де можливо.

### 4.2 Розширений health/status
**Задачі**
- Додати в `/api/health` технічні деталі: age останнього successful fetch, listener heartbeat age, остання помилка.

### 4.3 Runbook
**Задачі**
- Описати типові інциденти: «listener не конектиться», «AI падає», «бот не відправляє», «RSS віддає сміття».

**DoD**
- Оператор має покрокові інструкції відновлення сервісу.

---

## KPI успіху (через 30 днів)
- **Security:** 0 відкритих admin endpoint без auth.
- **Reliability:** 0 критичних інцидентів втрати новин через race/неконсистентність.
- **Quality:** не менше 15–20 автоматичних тестів (зараз базово 4).
- **Operations:** час первинної діагностики інциденту < 15 хв.

---

## Пріоритезація задач (короткий backlog)
1. Увімкнути auth + тести на auth.
2. Env-only secrets + ротація.
3. SQLite-first (мінімізація legacy JSON залежності).
4. Валідація AI output + fallback.
5. API integration tests + CI.
6. Structured logging і runbook.

# Аналіз проєкту `newsmonitor`

## 1) Призначення
`newsmonitor` — локальний Python-проєкт для моніторингу новин з RSS і Telegram, з опційним AI-аналізом (Anthropic Claude), фільтрацією за категоріями/ключовими словами та веб-дашбордом для керування.  

Ключові сценарії:
- Збір новин із RSS та Telegram.
- Класифікація та скоринг важливості новин через Claude.
- Оповіщення в Telegram-бот і щоденний дайджест.
- Окремий real-time listener для Telegram-каналів.

## 2) Архітектура

### Компоненти
- `server.py` — HTTP API + статика + orchestration задач (`fetcher.py`, digest, автозбір, Telegram auth).  
- `fetcher.py` — пакетний збір даних (RSS/Telegram), AI-аналіз, запис результатів у `news_data.json`.  
- `listener.py` — безперервний слухач нових Telegram-повідомлень через Telethon, точковий AI-аналіз і дозапис у `news_data.json`.  
- `index.html` — SPA-подібний фронтенд (чистий HTML/CSS/JS) з вкладками Дашборд/Джерела/Налаштування.  
- `config.py` — шляхи файлів + дефолтні налаштування/моделі/критерії важливості.

### Дані та файли стану
- `sources.json` — перелік RSS/Telegram джерел.
- `settings.json` — ключі API та робочі параметри.
- `news_data.json` — агреговані новини.
- `seen_ids.json` — дедуплікація вхідних елементів.
- `read_items.json` — UX-стан “прочитано” у дашборді.
- `listener_status.json`, `listener.lock` — статус/блокування listener-процесу.

## 3) API-контур (backend)
`server.py` реалізує JSON API, серед ключових endpoint'ів:
- `GET /api/news`, `GET /api/sources`, `GET /api/settings`, `GET /api/status`, `GET /api/listener/status`, `GET /api/refresh`, `GET /api/tg/session`.
- `POST /api/sources`, `/api/sources/toggle`, `/api/settings`, `/api/news/read`, `/api/news/unread`, `/api/news/clear_read`, `/api/news/send`, `/api/tg/send_code`, `/api/tg/sign_in`, `/api/tg/logout`.
- `DELETE /api/sources`.

Це дає фронтенду повний цикл керування без окремого фреймворку/ORM.

## 4) Сильні сторони
- **Проста структура**: 1 сервер + 2 воркери + 1 статичний фронт.
- **Атомарний запис JSON** через `*.tmp` + `os.replace` (зменшує ризик битих файлів).
- **Дедуплікація** та **очищення за часом/лімітом** у `fetcher.py` і `listener.py`.
- **Гнучка конфігурація** через UI: джерела, AI, ключові слова, автооновлення, дайджест.
- **Graceful fallback**: якщо AI вимкнено/ключ відсутній — система може продовжувати базовий збір.

## 5) Технічні ризики та вузькі місця
1. **Зберігання секретів у plaintext JSON** (`settings.json`), без шифрування/vault.
2. **Single-process JSON storage** замість БД:
   - конкуренція записів між `fetcher.py` і `listener.py` (частково пом'якшено lock'ами/атомарними replace, але між процесами lock не централізований для всіх файлів).
3. **`http.server` для production не підходить** (без auth, TLS, rate-limit, middleware).
4. **Відсутні автоматичні тести** (unit/integration/e2e).
5. **Виклики зовнішніх API без retry/backoff політики** (Anthropic/Telegram/RSS можуть деградувати).
6. **Без централізованого логування/метрик** — важко дебажити інциденти.

## 6) Що варто зробити в першу чергу

### P0 (критично)
- Перенести секрети з `settings.json` у env/secret-store.
- Додати просту авторизацію на веб-інтерфейс (хоча б basic auth/reverse proxy).
- Впровадити взаємовиключення на рівні процесів для запису `news_data.json` (file lock).

### P1 (важливо)
- Перейти на SQLite (джерела/новини/стан) замість flat JSON.
- Додати retry + exponential backoff + timeout policy для зовнішніх API.
- Додати тестовий мінімум: парсинг RSS, дедуп, cleanup, API smoke.

### P2 (покращення)
- Виділити backend у FastAPI/Flask для кращої підтримуваності.
- Додати спостережуваність: structured logging + health endpoint + лічильники.
- Винести prompt templates у окремий модуль/файли.

## 7) Висновок
Проєкт добре підходить як **MVP/внутрішній інструмент редакції**: швидко запускається, має корисний UI та працює без складної інфраструктури. Для стабільного продакшн-використання ключовий наступний крок — посилення безпеки, надійності зберігання даних і тестового покриття.

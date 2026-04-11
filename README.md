# News Monitor — запуск і продакшн-гайд (Raspberry Pi)

## Що змінилось
- Зберігання новин/seen/read перенесено на SQLite (`newsmonitor.db`) для стабільної роботи на Raspberry Pi.
- Додано retry/backoff для зовнішніх API (Telegram Bot, RSS, Anthropic).
- Додано `GET /api/health` для health-check.
- Додано structured JSON logging.
- Додано Basic Auth для веб-інтерфейсу через env (`NEWSMONITOR_AUTH_USER/PASS`).
- Підтримано читання секретів із env (`NEWSMONITOR_ANTHROPIC_API_KEY`, `NEWSMONITOR_TELEGRAM_API_HASH`, `NEWSMONITOR_BOT_TOKEN`).

---

## Швидкий запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

Веб: `http://<raspberry-ip>:8000`

---

## Безпека (обов'язково для продакшну)

### Що таке «секрети» і для чого вони?
**Секрети** — це чутливі дані, які дають доступ до зовнішніх сервісів або адмін-функцій.

У цьому проєкті це, зокрема:
- `NEWSMONITOR_AUTH_USER` / `NEWSMONITOR_AUTH_PASS` — вхід в адмінку.
- `NEWSMONITOR_ANTHROPIC_API_KEY` — доступ до AI-аналізу (Claude API).
- `NEWSMONITOR_TELEGRAM_API_HASH` (+ `telegram_api_id`) — доступ до Telegram client API.
- `NEWSMONITOR_BOT_TOKEN` — доступ до Telegram Bot API.

Чому це важливо:
- якщо секрет витече, стороння людина може керувати вашими інтеграціями або витрачати ваші API-квоти;
- секрети не можна публікувати в Git, скріншотах, логах або чатах;
- у продакшні їх краще зберігати в env/systemd variables або в secret manager.

Перед запуском задайте:

```bash
export NEWSMONITOR_AUTH_USER=admin
export NEWSMONITOR_AUTH_PASS='strong-password'
export NEWSMONITOR_ANTHROPIC_API_KEY='...'
export NEWSMONITOR_TELEGRAM_API_HASH='...'
export NEWSMONITOR_BOT_TOKEN='...'
```

> Секрети можуть бути збережені в `settings.json`, але значення з env мають пріоритет
> (`NEWSMONITOR_ANTHROPIC_API_KEY`, `NEWSMONITOR_TELEGRAM_API_HASH`, `NEWSMONITOR_BOT_TOKEN`).
> Для веб-адмінки увімкніть авторизацію:
>
> ```bash
> export NEWSMONITOR_AUTH_USER=admin
> export NEWSMONITOR_AUTH_PASS='strong-password'
> ```

---

## Raspberry Pi deployment (systemd)

### 1) Юніт серверу `/etc/systemd/system/newsmonitor-server.service`

```ini
[Unit]
Description=NewsMonitor Server
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/newsmonitor
Environment="NEWSMONITOR_AUTH_USER=admin"
Environment="NEWSMONITOR_AUTH_PASS=change-me"
Environment="NEWSMONITOR_LOG_LEVEL=INFO"
ExecStart=/home/pi/newsmonitor/.venv/bin/python3 server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 2) Юніт listener `/etc/systemd/system/newsmonitor-listener.service`

```ini
[Unit]
Description=NewsMonitor Telegram Listener
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/newsmonitor
ExecStart=/home/pi/newsmonitor/.venv/bin/python3 listener.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 3) Увімкнення

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now newsmonitor-server
sudo systemctl enable --now newsmonitor-listener
```

### 4) Перевірка

```bash
curl -u admin:change-me http://127.0.0.1:8000/api/health
journalctl -u newsmonitor-server -f
```

---

## Тести

```bash
python -m unittest discover -s tests
```

CI запускає:
- `ruff check io_utils.py tests`
- `python -m unittest discover -s tests`
- `python -m py_compile server.py fetcher.py listener.py io_utils.py storage.py utils.py`

## Legacy JSON snapshot (опційно)

За замовчуванням API працює зі SQLite як єдиним джерелом істини.
Якщо потрібен legacy `news_data.json` для зовнішніх інтеграцій — увімкніть:

```bash
export NEWSMONITOR_WRITE_LEGACY_JSON=true
```

## Як синхронізувати виправлення: Git → Raspberry Pi

### 1) На вашому ПК (де розробка)
```bash
git add .
git commit -m "your fix"
git push origin <branch>
```

### 2) На Raspberry Pi
```bash
cd /home/<user>/newsmonitor
git fetch --all --prune
git checkout <branch>
git pull origin <branch>
/home/<user>/newsmonitor/.venv/bin/pip install -r requirements.txt
sudo systemctl restart newsmonitor-server newsmonitor-listener
```

### 3) Перевірка
```bash
curl -u <user>:<pass> http://127.0.0.1:8000/api/version
curl -u <user>:<pass> http://127.0.0.1:8000/api/health
```

### 4) Рекомендований швидкий сценарій оновлення (копіпаст)
```bash
cd /home/<user>/newsmonitor \
&& git fetch --all --prune \
&& git checkout <branch> \
&& git pull --ff-only origin <branch> \
&& /home/<user>/newsmonitor/.venv/bin/pip install -r requirements.txt \
&& sudo systemctl restart newsmonitor-server newsmonitor-listener \
&& curl -u <user>:<pass> http://127.0.0.1:8000/api/health
```

> Якщо `git pull --ff-only` не проходить (локальні зміни на Pi), спочатку збережіть їх:
> `git stash -u`, потім зробіть `pull`, а далі `git stash pop` за потреби.

## Troubleshooting: кнопка "Зібрати новини" не дає результату

- Перевірте `/api/status`: якщо `error` не порожній, fetcher падає під час запуску.
- Сервер запускає `fetcher.py` тим самим Python-інтерпретатором, яким запущений `server.py`, тому важливо стартувати сервіс через `.venv/bin/python3`.

Детальні інструкції з інцидентів — у `RUNBOOK.md`.

---

## Структура

- `server.py` — веб сервер + API + scheduler.
- `listener.py` — realtime Telegram listener (ingest-only у БД).
- `ingest.py` — ingest RSS у БД.
- `analyzer.py` — пост-обробка з БД (keywords + AI категорія/важливість).
- `notifier.py` — моніторинг критеріїв і відправка сповіщень.
- `pipeline.py` — оркестратор етапів ingest → analyze → notify.
- `storage.py` — SQLite storage layer.
- `utils.py` — retry/backoff + structured logging.
- `index.html` — UI.

## Версіонування
- Поточна версія повертається endpoint-ом `GET /api/version`.
- Для цього проєкту прийнято правило: після кожної зміни підвищувати версію на `0.1`.

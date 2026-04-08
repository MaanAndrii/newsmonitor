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

Перед запуском задайте:

```bash
export NEWSMONITOR_AUTH_USER=admin
export NEWSMONITOR_AUTH_PASS='strong-password'
export NEWSMONITOR_ANTHROPIC_API_KEY='...'
export NEWSMONITOR_TELEGRAM_API_HASH='...'
export NEWSMONITOR_BOT_TOKEN='...'
```

> Якщо секрет заданий в env, він має пріоритет над `settings.json`.

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
git pull origin <branch>
/home/<user>/newsmonitor/.venv/bin/pip install -r requirements.txt
sudo systemctl restart newsmonitor-server newsmonitor-listener
```

### 3) Перевірка
```bash
curl -u <user>:<pass> http://127.0.0.1:8000/api/version
curl -u <user>:<pass> http://127.0.0.1:8000/api/health
```

## Troubleshooting: кнопка "Зібрати новини" не дає результату

- Перевірте `/api/status`: якщо `error` не порожній, fetcher падає під час запуску.
- Сервер запускає `fetcher.py` тим самим Python-інтерпретатором, яким запущений `server.py`, тому важливо стартувати сервіс через `.venv/bin/python3`.

---

## Структура

- `server.py` — веб сервер + API + scheduler.
- `fetcher.py` — пакетний збір новин.
- `listener.py` — realtime Telegram listener.
- `storage.py` — SQLite storage layer.
- `utils.py` — retry/backoff + structured logging.
- `index.html` — UI.

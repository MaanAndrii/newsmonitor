# News Monitor — інструкція з запуску

## Структура проєкту

```
newsmonitor/
├── fetcher.py        ← збирає новини і запускає AI-аналіз
├── server.py         ← веб-сервер для дашборду
├── index.html        ← інтерфейс дашборду
├── requirements.txt  ← Python бібліотеки
├── .env.example      ← шаблон змінних середовища
└── news_data.json    ← генерується автоматично
```

---

## Крок 1 — Встановити Python та бібліотеки

```bash
# Переконайтесь що Python 3.10+ встановлений
python --version

# Встановити залежності
pip install -r requirements.txt
```

---

## Крок 2 — Отримати API ключі

### Anthropic (Claude)
1. Зайдіть на https://console.anthropic.com/
2. Settings → API Keys → Create Key
3. Скопіюйте ключ (починається з `sk-ant-...`)

### Telegram
1. Зайдіть на https://my.telegram.org
2. Увійдіть своїм номером телефону
3. "API development tools" → створіть застосунок
4. Скопіюйте `api_id` (число) та `api_hash` (рядок)

---

## Крок 3 — Налаштувати ключі

```bash
# Скопіюйте шаблон
cp .env.example .env

# Відредагуйте .env і вставте свої ключі
nano .env   # або будь-який текстовий редактор

# Завантажте змінні
source .env
```

Або вставте ключі напряму в fetcher.py (рядки 14-16).

---

## Крок 4 — Перший запуск Telegram (авторизація)

При першому запуску Telethon попросить вас увійти в Telegram:

```bash
python fetcher.py
```

Введіть свій номер телефону (+380...) і код підтвердження.
Сесія збережеться у файл `tg_session.session` — наступні запуски без авторизації.

---

## Крок 5 — Запустити сервер і дашборд

```bash
# В одному терміналі — сервер
python server.py

# Відкрийте браузер
# http://localhost:8000
```

Натисніть **"Зібрати новини"** в дашборді — fetcher запуститься автоматично (~60 сек).

---

## Автоматичне оновлення (кожні 30 хвилин)

### Linux/Mac (cron):
```bash
crontab -e
# Додати рядок:
*/30 * * * * cd /шлях/до/newsmonitor && source .env && python fetcher.py
```

### Windows (Task Scheduler):
Створіть завдання що запускає `python fetcher.py` кожні 30 хвилин.

---

## Додати нові джерела

### RSS сайт — в fetcher.py:
```python
RSS_SOURCES = [
    {"id": "up_main", "name": "Укр. правда", "url": "https://www.pravda.com.ua/rss/view_mainnews/"},
    {"id": "nv",      "name": "НВ",          "url": "https://nv.ua/rss/all.xml"},
    # додайте сюди...
]
```

### Telegram канал — в fetcher.py:
```python
TELEGRAM_CHANNELS = [
    {"id": "dmytro_lubinetzs", "name": "Дмитро Лубінець"},
    # додайте сюди username каналу (без @)...
]
```

---

## Орієнтовна вартість Claude API

| Обсяг | Вартість/день |
|-------|--------------|
| 9 джерел, кожні 30 хв | ~$0.02–0.05 |
| 30 джерел, кожні 15 хв | ~$0.10–0.20 |

Модель `claude-sonnet-4` — оптимальний баланс ціна/якість.

# ?? Series Tracker Bot

Telegram-бот для відстеження серіалів через TMDB з простим меню, локалізацією та автоматичними сповіщеннями про нові епізоди.

## Функціонал

- ?? **Зручне меню** (кнопки в чаті): пошук, список, допомога, зміна мови
- ?? **Пошук у приватному чаті** — без `@bot ...` (inline лишився як опція)
- ?? **Локалізація інтерфейсу** — `uk`, `en`, `de`
- ?? **Картка серіалу** — постер, опис, рейтинг, жанри, статус
- ?? **Відстеження сезону** — додавання у watchlist
- ?? **Автоперевірка епізодів** — кожні N хвилин (за змінною `CHECK_INTERVAL_MINUTES`)

## Структура

```text
bot.py            # хендлери, FSM, локалізація, планувальник
db.py            # БД (users/watchlist/user_settings)
tmdb.py           # клієнт TMDB API
locales/          # uk.json / en.json / de.json
requirements.txt
Procfile
.env.example
```

## Встановлення

```bash
git clone <your-repo>
cd film_bot
pip install -r requirements.txt
cp .env.example .env
```

Заповни `.env`:

- `BOT_TOKEN` — від `@BotFather`
- `TMDB_API_TOKEN` — TMDB API Read Access Token v4
- `DATABASE_URL` (або `DATABASE_PUBLIC_URL`) — PostgreSQL DSN
- `CHECK_INTERVAL_MINUTES` (опційно, мін. `5`, за замовчуванням `15`)

Локальний запуск:

```bash
python bot.py
```

## Деплой на Railway

1. Deploy from GitHub repo
2. Додай PostgreSQL у той самий проєкт
3. У Variables сервісу бота задай:
   - `BOT_TOKEN`
   - `TMDB_API_TOKEN`
   - `DATABASE_URL` (краще private reference з Postgres)
4. Start command: `python bot.py` (або через `Procfile`)

## Локалізація

Файли перекладів:

- `locales/uk.json`
- `locales/en.json`
- `locales/de.json`

Мова користувача визначається з Telegram на першому `/start`, потім можна змінити через кнопку `??`.
Вибір мови зберігається в таблиці `user_settings`.

## Як працюють сповіщення

Планувальник (APScheduler) кожні `CHECK_INTERVAL_MINUTES`:

1. читає всі записи `watchlist`
2. перевіряє, скільки епізодів уже вийшло в TMDB
3. якщо вийшли нові — надсилає сповіщення і оновлює `last_notified_episode`

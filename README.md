# 📺 Series Tracker Bot

Telegram-бот для відстеження серіалів через TMDB з щоденними сповіщеннями про нові епізоди.

## Функціонал

- 🔍 **Автодоповнення пошуку** — через InlineQuery (`@bot Назва серіалу`)
- 🖼 **Постер + опис** — рейтинг, жанри, статус серіалу
- 📺 **Вибір сезону** — відстеження конкретного сезону
- 🔔 **Щоденні сповіщення** — о 10:00 перевіряє нові епізоди
- 📋 `/list` — перегляд і видалення зі списку відстеження

## Технічний стек

| Компонент    | Технологія              |
|-------------|-------------------------|
| Bot framework| aiogram 3.x            |
| HTTP client  | aiohttp                 |
| Database     | PostgreSQL (asyncpg)    |
| Scheduler    | APScheduler             |
| API          | TMDB API v3/v4          |
| Deploy       | Railway                 |

## Структура проєкту

```
├── bot.py          # Хендлери, планувальник, точка входу
├── db.py           # Робота з PostgreSQL
├── tmdb.py         # TMDB API клієнт
├── requirements.txt
├── Procfile        # Railway deployment
└── .env.example
```

## Встановлення

### 1. Клонуйте репозиторій

```bash
git clone <your-repo>
cd series-tracker-bot
```

### 2. Отримайте токени

**Telegram:**
1. Напишіть @BotFather → `/newbot`
2. Скопіюйте `BOT_TOKEN`
3. Увімкніть Inline Mode: `/setinline` → оберіть бота → введіть підказку (напр. "Шукати серіал...")

**TMDB:**
1. Зареєструйтесь на [themoviedb.org](https://www.themoviedb.org)
2. Налаштування → API → "API Read Access Token (v4 auth)"
3. Скопіюйте довгий токен (починається з `eyJ...`)

### 3. Налаштуйте змінні середовища

```bash
cp .env.example .env
# Відредагуйте .env і вставте свої токени
```

### 4. Локальний запуск

```bash
pip install -r requirements.txt
python bot.py
```

## Деплой на Railway

### Варіант А — через GitHub (рекомендовано)

1. Запушіть код на GitHub
2. Зайдіть на [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Додайте сервіс PostgreSQL: `+ New` → Database → PostgreSQL
4. У змінних середовища сервісу бота додайте:
   - `BOT_TOKEN` 
   - `TMDB_API_TOKEN`
   - `DATABASE_URL` — скопіюйте з вкладки PostgreSQL сервісу (кнопка "Connect")
5. Railway автоматично підхопить `Procfile`

### Варіант Б — Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway add --database postgresql
railway variables set BOT_TOKEN=... TMDB_API_TOKEN=...
railway up
```

## Використання

```
Пошук серіалу:
  Введіть @your_bot_name Назва серіалу в будь-якому чаті
  → виберіть із списку → побачите постер
  → кнопка "Стежити" → виберіть сезон

Команди:
  /start  — запуск і вітання
  /list   — список відстежуваних серіалів (з можливістю видалення)
  /help   — довідка
```

## Схема бази даних

```sql
users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  full_name TEXT,
  joined_at TIMESTAMPTZ
)

watchlist (
  id SERIAL PRIMARY KEY,
  user_id BIGINT → users,
  series_id INTEGER,          -- TMDB ID
  series_name TEXT,
  poster_path TEXT,
  season_number INTEGER,
  total_seasons INTEGER,
  last_notified_episode INTEGER,  -- скільки епізодів вже оголошено
  added_at TIMESTAMPTZ,
  UNIQUE(user_id, series_id, season_number)
)
```

## Як працюють сповіщення

Щодня о 10:00 (Kyiv time) планувальник:
1. Отримує всі записи watchlist
2. Для кожного запитує TMDB: скільки епізодів вийшло (air_date ≤ сьогодні)
3. Порівнює з `last_notified_episode`
4. Якщо нові є — надсилає повідомлення з постером і оновлює лічильник

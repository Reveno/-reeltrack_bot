"""
Series Tracker Bot
──────────────────
Autocomplete series search via InlineQuery → TMDB poster + info →
season picker → daily episode notifications.

Requires env vars: BOT_TOKEN, TMDB_API_TOKEN, DATABASE_URL
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import db
import tmdb

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_bot_token = (os.getenv("BOT_TOKEN") or "").strip()
if not _bot_token:
    raise RuntimeError(
        "BOT_TOKEN не задано (os.getenv повернув порожньо). "
        "Railway: відкрий сервіс, де запускається бот (не Postgres) → Variables → "
        "додай BOT_TOKEN = токен від @BotFather. Назва змінної саме BOT_TOKEN, без лапок у значенні."
    )
bot = Bot(token=_bot_token)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

PLACEHOLDER_POSTER = "https://placehold.co/500x750/1a1a2e/ffffff?text=No+Poster"


# ═══════════════════════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════════════════════

def kb_series_actions(series_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📺 Стежити за серіалом", callback_data=f"track:{series_id}")
    builder.button(text="🔍 Шукати ще", switch_inline_query_current_chat="")
    builder.adjust(1)
    return builder.as_markup()


def kb_seasons(series_id: int, seasons: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in seasons:
        n = s["season_number"]
        ep = s.get("episode_count", "?")
        if n == 0:
            continue  # skip specials
        builder.button(
            text=f"Сезон {n}  ({ep} еп.)",
            callback_data=f"season:{series_id}:{n}",
        )
    builder.button(text="↩ Назад", callback_data=f"info:{series_id}")
    builder.adjust(2)
    return builder.as_markup()


def kb_my_list(items) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(
            text=f"🗑 {item['series_name']} — С{item['season_number']}",
            callback_data=f"remove:{item['id']}",
        )
    builder.adjust(1)
    return builder.as_markup()


# ═══════════════════════════════════════════════════════════════════════════════
#  /start  /help
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        "👋 <b>Привіт! Я бот для відстеження серіалів.</b>\n\n"
        "🔍 <b>Як шукати серіали:</b>\n"
        "Введіть <code>@" + (await bot.get_me()).username + " Назва серіалу</code> "
        "в будь-якому чаті — з'явиться список із постерами.\n\n"
        "📋 <b>Команди:</b>\n"
        "/list — мій список відстеження\n"
        "/help — допомога\n\n"
        "Коли вийде новий епізод — я повідомлю! 🔔",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    bot_info = await bot.get_me()
    await message.answer(
        "📖 <b>Як користуватися ботом</b>\n\n"
        f"1️⃣ Введіть <code>@{bot_info.username} Назва</code> в цьому або будь-якому іншому чаті\n"
        "2️⃣ Виберіть серіал зі списку → побачите постер і опис\n"
        "3️⃣ Натисніть «Стежити» → виберіть сезон\n"
        "4️⃣ Щодня о 10:00 бот перевіряє нові епізоди і надсилає сповіщення\n\n"
        "📋 /list — переглянути та видалити серіали зі списку",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Inline Query — автодоповнення пошуку
# ═══════════════════════════════════════════════════════════════════════════════

@dp.inline_query()
async def handle_inline_search(query: InlineQuery):
    text = query.query.strip()
    if len(text) < 2:
        await query.answer(
            [],
            switch_pm_text="Введіть назву серіалу (мін. 2 символи)",
            switch_pm_parameter="help",
            cache_time=1,
        )
        return

    results_data = await tmdb.search_series(text)
    items = []

    for s in results_data:
        series_id = str(s["id"])
        poster = tmdb.poster_url(s.get("poster_path")) or PLACEHOLDER_POSTER
        name = s.get("name") or s.get("original_name") or "Невідомо"
        year = (s.get("first_air_date") or "")[:4]
        overview = (s.get("overview") or "Опис відсутній.")[:200]

        caption = (
            f"<b>{name}</b>"
            + (f" ({year})" if year else "")
            + f"\n\n{overview}"
        )

        items.append(
            InlineQueryResultPhoto(
                id=series_id,
                photo_url=poster,
                thumbnail_url=poster,
                title=name,
                description=f"{year} — {overview[:80]}",
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb_series_actions(int(series_id)),
            )
        )

    await query.answer(items, cache_time=60, is_personal=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  Callback: показати деталі серіалу (кнопка «info:ID»)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("info:"))
async def cb_series_info(call: CallbackQuery):
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id)
    if data.get("success") is False:
        await call.message.answer("⚠️ Серіал не знайдено.")
        return

    caption = tmdb.format_series_info(data)
    await call.message.edit_caption(
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb_series_actions(series_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Callback: вибір сезону (кнопка «track:ID»)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("track:"))
async def cb_track_series(call: CallbackQuery):
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id)
    if data.get("success") is False:
        await call.message.answer("⚠️ Серіал не знайдено.")
        return

    seasons = [s for s in data.get("seasons", []) if s.get("season_number", 0) > 0]

    if not seasons:
        await call.message.answer("⚠️ Інформація про сезони відсутня.")
        return

    full_info = tmdb.format_series_info(data)
    await call.message.edit_caption(
        caption=full_info + "\n\n<b>Виберіть сезон для відстеження:</b>",
        parse_mode="HTML",
        reply_markup=kb_seasons(series_id, seasons),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Callback: підтвердження додавання сезону
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("season:"))
async def cb_add_season(call: CallbackQuery):
    _, series_id_str, season_str = call.data.split(":")
    series_id = int(series_id_str)
    season_number = int(season_str)

    await call.answer("⏳ Додаємо…")

    await db.upsert_user(
        call.from_user.id,
        call.from_user.username,
        call.from_user.full_name,
    )

    # Already tracking?
    if await db.is_tracking(call.from_user.id, series_id, season_number):
        await call.message.answer(
            f"⚠️ Ви вже відстежуєте цей сезон.",
            parse_mode="HTML",
        )
        return

    # Get series & season info
    series_data = await tmdb.get_series(series_id)
    if series_data.get("success") is False:
        await call.message.answer("⚠️ Серіал не знайдено.")
        return

    aired = await tmdb.count_aired_episodes(series_id, season_number)
    total_seasons = series_data.get("number_of_seasons", 1)
    series_name = series_data.get("name") or series_data.get("original_name") or "Невідомо"
    poster_path = series_data.get("poster_path")

    added = await db.add_to_watchlist(
        user_id=call.from_user.id,
        series_id=series_id,
        series_name=series_name,
        poster_path=poster_path,
        season_number=season_number,
        total_seasons=total_seasons,
        current_aired=aired,
    )

    if added:
        await call.message.answer(
            f"✅ <b>{series_name}</b> — Сезон {season_number} додано до списку відстеження!\n"
            f"Вже вийшло епізодів: <b>{aired}</b>\n\n"
            f"🔔 Повідомлення надходитимуть щодня о 10:00 при виходi нових епізодів.",
            parse_mode="HTML",
        )
    else:
        await call.message.answer("⚠️ Не вдалося додати (можливо, вже у списку).")


# ═══════════════════════════════════════════════════════════════════════════════
#  /list — список відстежуваних серіалів
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(Command("list"))
async def cmd_list(message: Message):
    items = await db.get_watchlist(message.from_user.id)

    if not items:
        await message.answer(
            "📋 Ваш список порожній.\n\n"
            "Щоб знайти серіал, введіть <code>@bot Назва</code> у рядку повідомлення.",
            parse_mode="HTML",
        )
        return

    text = "📋 <b>Ваші серіали:</b>\n\n"
    for item in items:
        ep_info = f"Переглянуто/вийшло: {item['last_notified_episode']} еп."
        text += (
            f"▪️ <b>{item['series_name']}</b> — Сезон {item['season_number']}\n"
            f"   <i>{ep_info}</i>\n"
        )

    text += "\nНатисніть кнопку нижче, щоб видалити:"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=kb_my_list(items),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Callback: видалення зі списку
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(call: CallbackQuery):
    watchlist_id = int(call.data.split(":")[1])
    await db.remove_from_watchlist(call.from_user.id, watchlist_id)
    await call.answer("✅ Видалено зі списку")
    await call.message.delete()
    await call.message.answer("✅ Серіал видалено зі списку відстеження.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduler: щоденна перевірка нових епізодів
# ═══════════════════════════════════════════════════════════════════════════════

async def check_new_episodes():
    """Daily job: check for new aired episodes and notify users."""
    log.info("🕙 Running daily episode check…")
    tracked = await db.get_all_tracked()

    for row in tracked:
        try:
            aired = await tmdb.count_aired_episodes(row["series_id"], row["season_number"])
            known = row["last_notified_episode"]

            if aired <= known:
                continue

            # New episode(s) found!
            new_count = aired - known
            ep_word = "епізод" if new_count == 1 else "епізоди" if new_count < 5 else "епізодів"

            series_data = await tmdb.get_series(row["series_id"])
            poster = tmdb.poster_url(series_data.get("poster_path"))

            text = (
                f"🔔 <b>Новий {ep_word}!</b>\n\n"
                f"<b>{row['series_name']}</b> — Сезон {row['season_number']}\n"
                f"Вийшло ще <b>{new_count} {ep_word}</b>. "
                f"Всього вийшло: <b>{aired}</b>."
            )

            if poster:
                await bot.send_photo(
                    chat_id=row["user_id"],
                    photo=poster,
                    caption=text,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=row["user_id"],
                    text=text,
                    parse_mode="HTML",
                )

            await db.update_notified_episode(row["id"], aired)
            log.info(
                f"Notified user {row['user_id']} about {row['series_name']} "
                f"S{row['season_number']} ep {known}→{aired}"
            )

        except Exception as e:
            log.error(f"Error checking {row['series_name']} S{row['season_number']}: {e}")

    log.info("✅ Episode check done.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    for var in ("BOT_TOKEN", "TMDB_API_TOKEN", "DATABASE_URL"):
        if not os.getenv(var):
            raise RuntimeError(f"Відсутня змінна середовища: {var}")

    await db.init_db()
    log.info("Database initialized.")

    scheduler.add_job(
        check_new_episodes,
        trigger="cron",
        hour=10,
        minute=0,
        timezone="Europe/Kyiv",
    )
    scheduler.start()
    log.info("Scheduler started.")

    try:
        log.info("Starting bot polling…")
        # close_bot_session=False: закриваємо сесію в finally, щоб не дублювати з aiogram
        await dp.start_polling(bot, skip_updates=True, close_bot_session=False)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_pool()
        await bot.session.close()
        log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())

"""
Series Tracker Bot
──────────────────
Меню (ReplyKeyboard) → пошук текстом у чаті → TMDB → сезони → сповіщення про нові епізоди.
Опційно: інлайн-пошук @username у будь-якому чаті.

Requires env vars: BOT_TOKEN, TMDB_API_TOKEN, DATABASE_URL (або DATABASE_PUBLIC_URL)
Optional: CHECK_INTERVAL_MINUTES (за замовчуванням 15)
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultPhoto,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
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
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

CHECK_INTERVAL_MINUTES = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES") or "15"))

PLACEHOLDER_POSTER = "https://placehold.co/500x750/1a1a2e/ffffff?text=No+Poster"

# Кнопки головного меню (текст має збігатися з F.text)
BTN_SEARCH = "🔍 Пошук серіалу"
BTN_LIST = "📋 Мій список"
BTN_HELP = "❓ Допомога"


class SearchStates(StatesGroup):
    waiting_query = State()


# ═══════════════════════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════════════════════

def main_reply_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_LIST))
    builder.row(KeyboardButton(text=BTN_HELP))
    return builder.as_markup(resize_keyboard=True)


def kb_series_actions(series_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📺 Стежити за серіалом", callback_data=f"track:{series_id}")
    builder.button(text="🔍 Інший серіал", callback_data="search:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_seasons(series_id: int, seasons: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in seasons:
        n = s["season_number"]
        ep = s.get("episode_count", "?")
        if n == 0:
            continue
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


def kb_pick_search_result(results: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in results[:10]:
        sid = s["id"]
        name = s.get("name") or s.get("original_name") or "Без назви"
        year = (s.get("first_air_date") or "")[:4]
        label = f"{name}" + (f" ({year})" if year else "")
        if len(label) > 60:
            label = label[:57] + "…"
        builder.button(text=label, callback_data=f"open:{sid}")
    builder.adjust(1)
    return builder.as_markup()


# ═══════════════════════════════════════════════════════════════════════════════
#  Допоміжні функції
# ═══════════════════════════════════════════════════════════════════════════════

async def send_watchlist_view(message: Message):
    items = await db.get_watchlist(message.from_user.id)

    if not items:
        await message.answer(
            "📋 <b>Ваш список порожній.</b>\n\n"
            f"Натисніть «{BTN_SEARCH}» і введіть назву серіалу.",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
        return

    text = "📋 <b>Ваші серіали:</b>\n\n"
    for item in items:
        ep_info = f"Останнє відоме вийшло: {item['last_notified_episode']} еп."
        text += (
            f"▪️ <b>{item['series_name']}</b> — Сезон {item['season_number']}\n"
            f"   <i>{ep_info}</i>\n"
        )

    text += "\nНатисніть кнопку нижче, щоб видалити зі списку:"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=kb_my_list(items),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  /start  /help  /cancel
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        "👋 <b>Привіт! Я бот для відстеження серіалів.</b>\n\n"
        f"Користуйся кнопками внизу:\n"
        f"• <b>{BTN_SEARCH}</b> — знайти серіал у TMDB\n"
        f"• <b>{BTN_LIST}</b> — що ти відстежуєш\n"
        f"• <b>{BTN_HELP}</b> — коротка інструкція\n\n"
        "Про нові епізоди напишу, коли TMDB оновить дані (перевірка кожні "
        f"<b>{CHECK_INTERVAL_MINUTES}</b> хв).",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📖 <b>Як користуватися</b>\n\n"
        f"1️⃣ Натисни «{BTN_SEARCH}» і напиши назву серіалу.\n"
        "2️⃣ Обери рядок зі списку — з’явиться постер і опис.\n"
        "3️⃣ «Стежити за серіалом» → вибери сезон.\n"
        f"4️⃣ Список перегляду — «{BTN_LIST}».\n\n"
        "<i>Додатково:</i> у будь-якому чаті можна набрати "
        f"<code>@{ (await bot.get_me()).username} назва</code> (інлайн-режим).\n\n"
        "/cancel — скасувати введення пошуку.",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Гаразд.", reply_markup=main_reply_keyboard())


# ═══════════════════════════════════════════════════════════════════════════════
#  Меню: пошук
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(F.text == BTN_SEARCH)
async def menu_search(message: Message, state: FSMContext):
    await db.upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await state.set_state(SearchStates.waiting_query)
    await message.answer(
        "Напишіть <b>назву серіалу</b> (мінімум 2 символи).\n"
        "Скасувати: /cancel",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@dp.message(Command("list"))
@dp.message(F.text == BTN_LIST)
async def cmd_list(message: Message, state: FSMContext):
    await state.clear()
    await send_watchlist_view(message)


@dp.message(StateFilter(SearchStates.waiting_query), F.text)
async def process_search_query(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return

    if len(raw) < 2:
        await message.answer("Занадто коротко. Мінімум 2 символів або /cancel.")
        return

    await message.answer("⏳ Шукаю…")
    results_data = await tmdb.search_series(raw)
    await state.clear()

    if not results_data:
        await message.answer(
            "Нічого не знайдено. Спробуйте іншу назву або натисніть «Пошук» ще раз.",
            reply_markup=main_reply_keyboard(),
        )
        return

    await message.answer(
        "Оберіть серіал:",
        reply_markup=kb_pick_search_result(results_data),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Відкрити картку серіалу з чату (після пошуку)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("open:"))
async def cb_open_series(call: CallbackQuery):
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id)
    if data.get("success") is False:
        await call.message.answer("⚠️ Серіал не знайдено.")
        return

    caption = tmdb.format_series_info(data)
    poster = tmdb.poster_url(data.get("poster_path")) or PLACEHOLDER_POSTER
    await call.message.answer_photo(
        photo=poster,
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb_series_actions(series_id),
    )


@dp.callback_query(F.data == "search:new")
async def cb_search_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_query)
    await call.answer()
    await call.message.answer(
        "Напишіть назву серіалу (мін. 2 символи). /cancel — скасувати.",
        reply_markup=main_reply_keyboard(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Inline Query (опційно, будь-який чат)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.inline_query()
async def handle_inline_search(query: InlineQuery):
    text = query.query.strip()
    if len(text) < 2:
        await query.answer(
            [],
            switch_pm_text="Відкрийте бота й натисніть «Пошук» або введіть мін. 2 символи",
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
#  Callback: деталі / сезони / додати / видалити
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

    if await db.is_tracking(call.from_user.id, series_id, season_number):
        await call.message.answer(
            "⚠️ Ви вже відстежуєте цей сезон.",
            parse_mode="HTML",
        )
        return

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
            f"🔔 Нагадаю про нові епізоди після оновлення даних TMDB "
            f"(перевірка кожні ~{CHECK_INTERVAL_MINUTES} хв).",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
    else:
        await call.message.answer(
            "⚠️ Не вдалося додати (можливо, вже у списку).",
            reply_markup=main_reply_keyboard(),
        )


@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(call: CallbackQuery):
    watchlist_id = int(call.data.split(":")[1])
    await db.remove_from_watchlist(call.from_user.id, watchlist_id)
    await call.answer("✅ Видалено зі списку")
    await call.message.delete()
    await call.message.answer(
        "✅ Серіал видалено зі списку відстеження.",
        reply_markup=main_reply_keyboard(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Текст поза меню / станом
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(StateFilter(default_state), F.text, ~F.text.startswith("/"))
async def idle_text_hint(message: Message):
    await message.answer(
        f"Оберіть дію кнопками внизу або натисніть «{BTN_SEARCH}».",
        reply_markup=main_reply_keyboard(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduler: періодична перевірка нових епізодів
# ═══════════════════════════════════════════════════════════════════════════════

async def check_new_episodes():
    log.info("Running episode check (interval %s min)…", CHECK_INTERVAL_MINUTES)
    tracked = await db.get_all_tracked()

    for row in tracked:
        try:
            aired = await tmdb.count_aired_episodes(row["series_id"], row["season_number"])
            known = row["last_notified_episode"]

            if aired <= known:
                continue

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

    log.info("Episode check done.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

_ENV_HINTS = {
    "BOT_TOKEN": "Railway → сервіс з bot.py → Variables → BOT_TOKEN.",
    "TMDB_API_TOKEN": "Railway → той самий сервіс → Variables → TMDB_API_TOKEN (Read Access Token v4).",
}

_DB_HINT = (
    "Додай PostgreSQL у проєкт. У сервісі бота: Variables → + New variable → Variable Reference → "
    "Postgres → обери **DATABASE_URL** (внутрішнє підключення, без egress). "
    "Якщо вже є лише DATABASE_PUBLIC_URL — бот теж підхопить його."
)


async def main():
    for var in ("BOT_TOKEN", "TMDB_API_TOKEN"):
        if not (os.getenv(var) or "").strip():
            raise RuntimeError(
                f"Відсутня змінна середовища: {var}. {_ENV_HINTS[var]}"
            )

    if not db.database_dsn():
        raise RuntimeError(f"Відсутній DATABASE_URL (або DATABASE_PUBLIC_URL). {_DB_HINT}")

    await db.init_db()
    log.info("Database initialized.")

    scheduler.add_job(
        check_new_episodes,
        trigger="interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="episode_check",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started (every %s min).", CHECK_INTERVAL_MINUTES)

    try:
        log.info("Starting bot polling…")
        await dp.start_polling(bot, skip_updates=True, close_bot_session=False)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_pool()
        await bot.session.close()
        log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())

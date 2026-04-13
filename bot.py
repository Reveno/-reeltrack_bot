"""
Series Tracker Bot
──────────────────
ReplyKeyboard menu + chat search + optional inline search + localized UI.

Requires env vars: BOT_TOKEN, TMDB_API_TOKEN, DATABASE_URL (or DATABASE_PUBLIC_URL)
Optional: CHECK_INTERVAL_MINUTES (default 15)
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import db
import tmdb

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

SUPPORTED_LANGS = ("uk", "en", "de")
DEFAULT_LANG = "uk"
LOCALES_DIR = Path(__file__).parent / "locales"


def load_locales() -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    for code in SUPPORTED_LANGS:
        # utf-8-sig tolerates BOM from Windows editors/PowerShell writes
        with (LOCALES_DIR / f"{code}.json").open("r", encoding="utf-8-sig") as f:
            data[code] = json.load(f)
    return data


T = load_locales()


def detect_lang(tg_code: str | None) -> str:
    code = (tg_code or "").split("-")[0].lower()
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def tr(lang: str, key: str, **kwargs) -> str:
    template = T.get(lang, T[DEFAULT_LANG]).get(key, T[DEFAULT_LANG].get(key, key))
    return template.replace("\\n", "\n").format(**kwargs)


def all_button_texts(key: str) -> set[str]:
    return {T[lang][key] for lang in SUPPORTED_LANGS}


_bot_token = (os.getenv("BOT_TOKEN") or "").strip()
if not _bot_token:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=_bot_token)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
CHECK_INTERVAL_MINUTES = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES") or "15"))
PLACEHOLDER_POSTER = "https://placehold.co/500x750/1a1a2e/ffffff?text=No+Poster"

BTN_SEARCH_SET = all_button_texts("btn_search")
BTN_LIST_SET = all_button_texts("btn_list")
BTN_HELP_SET = all_button_texts("btn_help")
BTN_LANG_SET = all_button_texts("btn_language")


class SearchStates(StatesGroup):
    waiting_query = State()


async def user_lang(user_id: int) -> str:
    lang = await db.get_user_language(user_id, default=DEFAULT_LANG)
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def main_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=tr(lang, "btn_search")), KeyboardButton(text=tr(lang, "btn_list")))
    builder.row(KeyboardButton(text=tr(lang, "btn_help")), KeyboardButton(text=tr(lang, "btn_language")))
    return builder.as_markup(resize_keyboard=True)


def kb_series_actions(series_id: int, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr(lang, "btn_track"), callback_data=f"track:{series_id}")
    builder.button(text=tr(lang, "btn_new_search"), callback_data="search:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_seasons(series_id: int, seasons: list[dict], lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in seasons:
        n = s.get("season_number", 0)
        if n == 0:
            continue
        ep = s.get("episode_count", "?")
        builder.button(text=f"Season {n} ({ep})", callback_data=f"season:{series_id}:{n}")
    builder.button(text=tr(lang, "btn_back"), callback_data=f"info:{series_id}")
    builder.adjust(2)
    return builder.as_markup()


def kb_my_list(items) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=f"🗑 {item['series_name']} — S{item['season_number']}", callback_data=f"remove:{item['id']}")
    builder.adjust(1)
    return builder.as_markup()


def kb_pick_search_result(results: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in results[:10]:
        sid = s["id"]
        name = s.get("name") or s.get("original_name") or "Unknown"
        year = (s.get("first_air_date") or "")[:4]
        label = f"{name}" + (f" ({year})" if year else "")
        builder.button(text=label[:60], callback_data=f"open:{sid}")
    builder.adjust(1)
    return builder.as_markup()


def kb_language_picker(current_lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lang in SUPPORTED_LANGS:
        mark = "✅ " if lang == current_lang else ""
        builder.button(text=f"{mark}{T[lang]['lang_name']}", callback_data=f"lang:set:{lang}")
    builder.adjust(1)
    return builder.as_markup()


async def send_watchlist_view(message: Message, lang: str):
    items = await db.get_watchlist(message.from_user.id)
    if not items:
        await message.answer(
            tr(lang, "watchlist_empty", btn_search=tr(lang, "btn_search")),
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(lang),
        )
        return

    lines = [tr(lang, "watchlist_header"), ""]
    for item in items:
        ep_info = tr(lang, "watchlist_episode_info", episode=item["last_notified_episode"])
        lines.append(
            tr(
                lang,
                "watchlist_row",
                series_name=item["series_name"],
                season_number=item["season_number"],
                ep_info=ep_info,
            )
        )
    lines.append("")
    lines.append(tr(lang, "watchlist_remove_hint"))
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_my_list(items))


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    tg_lang = detect_lang(message.from_user.language_code)
    await db.ensure_user_settings(message.from_user.id, tg_lang)
    lang = await user_lang(message.from_user.id)

    await message.answer(
        tr(
            lang,
            "start",
            btn_search=tr(lang, "btn_search"),
            btn_list=tr(lang, "btn_list"),
            btn_help=tr(lang, "btn_help"),
            btn_language=tr(lang, "btn_language"),
            minutes=CHECK_INTERVAL_MINUTES,
        ),
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(lang),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await message.answer(
        tr(
            lang,
            "help",
            btn_search=tr(lang, "btn_search"),
            btn_list=tr(lang, "btn_list"),
            btn_track=tr(lang, "btn_track"),
        ),
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(lang),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await message.answer(tr(lang, "cancelled"), reply_markup=main_reply_keyboard(lang))


@dp.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await send_watchlist_view(message, lang)


@dp.message(StateFilter(default_state), F.text)
async def handle_menu(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    lang = await user_lang(message.from_user.id)

    if text in BTN_SEARCH_SET:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        await state.set_state(SearchStates.waiting_query)
        await message.answer(tr(lang, "prompt_search"), parse_mode="HTML", reply_markup=main_reply_keyboard(lang))
        return

    if text in BTN_LIST_SET:
        await state.clear()
        await send_watchlist_view(message, lang)
        return

    if text in BTN_HELP_SET:
        await state.clear()
        await cmd_help(message, state)
        return

    if text in BTN_LANG_SET:
        await state.clear()
        await message.answer(tr(lang, "lang_choose"), reply_markup=kb_language_picker(lang))
        return

    if text.startswith("/"):
        await message.answer(tr(lang, "unknown_cmd"), reply_markup=main_reply_keyboard(lang))
        return

    await message.answer(tr(lang, "idle_hint", btn_search=tr(lang, "btn_search")), reply_markup=main_reply_keyboard(lang))


@dp.callback_query(F.data.startswith("lang:set:"))
async def cb_set_lang(call: CallbackQuery):
    lang = call.data.split(":")[-1]
    if lang not in SUPPORTED_LANGS:
        await call.answer()
        return
    await db.set_user_language(call.from_user.id, lang)
    await call.answer()
    await call.message.answer(
        tr(lang, "lang_changed", lang_name=T[lang]["lang_name"]),
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(lang),
    )


@dp.message(StateFilter(SearchStates.waiting_query), F.text)
async def process_search_query(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    if raw in BTN_LIST_SET or raw in BTN_HELP_SET or raw in BTN_LANG_SET:
        await state.clear()
        await handle_menu(message, state)
        return
    if len(raw) < 2:
        await message.answer(tr(lang, "search_too_short"))
        return

    await message.answer(tr(lang, "searching"))
    results = await tmdb.search_series(raw, language=lang)
    await state.clear()

    if not results:
        await message.answer(tr(lang, "search_empty"), reply_markup=main_reply_keyboard(lang))
        return

    await message.answer(tr(lang, "choose_series"), reply_markup=kb_pick_search_result(results))


@dp.callback_query(F.data.startswith("open:"))
async def cb_open_series(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id, language=lang)
    if data.get("success") is False:
        await call.message.answer(tr(lang, "series_not_found"))
        return

    caption = tmdb.format_series_info(data)
    poster = tmdb.poster_url(data.get("poster_path")) or PLACEHOLDER_POSTER
    await call.message.answer_photo(
        photo=poster,
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb_series_actions(series_id, lang),
    )


@dp.callback_query(F.data == "search:new")
async def cb_search_new(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    await call.answer()
    await state.set_state(SearchStates.waiting_query)
    await call.message.answer(tr(lang, "prompt_search"), parse_mode="HTML", reply_markup=main_reply_keyboard(lang))


@dp.inline_query()
async def handle_inline_search(query: InlineQuery):
    lang = detect_lang(query.from_user.language_code)
    text = query.query.strip()
    if len(text) < 2:
        await query.answer([], switch_pm_text=tr(lang, "inline_hint"), switch_pm_parameter="help", cache_time=1)
        return

    results = await tmdb.search_series(text, language=lang)
    items = []
    for s in results:
        sid = str(s["id"])
        poster = tmdb.poster_url(s.get("poster_path")) or PLACEHOLDER_POSTER
        name = s.get("name") or s.get("original_name") or "Unknown"
        year = (s.get("first_air_date") or "")[:4]
        overview = (s.get("overview") or "No description")[:200]
        caption = f"<b>{name}</b>" + (f" ({year})" if year else "") + f"\n\n{overview}"
        items.append(
            InlineQueryResultPhoto(
                id=sid,
                photo_url=poster,
                thumbnail_url=poster,
                title=name,
                description=f"{year} — {overview[:80]}",
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb_series_actions(int(sid), lang),
            )
        )
    await query.answer(items, cache_time=60, is_personal=False)


@dp.callback_query(F.data.startswith("info:"))
async def cb_series_info(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id, language=lang)
    if data.get("success") is False:
        await call.message.answer(tr(lang, "series_not_found"))
        return

    await call.message.edit_caption(
        caption=tmdb.format_series_info(data),
        parse_mode="HTML",
        reply_markup=kb_series_actions(series_id, lang),
    )


@dp.callback_query(F.data.startswith("track:"))
async def cb_track_series(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    series_id = int(call.data.split(":")[1])
    await call.answer()

    data = await tmdb.get_series(series_id, language=lang)
    if data.get("success") is False:
        await call.message.answer(tr(lang, "series_not_found"))
        return

    seasons = [s for s in data.get("seasons", []) if s.get("season_number", 0) > 0]
    if not seasons:
        await call.message.answer(tr(lang, "seasons_missing"))
        return

    await call.message.edit_caption(
        caption=tmdb.format_series_info(data) + "\n\n" + tr(lang, "choose_season"),
        parse_mode="HTML",
        reply_markup=kb_seasons(series_id, seasons, lang),
    )


@dp.callback_query(F.data.startswith("season:"))
async def cb_add_season(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    _, sid, season_s = call.data.split(":")
    series_id, season_number = int(sid), int(season_s)

    await call.answer(tr(lang, "searching"))
    await db.upsert_user(call.from_user.id, call.from_user.username, call.from_user.full_name)

    if await db.is_tracking(call.from_user.id, series_id, season_number):
        await call.message.answer(tr(lang, "already_tracking"), parse_mode="HTML")
        return

    series_data = await tmdb.get_series(series_id, language=lang)
    if series_data.get("success") is False:
        await call.message.answer(tr(lang, "series_not_found"))
        return

    aired = await tmdb.count_aired_episodes(series_id, season_number, language=lang)
    added = await db.add_to_watchlist(
        user_id=call.from_user.id,
        series_id=series_id,
        series_name=series_data.get("name") or series_data.get("original_name") or "Unknown",
        poster_path=series_data.get("poster_path"),
        season_number=season_number,
        total_seasons=series_data.get("number_of_seasons", 1),
        current_aired=aired,
    )

    if added:
        await call.message.answer(
            tr(
                lang,
                "added_to_watchlist",
                series_name=series_data.get("name") or series_data.get("original_name") or "Unknown",
                season_number=season_number,
                aired=aired,
                minutes=CHECK_INTERVAL_MINUTES,
            ),
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(lang),
        )
    else:
        await call.message.answer(tr(lang, "add_failed"), reply_markup=main_reply_keyboard(lang))


@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    watchlist_id = int(call.data.split(":")[1])
    await db.remove_from_watchlist(call.from_user.id, watchlist_id)
    await call.answer(tr(lang, "removed_short"))
    await call.message.delete()
    await call.message.answer(tr(lang, "removed"), reply_markup=main_reply_keyboard(lang))


def plural_key_uk(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "few"
    return "many"


async def check_new_episodes():
    log.info("Running episode check (interval %s min)…", CHECK_INTERVAL_MINUTES)
    tracked = await db.get_all_tracked()

    for row in tracked:
        try:
            lang = row.get("language") or DEFAULT_LANG
            aired = await tmdb.count_aired_episodes(row["series_id"], row["season_number"], language=lang)
            known = row["last_notified_episode"]
            if aired <= known:
                continue

            new_count = aired - known
            key = plural_key_uk(new_count) if lang == "uk" else ("one" if new_count == 1 else "many")
            title = tr(lang, f"notify_title_{key}")
            ep_word = tr(lang, f"ep_word_{key}")
            text = tr(
                lang,
                "notify_text",
                title=title,
                series_name=row["series_name"],
                season_number=row["season_number"],
                new_count=new_count,
                ep_word=ep_word,
                aired=aired,
            )

            series_data = await tmdb.get_series(row["series_id"], language=lang)
            poster = tmdb.poster_url(series_data.get("poster_path"))
            if poster:
                await bot.send_photo(chat_id=row["user_id"], photo=poster, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=row["user_id"], text=text, parse_mode="HTML")

            await db.update_notified_episode(row["id"], aired)
        except Exception as exc:
            log.error("Error while checking tracked series %s: %s", row.get("series_name"), exc)


_ENV_HINTS = {
    "BOT_TOKEN": "Railway service Variables -> BOT_TOKEN",
    "TMDB_API_TOKEN": "Railway service Variables -> TMDB_API_TOKEN",
}


async def main():
    for var in ("BOT_TOKEN", "TMDB_API_TOKEN"):
        if not (os.getenv(var) or "").strip():
            raise RuntimeError(f"Missing environment variable: {var}. {_ENV_HINTS[var]}")
    if not db.database_dsn():
        raise RuntimeError("Missing DATABASE_URL or DATABASE_PUBLIC_URL")

    await db.init_db()
    scheduler.add_job(
        check_new_episodes,
        trigger="interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="episode_check",
        replace_existing=True,
    )
    scheduler.start()

    try:
        await dp.start_polling(bot, skip_updates=True, close_bot_session=False)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_pool()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

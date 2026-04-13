"""
Reeltrack bot: series + movie tracking with localization.
"""

import asyncio
import html
import json
import logging
import os
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineQuery, InlineQueryResultPhoto, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import db
import tmdb

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

SUPPORTED_LANGS = ("uk", "en", "de", "pl", "es", "pt", "tr", "fr", "ar", "it")
DEFAULT_LANG = "uk"
LOCALES_DIR = Path(__file__).parent / "locales"
REGIONS = ("US", "GB", "DE", "PL", "UA", "FR", "ES", "IT", "TR")
REGIONS_SET = set(REGIONS)


def load_locales() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for code in SUPPORTED_LANGS:
        with (LOCALES_DIR / f"{code}.json").open("r", encoding="utf-8-sig") as f:
            out[code] = json.load(f)
    return out


T = load_locales()


def tr(lang: str, key: str, **kwargs) -> str:
    for d in (T.get(lang), T.get("en"), T.get(DEFAULT_LANG)):
        if d and key in d:
            tmpl = d[key]
            break
    else:
        tmpl = key
    return tmpl.replace("\\n", "\n").format(**kwargs)


def detect_lang(tg_code: str | None) -> str:
    code = (tg_code or "").split("-")[0].lower()
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def all_button_texts(key: str) -> set[str]:
    return {T[l][key] for l in SUPPORTED_LANGS if key in T[l]}


def resolve_language_code_from_button_text(text: str) -> str | None:
    t = (text or "").strip()
    if t.startswith("✅ "):
        t = t[3:].strip()
    for code in SUPPORTED_LANGS:
        if T[code]["lang_name"] == t:
            return code
    return None


_bot_token = (os.getenv("BOT_TOKEN") or "").strip()
if not _bot_token:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=_bot_token)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
CHECK_INTERVAL_MINUTES = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES") or "15"))
PLACEHOLDER_POSTER = "https://placehold.co/500x750/1a1a2e/ffffff?text=No+Poster"
TMDB_TITLE_CONCURRENCY = 4
_tmdb_title_sem = asyncio.Semaphore(TMDB_TITLE_CONCURRENCY)

BTN_SERIES_SET = all_button_texts("btn_search_series")
BTN_MOVIES_SET = all_button_texts("btn_search_movies")
BTN_LIST_SET = all_button_texts("btn_list")
BTN_HELP_SET = all_button_texts("btn_help")
BTN_LANG_SET = all_button_texts("btn_language")
BTN_CANCEL_SET = all_button_texts("btn_cancel")
BTN_WATCHLIST_SERIES_SET = all_button_texts("btn_watchlist_series")
BTN_WATCHLIST_MOVIES_SET = all_button_texts("btn_watchlist_movies")
BTN_BACK_TO_MENU_SET = all_button_texts("btn_back_to_menu")
BTN_WATCHLIST_CATEGORIES_SET = all_button_texts("btn_watchlist_categories")
BTN_TRACK_SET = all_button_texts("btn_track")
BTN_TRACK_MOVIE_SET = all_button_texts("btn_track_movie")
BTN_NEW_SEARCH_SET = all_button_texts("btn_new_search")


class BotStates(StatesGroup):
    waiting_query = State()
    watchlist_pick = State()
    watchlist_remove_pick = State()
    language_pick = State()
    search_results_pick = State()
    series_actions = State()
    movie_actions = State()
    season_pick = State()
    movie_region_pick = State()


async def user_lang(user_id: int) -> str:
    lang = await db.get_user_language(user_id, default=DEFAULT_LANG)
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=tr(lang, "btn_search_series")), KeyboardButton(text=tr(lang, "btn_search_movies")))
    b.row(KeyboardButton(text=tr(lang, "btn_list")), KeyboardButton(text=tr(lang, "btn_language")))
    b.row(KeyboardButton(text=tr(lang, "btn_help")))
    return b.as_markup(resize_keyboard=True)


def search_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    return b.as_markup(resize_keyboard=True)


def watchlist_pick_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(
        KeyboardButton(text=tr(lang, "btn_watchlist_series")),
        KeyboardButton(text=tr(lang, "btn_watchlist_movies")),
    )
    b.row(KeyboardButton(text=tr(lang, "btn_back_to_menu")))
    return b.as_markup(resize_keyboard=True)


def watchlist_remove_reply_keyboard(lang: str, n: int) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    nums = [str(i + 1) for i in range(n)]
    for i in range(0, len(nums), 5):
        b.row(*[KeyboardButton(text=x) for x in nums[i : i + 5]])
    b.row(KeyboardButton(text=tr(lang, "btn_watchlist_categories")))
    return b.as_markup(resize_keyboard=True)


def language_pick_reply_keyboard(lang: str, current_ui_lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(0, len(SUPPORTED_LANGS), 2):
        row = []
        for code in SUPPORTED_LANGS[i : i + 2]:
            mark = "✅ " if code == current_ui_lang else ""
            row.append(KeyboardButton(text=f"{mark}{T[code]['lang_name']}"))
        b.row(*row)
    b.row(KeyboardButton(text=tr(lang, "btn_back_to_menu")))
    return b.as_markup(resize_keyboard=True)


def search_pick_reply_keyboard(lang: str, n: int) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    nums = [str(i + 1) for i in range(n)]
    for i in range(0, len(nums), 5):
        b.row(*[KeyboardButton(text=x) for x in nums[i : i + 5]])
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    return b.as_markup(resize_keyboard=True)


def series_actions_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=tr(lang, "btn_track")), KeyboardButton(text=tr(lang, "btn_new_search")))
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    return b.as_markup(resize_keyboard=True)


def movie_actions_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=tr(lang, "btn_track_movie")), KeyboardButton(text=tr(lang, "btn_new_search")))
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    return b.as_markup(resize_keyboard=True)


def movie_regions_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(0, len(REGIONS), 3):
        b.row(*[KeyboardButton(text=r) for r in REGIONS[i : i + 3]])
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    return b.as_markup(resize_keyboard=True)


def kb_results(items: list[dict], lang: str, media: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item in items[:10]:
        title = item.get("name") or item.get("title") or item.get("original_name") or item.get("original_title") or tr(lang, "series_unknown")
        year = (item.get("first_air_date") or item.get("release_date") or "")[:4]
        label = f"{title}" + (f" ({year})" if year else "")
        b.button(text=label[:60], callback_data=f"open:{media}:{item['id']}")
    b.adjust(1)
    return b.as_markup()


def kb_series_actions(series_id: int, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr(lang, "btn_track"), callback_data=f"track_series:{series_id}")
    b.button(text=tr(lang, "btn_new_search"), callback_data="search:new:tv")
    b.adjust(1)
    return b.as_markup()


def kb_movie_actions(movie_id: int, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr(lang, "btn_track_movie"), callback_data=f"track_movie:{movie_id}")
    b.button(text=tr(lang, "btn_new_search"), callback_data="search:new:movie")
    b.adjust(1)
    return b.as_markup()


def kb_seasons(series_id: int, seasons: list[dict], lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in seasons:
        n = s.get("season_number", 0)
        if n == 0:
            continue
        b.button(text=tr(lang, "season_btn", n=n, ep=s.get("episode_count", "?")), callback_data=f"season:{series_id}:{n}")
    b.adjust(2)
    return b.as_markup()


def kb_movie_regions(movie_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for r in REGIONS:
        b.button(text=r, callback_data=f"movie_region:{movie_id}:{r}")
    b.adjust(3)
    return b.as_markup()


async def try_dispatch_main_menu(message: Message, state: FSMContext, text: str, lang: str) -> bool:
    if text in BTN_SERIES_SET:
        await state.clear()
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="tv")
        await message.answer(tr(lang, "prompt_search_series"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return True
    if text in BTN_MOVIES_SET:
        await state.clear()
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="movie")
        await message.answer(tr(lang, "prompt_search_movies"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return True
    if text in BTN_LIST_SET:
        await state.clear()
        await render_watchlist(message, lang, state)
        return True
    if text in BTN_HELP_SET:
        await state.clear()
        await cmd_help(message, state)
        return True
    if text in BTN_LANG_SET:
        await state.clear()
        await state.set_state(BotStates.language_pick)
        await message.answer(tr(lang, "lang_choose"), reply_markup=language_pick_reply_keyboard(lang, lang))
        return True
    return False


async def _tmdb_series_display_title(series_id: int, lang: str, fallback: str) -> str:
    async with _tmdb_title_sem:
        try:
            data = await tmdb.get_series(series_id, language=lang)
            if data.get("success") is False:
                return fallback
            return data.get("name") or data.get("original_name") or fallback
        except Exception:
            log.exception("tmdb series title %s", series_id)
            return fallback


async def _tmdb_movie_display_title(movie_id: int, lang: str, fallback: str) -> str:
    async with _tmdb_title_sem:
        try:
            data = await tmdb.get_movie(movie_id, language=lang)
            if data.get("success") is False:
                return fallback
            return data.get("title") or data.get("original_title") or fallback
        except Exception:
            log.exception("tmdb movie title %s", movie_id)
            return fallback


async def enrich_series_display_titles(items: list[dict], lang: str) -> list[dict]:
    if not items:
        return []
    titles = await asyncio.gather(*[_tmdb_series_display_title(i["series_id"], lang, i["series_name"]) for i in items])
    return [{**i, "display_title": t} for i, t in zip(items, titles)]


async def enrich_movie_display_titles(items: list[dict], lang: str) -> list[dict]:
    if not items:
        return []
    titles = await asyncio.gather(*[_tmdb_movie_display_title(i["movie_id"], lang, i["movie_title"]) for i in items])
    return [{**i, "display_title": t} for i, t in zip(items, titles)]


async def render_watchlist_series(message: Message, lang: str, user_id: int, state: FSMContext):
    series_items = await db.get_watchlist(user_id)
    if not series_items:
        await state.set_state(BotStates.watchlist_pick)
        await message.answer(tr(lang, "watchlist_empty_series"), parse_mode="HTML", reply_markup=watchlist_pick_reply_keyboard(lang))
        return
    series_enriched = await enrich_series_display_titles(series_items, lang)
    lines = [tr(lang, "watchlist_header"), "", tr(lang, "watchlist_remove_by_number"), ""]
    for idx, i in enumerate(series_enriched, 1):
        ep = tr(lang, "watchlist_episode_info", episode=i["last_notified_episode"])
        row = tr(
            lang,
            "watchlist_row",
            series_name=i["display_title"],
            season_number=i["season_number"],
            ep_info=ep,
        )
        lines.append(f"{idx}. {row}")
    n = len(series_enriched)
    await state.set_state(BotStates.watchlist_remove_pick)
    await state.update_data(wl_remove_kind="tv", wl_remove_ids=[i["id"] for i in series_enriched])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=watchlist_remove_reply_keyboard(lang, n))


async def render_watchlist_movies(message: Message, lang: str, user_id: int, state: FSMContext):
    movie_items = await db.get_movie_watchlist(user_id)
    if not movie_items:
        await state.set_state(BotStates.watchlist_pick)
        await message.answer(tr(lang, "watchlist_empty_movies"), parse_mode="HTML", reply_markup=watchlist_pick_reply_keyboard(lang))
        return
    movie_enriched = await enrich_movie_display_titles(movie_items, lang)
    lines = [tr(lang, "movie_watchlist_header"), "", tr(lang, "watchlist_remove_by_number_movies"), ""]
    for idx, i in enumerate(movie_enriched, 1):
        row = tr(lang, "movie_watchlist_row", movie_title=i["display_title"], region=i["region"])
        lines.append(f"{idx}. {row}")
    n = len(movie_enriched)
    await state.set_state(BotStates.watchlist_remove_pick)
    await state.update_data(wl_remove_kind="movie", wl_remove_ids=[i["id"] for i in movie_enriched])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=watchlist_remove_reply_keyboard(lang, n))


async def render_watchlist(message: Message, lang: str, state: FSMContext):
    user_id = message.from_user.id
    series_items = await db.get_watchlist(user_id)
    movie_items = await db.get_movie_watchlist(user_id)

    if not series_items and not movie_items:
        await state.clear()
        await message.answer(
            tr(
                lang,
                "watchlist_empty",
                btn_search_series=tr(lang, "btn_search_series"),
                btn_search_movies=tr(lang, "btn_search_movies"),
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard(lang),
        )
        return

    await state.set_state(BotStates.watchlist_pick)
    await message.answer(tr(lang, "watchlist_choose_kind"), reply_markup=watchlist_pick_reply_keyboard(lang))


async def open_media_detail(message: Message, state: FSMContext, lang: str, media: str, tmdb_id: int):
    await state.clear()
    if media == "tv":
        data = await tmdb.get_series(tmdb_id, language=lang)
        if data.get("success") is False:
            await message.answer(tr(lang, "series_not_found"), reply_markup=main_keyboard(lang))
            return
        caption = tmdb.format_series_info(data, language=lang, tr_func=tr)
        poster = tmdb.poster_url(data.get("poster_path")) or PLACEHOLDER_POSTER
        await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML")
        await state.set_state(BotStates.series_actions)
        await state.update_data(detail_series_id=tmdb_id)
        await message.answer(tr(lang, "pick_action_hint"), reply_markup=series_actions_reply_keyboard(lang))
        return

    data = await tmdb.get_movie(tmdb_id, language=lang)
    if data.get("success") is False:
        await message.answer(tr(lang, "movie_not_found"), reply_markup=main_keyboard(lang))
        return
    caption = tmdb.format_movie_info(data, language=lang, tr_func=tr)
    poster = tmdb.poster_url(data.get("poster_path")) or PLACEHOLDER_POSTER
    await message.answer_photo(photo=poster, caption=caption, parse_mode="HTML")
    await state.set_state(BotStates.movie_actions)
    await state.update_data(detail_movie_id=tmdb_id)
    await message.answer(tr(lang, "pick_action_hint"), reply_markup=movie_actions_reply_keyboard(lang))


async def prompt_season_choice_reply(message: Message, state: FSMContext, lang: str, series_id: int):
    data = await tmdb.get_series(series_id, language=lang)
    seasons = [s for s in data.get("seasons", []) if s.get("season_number", 0) > 0]
    if not seasons:
        await message.answer(tr(lang, "seasons_missing"), reply_markup=main_keyboard(lang))
        await state.clear()
        return
    label_to_num: dict[str, int] = {}
    b = ReplyKeyboardBuilder()
    row: list[KeyboardButton] = []
    for s in seasons:
        n = s.get("season_number", 0)
        label = tr(lang, "season_btn", n=n, ep=s.get("episode_count", "?"))
        label_to_num[label] = n
        row.append(KeyboardButton(text=label))
        if len(row) >= 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(KeyboardButton(text=tr(lang, "btn_cancel")))
    await state.set_state(BotStates.season_pick)
    await state.update_data(season_pick_series_id=series_id, season_label_to_num=label_to_num)
    await message.answer(tr(lang, "choose_season"), parse_mode="HTML", reply_markup=b.as_markup(resize_keyboard=True))


async def complete_season_track(message: Message, lang: str, user_id: int, username: str | None, full_name: str, series_id: int, season_number: int):
    await db.upsert_user(user_id, username, full_name)
    if await db.is_tracking(user_id, series_id, season_number):
        await message.answer(tr(lang, "already_tracking"), reply_markup=main_keyboard(lang))
        return
    series = await tmdb.get_series(series_id, language=lang)
    aired = await tmdb.count_aired_episodes(series_id, season_number, language=lang)
    added = await db.add_to_watchlist(
        user_id,
        series_id,
        series.get("name") or series.get("original_name") or tr(lang, "series_unknown"),
        series.get("poster_path"),
        season_number,
        series.get("number_of_seasons", 1),
        aired,
    )
    if added:
        await message.answer(
            tr(
                lang,
                "added_to_watchlist",
                series_name=series.get("name") or series.get("original_name") or tr(lang, "series_unknown"),
                season_number=season_number,
                aired=aired,
                minutes=CHECK_INTERVAL_MINUTES,
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard(lang),
        )
    else:
        await message.answer(tr(lang, "add_failed"), reply_markup=main_keyboard(lang))


async def complete_movie_track(message: Message, lang: str, user_id: int, username: str | None, full_name: str, movie_id: int, region: str):
    await db.upsert_user(user_id, username, full_name)
    if await db.is_tracking_movie(user_id, movie_id, region):
        await message.answer(tr(lang, "already_tracking_movie", region=region), reply_markup=main_keyboard(lang))
        return
    movie = await tmdb.get_movie(movie_id, language=lang)
    title = movie.get("title") or movie.get("original_title") or tr(lang, "movie_unknown")
    added = await db.add_movie_to_watchlist(user_id, movie_id, title, movie.get("poster_path"), region)
    if not added:
        await message.answer(tr(lang, "add_failed"), reply_markup=main_keyboard(lang))
        return
    release_date = await tmdb.get_movie_release_date(movie_id, region) or (movie.get("release_date") or "")[:10] or "—"
    await message.answer(
        tr(lang, "added_movie_watchlist", movie_title=title, region=region, release_date=release_date),
        parse_mode="HTML",
        reply_markup=main_keyboard(lang),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await db.ensure_user_settings(message.from_user.id, detect_lang(message.from_user.language_code))
    lang = await user_lang(message.from_user.id)
    await message.answer(
        tr(lang, "start", btn_search_series=tr(lang, "btn_search_series"), btn_search_movies=tr(lang, "btn_search_movies"), btn_list=tr(lang, "btn_list"), btn_help=tr(lang, "btn_help"), btn_language=tr(lang, "btn_language"), minutes=CHECK_INTERVAL_MINUTES),
        parse_mode="HTML",
        reply_markup=main_keyboard(lang),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await message.answer(
        tr(lang, "help", btn_search_series=tr(lang, "btn_search_series"), btn_search_movies=tr(lang, "btn_search_movies"), btn_track=tr(lang, "btn_track"), btn_track_movie=tr(lang, "btn_track_movie"), btn_list=tr(lang, "btn_list")),
        parse_mode="HTML",
        reply_markup=main_keyboard(lang),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))


@dp.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(message.from_user.id)
    await render_watchlist(message, lang, state)


@dp.message(StateFilter(BotStates.watchlist_pick), F.text)
async def handle_watchlist_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_BACK_TO_MENU_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    if text in BTN_WATCHLIST_SERIES_SET:
        await state.clear()
        await render_watchlist_series(message, lang, message.from_user.id, state)
        return
    if text in BTN_WATCHLIST_MOVIES_SET:
        await state.clear()
        await render_watchlist_movies(message, lang, message.from_user.id, state)
        return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=watchlist_pick_reply_keyboard(lang))


@dp.message(StateFilter(BotStates.watchlist_remove_pick), F.text)
async def handle_watchlist_remove_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    uid = message.from_user.id
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_WATCHLIST_CATEGORIES_SET:
        await state.set_state(BotStates.watchlist_pick)
        await message.answer(tr(lang, "watchlist_choose_kind"), reply_markup=watchlist_pick_reply_keyboard(lang))
        return
    data = await state.get_data()
    ids: list = data.get("wl_remove_ids") or []
    kind = data.get("wl_remove_kind", "tv")
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(ids):
            rid = ids[idx]
            if kind == "tv":
                await db.remove_from_watchlist(uid, rid)
            else:
                await db.remove_movie_from_watchlist(uid, rid)
            await message.answer(tr(lang, "removed_short"))
            if kind == "tv":
                await render_watchlist_series(message, lang, uid, state)
            else:
                await render_watchlist_movies(message, lang, uid, state)
            return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=watchlist_remove_reply_keyboard(lang, len(ids)))


@dp.message(StateFilter(BotStates.language_pick), F.text)
async def handle_language_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_BACK_TO_MENU_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    code = resolve_language_code_from_button_text(text)
    if code:
        await db.set_user_language(message.from_user.id, code)
        lang = await user_lang(message.from_user.id)
        await state.clear()
        await message.answer(tr(lang, "lang_changed", lang_name=T[lang]["lang_name"]), parse_mode="HTML", reply_markup=main_keyboard(lang))
        return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=language_pick_reply_keyboard(lang, lang))


@dp.message(StateFilter(BotStates.search_results_pick), F.text)
async def handle_search_results_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    data = await state.get_data()
    ids: list = data.get("search_pick_ids") or []
    media = data.get("search_pick_media", "tv")
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(ids):
            await open_media_detail(message, state, lang, media, ids[idx])
            return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=search_pick_reply_keyboard(lang, len(ids)))


@dp.message(StateFilter(BotStates.series_actions), F.text)
async def handle_series_actions(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    data = await state.get_data()
    series_id = data.get("detail_series_id")
    if text in BTN_TRACK_SET and series_id:
        await prompt_season_choice_reply(message, state, lang, int(series_id))
        return
    if text in BTN_NEW_SEARCH_SET:
        await state.clear()
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="tv")
        await message.answer(tr(lang, "prompt_search_series"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=series_actions_reply_keyboard(lang))


@dp.message(StateFilter(BotStates.movie_actions), F.text)
async def handle_movie_actions(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    data = await state.get_data()
    movie_id = data.get("detail_movie_id")
    if text in BTN_TRACK_MOVIE_SET and movie_id:
        await state.set_state(BotStates.movie_region_pick)
        await state.update_data(movie_region_movie_id=int(movie_id))
        await message.answer(tr(lang, "choose_region"), reply_markup=movie_regions_reply_keyboard(lang))
        return
    if text in BTN_NEW_SEARCH_SET:
        await state.clear()
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="movie")
        await message.answer(tr(lang, "prompt_search_movies"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=movie_actions_reply_keyboard(lang))


@dp.message(StateFilter(BotStates.season_pick), F.text)
async def handle_season_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    data = await state.get_data()
    mapping: dict = data.get("season_label_to_num") or {}
    series_id = data.get("season_pick_series_id")
    if text in mapping and series_id:
        await state.clear()
        u = message.from_user
        await complete_season_track(message, lang, u.id, u.username, u.full_name, int(series_id), int(mapping[text]))
        return
    await state.clear()
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=main_keyboard(lang))


@dp.message(StateFilter(BotStates.movie_region_pick), F.text)
async def handle_movie_region_pick(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if await try_dispatch_main_menu(message, state, text, lang):
        return
    if text in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    data = await state.get_data()
    movie_id = data.get("movie_region_movie_id")
    if text in REGIONS_SET and movie_id:
        await state.clear()
        u = message.from_user
        await complete_movie_track(message, lang, u.id, u.username, u.full_name, int(movie_id), text)
        return
    await message.answer(tr(lang, "unknown_cmd"), reply_markup=movie_regions_reply_keyboard(lang))


@dp.message(StateFilter(default_state), F.text)
async def handle_menu(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    lang = await user_lang(message.from_user.id)

    if text in BTN_SERIES_SET:
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="tv")
        await message.answer(tr(lang, "prompt_search_series"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return
    if text in BTN_MOVIES_SET:
        await state.set_state(BotStates.waiting_query)
        await state.update_data(media="movie")
        await message.answer(tr(lang, "prompt_search_movies"), parse_mode="HTML", reply_markup=search_keyboard(lang))
        return
    if text in BTN_LIST_SET:
        await state.clear()
        await render_watchlist(message, lang, state)
        return
    if text in BTN_HELP_SET:
        await state.clear()
        await cmd_help(message, state)
        return
    if text in BTN_LANG_SET:
        await state.clear()
        await state.set_state(BotStates.language_pick)
        await message.answer(tr(lang, "lang_choose"), reply_markup=language_pick_reply_keyboard(lang, lang))
        return

    if text.startswith("/"):
        await message.answer(tr(lang, "unknown_cmd"), reply_markup=main_keyboard(lang))
        return
    await message.answer(tr(lang, "idle_hint", btn_search_series=tr(lang, "btn_search_series"), btn_search_movies=tr(lang, "btn_search_movies")), reply_markup=main_keyboard(lang))


@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    if code in SUPPORTED_LANGS:
        await db.set_user_language(call.from_user.id, code)
    lang = await user_lang(call.from_user.id)
    await call.answer()
    await call.message.answer(tr(lang, "lang_changed", lang_name=T[lang]["lang_name"]), parse_mode="HTML", reply_markup=main_keyboard(lang))


@dp.message(StateFilter(BotStates.waiting_query), F.text)
async def process_search(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    raw = (message.text or "").strip()
    if raw in BTN_CANCEL_SET:
        await state.clear()
        await message.answer(tr(lang, "cancelled"), reply_markup=main_keyboard(lang))
        return
    if raw.startswith("/"):
        return
    if len(raw) < 2:
        await message.answer(tr(lang, "search_too_short"))
        return

    data = await state.get_data()
    media = data.get("media", "tv")
    await message.answer(tr(lang, "searching"))
    results = await (tmdb.search_series(raw, language=lang) if media == "tv" else tmdb.search_movies(raw, language=lang))
    if not results:
        await state.clear()
        await message.answer(tr(lang, "search_empty"), reply_markup=main_keyboard(lang))
        return
    n = min(len(results), 10)
    ids = [results[i]["id"] for i in range(n)]
    lines = [tr(lang, "choose_series" if media == "tv" else "choose_movie")]
    for i, item in enumerate(results[:n], 1):
        title = item.get("name") or item.get("title") or item.get("original_name") or item.get("original_title") or tr(lang, "series_unknown")
        year = (item.get("first_air_date") or item.get("release_date") or "")[:4]
        line = f"{i}. {html.escape(title)}" + (f" ({year})" if year else "")
        lines.append(line)
    await state.set_state(BotStates.search_results_pick)
    await state.update_data(search_pick_ids=ids, search_pick_media=media)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=search_pick_reply_keyboard(lang, n))


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    _, media, sid = call.data.split(":")
    tmdb_id = int(sid)
    await call.answer()

    if media == "tv":
        data = await tmdb.get_series(tmdb_id, language=lang)
        if data.get("success") is False:
            await call.message.answer(tr(lang, "series_not_found"))
            return
        caption = tmdb.format_series_info(data, language=lang, tr_func=tr)
        markup = kb_series_actions(tmdb_id, lang)
    else:
        data = await tmdb.get_movie(tmdb_id, language=lang)
        if data.get("success") is False:
            await call.message.answer(tr(lang, "movie_not_found"))
            return
        caption = tmdb.format_movie_info(data, language=lang, tr_func=tr)
        markup = kb_movie_actions(tmdb_id, lang)

    poster = tmdb.poster_url(data.get("poster_path")) or PLACEHOLDER_POSTER
    await call.message.answer_photo(photo=poster, caption=caption, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data == "search:new:tv")
async def cb_search_new_tv(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    await call.answer()
    await state.set_state(BotStates.waiting_query)
    await state.update_data(media="tv")
    await call.message.answer(tr(lang, "prompt_search_series"), parse_mode="HTML", reply_markup=search_keyboard(lang))


@dp.callback_query(F.data == "search:new:movie")
async def cb_search_new_movie(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    await call.answer()
    await state.set_state(BotStates.waiting_query)
    await state.update_data(media="movie")
    await call.message.answer(tr(lang, "prompt_search_movies"), parse_mode="HTML", reply_markup=search_keyboard(lang))


@dp.callback_query(F.data.startswith("track_series:"))
async def cb_track_series(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    series_id = int(call.data.split(":")[1])
    await call.answer()
    data = await tmdb.get_series(series_id, language=lang)
    seasons = [s for s in data.get("seasons", []) if s.get("season_number", 0) > 0]
    if not seasons:
        await call.message.answer(tr(lang, "seasons_missing"))
        return
    await call.message.edit_caption(caption=tmdb.format_series_info(data, language=lang, tr_func=tr) + "\n\n" + tr(lang, "choose_season"), parse_mode="HTML", reply_markup=kb_seasons(series_id, seasons, lang))


@dp.callback_query(F.data.startswith("season:"))
async def cb_add_season(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    _, sid, sn = call.data.split(":")
    series_id, season_number = int(sid), int(sn)
    await call.answer(tr(lang, "searching"))
    u = call.from_user
    await complete_season_track(call.message, lang, u.id, u.username, u.full_name, series_id, season_number)


@dp.callback_query(F.data.startswith("track_movie:"))
async def cb_track_movie(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    movie_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.answer(tr(lang, "choose_region"), reply_markup=kb_movie_regions(movie_id))


@dp.callback_query(F.data.startswith("movie_region:"))
async def cb_movie_region(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    _, mid, region = call.data.split(":")
    movie_id = int(mid)
    await call.answer()
    u = call.from_user
    await complete_movie_track(call.message, lang, u.id, u.username, u.full_name, movie_id, region)


@dp.callback_query(F.data.startswith("remove_series:"))
async def cb_remove_series(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    rid = int(call.data.split(":")[1])
    await db.remove_from_watchlist(call.from_user.id, rid)
    await call.answer(tr(lang, "removed_short"))
    await call.message.delete()
    await call.message.answer(tr(lang, "removed"), reply_markup=main_keyboard(lang))


@dp.callback_query(F.data.startswith("remove_movie:"))
async def cb_remove_movie(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    rid = int(call.data.split(":")[1])
    await db.remove_movie_from_watchlist(call.from_user.id, rid)
    await call.answer(tr(lang, "removed_short"))
    await call.message.delete()
    await call.message.answer(tr(lang, "removed_movie"), reply_markup=main_keyboard(lang))


@dp.inline_query()
async def inline_search(query: InlineQuery):
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
        name = s.get("name") or s.get("original_name") or tr(lang, "series_unknown")
        year = (s.get("first_air_date") or "")[:4]
        overview = (s.get("overview") or tr(lang, "series_no_description"))[:200]
        items.append(InlineQueryResultPhoto(id=sid, photo_url=poster, thumbnail_url=poster, title=name, description=f"{year} — {overview[:80]}", caption=f"<b>{name}</b>" + (f" ({year})" if year else "") + f"\n\n{overview}", parse_mode="HTML", reply_markup=kb_series_actions(int(sid), lang)))
    await query.answer(items, cache_time=60, is_personal=False)


def plural_key_uk(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "few"
    return "many"


async def check_updates():
    for row in await db.get_all_tracked():
        try:
            lang = row.get("language") or DEFAULT_LANG
            aired = await tmdb.count_aired_episodes(row["series_id"], row["season_number"], language=lang)
            known = row["last_notified_episode"]
            if aired <= known:
                continue
            new_count = aired - known
            key = plural_key_uk(new_count) if lang == "uk" else ("one" if new_count == 1 else "many")
            series_data = await tmdb.get_series(row["series_id"], language=lang)
            display_series = series_data.get("name") or series_data.get("original_name") or row["series_name"]
            text = tr(
                lang,
                "notify_text",
                title=tr(lang, f"notify_title_{key}"),
                series_name=display_series,
                season_number=row["season_number"],
                new_count=new_count,
                ep_word=tr(lang, f"ep_word_{key}"),
                aired=aired,
            )
            poster = tmdb.poster_url(series_data.get("poster_path"))
            if poster:
                await bot.send_photo(chat_id=row["user_id"], photo=poster, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=row["user_id"], text=text, parse_mode="HTML")
            await db.update_notified_episode(row["id"], aired)
        except Exception as exc:
            log.error("Series check error: %s", exc)

    today_iso = date.today().isoformat()
    for row in await db.get_all_tracked_movies():
        try:
            if row["released_notified"]:
                continue
            lang = row.get("language") or DEFAULT_LANG
            release_date = await tmdb.get_movie_release_date(row["movie_id"], row["region"])
            if not release_date:
                movie_data = await tmdb.get_movie(row["movie_id"], language=lang)
                release_date = (movie_data.get("release_date") or "")[:10]
            if not release_date or release_date > today_iso:
                continue
            movie_data = await tmdb.get_movie(row["movie_id"], language=lang)
            display_movie = movie_data.get("title") or movie_data.get("original_title") or row["movie_title"]
            text = tr(lang, "movie_released_notify", movie_title=display_movie, region=row["region"], release_date=release_date)
            poster = tmdb.poster_url(row.get("poster_path"))
            if poster:
                await bot.send_photo(chat_id=row["user_id"], photo=poster, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=row["user_id"], text=text, parse_mode="HTML")
            await db.mark_movie_released_notified(row["id"])
        except Exception as exc:
            log.error("Movie check error: %s", exc)


async def main():
    for var in ("BOT_TOKEN", "TMDB_API_TOKEN"):
        if not (os.getenv(var) or "").strip():
            raise RuntimeError(f"Missing environment variable: {var}")
    if not db.database_dsn():
        raise RuntimeError("Missing DATABASE_URL or DATABASE_PUBLIC_URL")

    await db.init_db()
    scheduler.add_job(check_updates, trigger="interval", minutes=CHECK_INTERVAL_MINUTES, id="updates_check", replace_existing=True)
    scheduler.start()

    try:
        await dp.start_polling(bot, skip_updates=True, close_bot_session=False)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_pool()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

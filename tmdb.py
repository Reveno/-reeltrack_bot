import os
from datetime import date
import aiohttp

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
LANGUAGE = "uk-UA"
LANG_MAP = {
    "uk": "uk-UA",
    "en": "en-US",
    "de": "de-DE",
    "pl": "pl-PL",
    "es": "es-ES",
    "pt": "pt-BR",
    "tr": "tr-TR",
    "fr": "fr-FR",
    "ar": "ar-SA",
    "it": "it-IT",
}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('TMDB_API_TOKEN')}",
        "accept": "application/json",
    }


def poster_url(path: str | None) -> str | None:
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


def resolve_tmdb_language(language: str | None = None) -> str:
    code = (language or "").split("-")[0].lower()
    return LANG_MAP.get(code, LANGUAGE)


async def search_series(query: str, language: str | None = None) -> list[dict]:
    """Search TV series by name. Returns up to 10 results."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/search/tv",
            headers=_headers(),
            params={"query": query, "language": resolve_tmdb_language(language), "page": 1},
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return data.get("results", [])[:10]


async def search_movies(query: str, language: str | None = None) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/search/movie",
            headers=_headers(),
            params={"query": query, "language": resolve_tmdb_language(language), "page": 1},
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return data.get("results", [])[:10]


async def get_series(series_id: int, language: str | None = None) -> dict:
    """Get full series details including seasons list."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/tv/{series_id}",
            headers=_headers(),
            params={"language": resolve_tmdb_language(language)},
        ) as r:
            data = await r.json()
            if r.status != 200:
                if isinstance(data, dict):
                    return {**data, "success": False}
                return {"success": False}
            return data


async def get_movie(movie_id: int, language: str | None = None) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/movie/{movie_id}",
            headers=_headers(),
            params={"language": resolve_tmdb_language(language)},
        ) as r:
            data = await r.json()
            if r.status != 200:
                if isinstance(data, dict):
                    return {**data, "success": False}
                return {"success": False}
            return data


async def get_season(series_id: int, season_number: int, language: str | None = None) -> dict:
    """Get season details with all episodes."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/tv/{series_id}/season/{season_number}",
            headers=_headers(),
            params={"language": resolve_tmdb_language(language)},
        ) as r:
            data = await r.json()
            if r.status != 200:
                if isinstance(data, dict):
                    return {**data, "success": False}
                return {"success": False}
            return data


async def count_aired_episodes(series_id: int, season_number: int, language: str | None = None) -> int:
    """Count episodes that have already aired (air_date <= today)."""
    season = await get_season(series_id, season_number, language=language)
    today = date.today().isoformat()
    episodes = season.get("episodes", [])
    return sum(
        1 for ep in episodes
        if ep.get("air_date") and ep["air_date"] <= today
    )


async def get_movie_release_date(movie_id: int, region: str = "US") -> str | None:
    """Best-effort region release date (ISO YYYY-MM-DD) from TMDB release_dates endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TMDB_BASE}/movie/{movie_id}/release_dates",
            headers=_headers(),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()

    target_region = (region or "US").upper()
    for entry in data.get("results", []):
        if entry.get("iso_3166_1") != target_region:
            continue
        dates = []
        for rel in entry.get("release_dates", []):
            d = (rel.get("release_date") or "")[:10]
            if d:
                dates.append(d)
        if dates:
            return min(dates)
    return None


def format_series_info(data: dict, language: str = "uk", tr_func=None) -> str:
    """Format series info as HTML caption."""
    t = tr_func or (lambda _lang, key, **kwargs: key.format(**kwargs))
    name = data.get("name", t(language, "series_unknown"))
    original = data.get("original_name", "")
    year = (data.get("first_air_date") or "")[:4]
    status_map = {
        "Returning Series": t(language, "series_status_returning"),
        "Ended": t(language, "series_status_ended"),
        "Canceled": t(language, "series_status_canceled"),
        "In Production": t(language, "series_status_production"),
    }
    status = status_map.get(data.get("status", ""), data.get("status", ""))
    raw_rating = data.get("vote_average")
    try:
        rating = float(raw_rating) if raw_rating is not None else 0.0
    except (TypeError, ValueError):
        rating = 0.0
    seasons = data.get("number_of_seasons", 0)
    episodes = data.get("number_of_episodes", 0)
    overview = data.get("overview") or t(language, "series_no_description")
    if len(overview) > 600:
        overview = overview[:600] + "…"

    genres = ", ".join(
        g.get("name", "") for g in data.get("genres", [])[:3] if g.get("name")
    )

    lines = [
        f"<b>{name}</b>",
        f"<i>{original}</i>" if original and original != name else "",
        t(language, "series_meta", year=year, rating=rating, status=status),
        f"🎭 {genres}" if genres else "",
        t(language, "series_counts", seasons=seasons, episodes=episodes),
        "",
        overview,
    ]
    return "\n".join(
        l for i, l in enumerate(lines)
        if l is not None and (l != "" or i > 2)
    )


def format_movie_info(data: dict, language: str = "uk", tr_func=None) -> str:
    t = tr_func or (lambda _lang, key, **kwargs: key.format(**kwargs))
    title = data.get("title") or data.get("original_title") or t(language, "movie_unknown")
    original = data.get("original_title", "")
    year = (data.get("release_date") or "")[:4]
    raw_rating = data.get("vote_average")
    try:
        rating = float(raw_rating) if raw_rating is not None else 0.0
    except (TypeError, ValueError):
        rating = 0.0
    status = data.get("status") or t(language, "movie_status_unknown")
    overview = data.get("overview") or t(language, "movie_no_description")
    if len(overview) > 600:
        overview = overview[:600] + "…"
    genres = ", ".join(g.get("name", "") for g in data.get("genres", [])[:3] if g.get("name"))

    lines = [
        f"<b>{title}</b>",
        f"<i>{original}</i>" if original and original != title else "",
        t(language, "movie_meta", year=year, rating=rating, status=status),
        f"🎭 {genres}" if genres else "",
        "",
        overview,
    ]
    return "\n".join(l for i, l in enumerate(lines) if l is not None and (l != "" or i > 2))

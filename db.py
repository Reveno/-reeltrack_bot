import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), min_size=1, max_size=5)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id                    SERIAL PRIMARY KEY,
                user_id               BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                series_id             INTEGER NOT NULL,
                series_name           TEXT    NOT NULL,
                poster_path           TEXT,
                season_number         INTEGER NOT NULL,
                total_seasons         INTEGER DEFAULT 1,
                last_notified_episode INTEGER DEFAULT 0,
                added_at              TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, series_id, season_number)
            );

            CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
        """)


# ── Users ────────────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str | None, full_name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
              SET username = EXCLUDED.username,
                  full_name = EXCLUDED.full_name
            """,
            user_id, username, full_name,
        )


# ── Watchlist ─────────────────────────────────────────────────────────────────

async def add_to_watchlist(
    user_id: int,
    series_id: int,
    series_name: str,
    poster_path: str | None,
    season_number: int,
    total_seasons: int,
    current_aired: int,
) -> bool:
    """Returns True if newly added, False if already exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            """
            INSERT INTO watchlist
                (user_id, series_id, series_name, poster_path, season_number,
                 total_seasons, last_notified_episode)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (user_id, series_id, season_number) DO NOTHING
            RETURNING id
            """,
            user_id, series_id, series_name, poster_path,
            season_number, total_seasons, current_aired,
        )
        return result is not None


async def remove_from_watchlist(user_id: int, watchlist_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM watchlist WHERE id = $1 AND user_id = $2",
            watchlist_id, user_id,
        )


async def get_watchlist(user_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, series_id, series_name, poster_path,
                   season_number, total_seasons, last_notified_episode
            FROM watchlist
            WHERE user_id = $1
            ORDER BY added_at DESC
            """,
            user_id,
        )


async def is_tracking(user_id: int, series_id: int, season_number: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM watchlist WHERE user_id=$1 AND series_id=$2 AND season_number=$3",
            user_id, series_id, season_number,
        ) is not None


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def get_all_tracked() -> list[asyncpg.Record]:
    """Get all unique (series_id, season_number) + all watching users."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, user_id, series_id, series_name, season_number, last_notified_episode
            FROM watchlist
            ORDER BY series_id, season_number
            """
        )


async def update_notified_episode(watchlist_id: int, episode_number: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE watchlist SET last_notified_episode = $1 WHERE id = $2",
            episode_number, watchlist_id,
        )

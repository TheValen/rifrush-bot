import os
import asyncpg
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ─────────────────────────────────────────────
# CONNECTION POOL
# ─────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool

    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            ssl="require",
            min_size=1,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=300,
        )
        logger.info("Database pool created")

    return _pool


# ─────────────────────────────────────────────
# INIT DB
# ─────────────────────────────────────────────

async def init_db():
    pool = await get_pool()

    async with pool.acquire() as conn:

        # USERS TABLE
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT DEFAULT '',
                plan        TEXT DEFAULT 'free',
                paid_until  TIMESTAMPTZ,
                threshold   INTEGER DEFAULT NULL,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        # Add threshold column if missing (for existing DBs)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS threshold INTEGER DEFAULT NULL
        """)

        # Fix paid_until column type to TIMESTAMPTZ
        await conn.execute("""
            ALTER TABLE users
            ALTER COLUMN paid_until TYPE TIMESTAMPTZ
            USING paid_until AT TIME ZONE 'UTC'
        """)

        # WALLETS TABLE — no FOREIGN KEY to avoid Supabase RLS issues
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                address     TEXT NOT NULL,
                chain       TEXT NOT NULL,
                label       TEXT DEFAULT '',
                last_tx     TEXT,
                added_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, address, chain)
            )
        """)

        # INDEXES
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wallets_user_id
            ON wallets(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wallets_chain
            ON wallets(chain)
        """)

        logger.info("Tables ready")


# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

async def get_user(user_id: int) -> dict | None:
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1",
            user_id
        )
        return dict(row) if row else None


async def upsert_user(user_id: int, username: str = ""):
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username
        """, user_id, username)


async def upgrade_user(user_id: int, plan: str, paid_until):
    """
    paid_until can be datetime object or ISO string — we handle both.
    """
    pool = await get_pool()

    # Normalize to UTC-aware datetime
    from datetime import datetime as dt, timezone
    if isinstance(paid_until, str):
        paid_until = dt.fromisoformat(paid_until)
    # Make timezone-aware if naive
    if paid_until.tzinfo is None:
        paid_until = paid_until.replace(tzinfo=timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET plan = $1, paid_until = $2
            WHERE user_id = $3
        """, plan, paid_until, user_id)


async def check_and_expire_plans():
    """
    Call this periodically to downgrade expired paid plans back to free.
    Run once per hour from monitor.py.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE users
            SET plan = 'free', paid_until = NULL
            WHERE plan != 'free'
              AND paid_until IS NOT NULL
              AND paid_until < NOW()
        """)
        # result looks like "UPDATE 2" — extract count
        count = int(result.split()[-1])
        if count > 0:
            logger.info(f"Expired {count} paid plan(s) → downgraded to free")


# ─────────────────────────────────────────────
# WALLETS
# ─────────────────────────────────────────────

PLAN_LIMITS = {
    "free":   3,
    "hunter": 25,
    "apex":   9999,
}


def normalize_address(address: str, chain: str) -> str:
    """EVM chains → lowercase. Solana → case-sensitive, keep as-is."""
    return address.lower() if chain in ("eth", "bsc", "base") else address


async def get_wallet_count(user_id: int) -> int:
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM wallets WHERE user_id = $1",
            user_id
        )
        return row["cnt"] if row else 0


async def add_wallet(user_id: int, address: str, chain: str, label: str = "") -> bool:
    pool = await get_pool()

    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO wallets (user_id, address, chain, label)
                VALUES ($1, $2, $3, $4)
            """, user_id, normalize_address(address, chain), chain, label)
        return True

    except asyncpg.UniqueViolationError:
        return False


async def remove_wallet(user_id: int, address: str, chain: str):
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM wallets
            WHERE user_id = $1 AND address = $2 AND chain = $3
        """, user_id, normalize_address(address, chain), chain)


async def remove_wallet_by_id(wallet_id: int, user_id: int):
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM wallets
            WHERE id = $1 AND user_id = $2
        """, wallet_id, user_id)


async def get_user_wallets(user_id: int) -> list[dict]:
    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM wallets
            WHERE user_id = $1
            ORDER BY added_at DESC
        """, user_id)
        return [dict(r) for r in rows]


async def get_all_wallets() -> list[dict]:
    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM wallets")
        return [dict(r) for r in rows]


async def update_last_tx(wallet_id: int, tx_hash: str):
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE wallets SET last_tx = $1 WHERE id = $2
        """, tx_hash, wallet_id)



async def get_user_threshold(user_id: int) -> int | None:
    """Get custom threshold for user. None = use plan default."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT threshold FROM users WHERE user_id = $1", user_id
        )
        return row["threshold"] if row else None


async def set_user_threshold(user_id: int, threshold: int):
    """Save custom threshold for user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET threshold = $1 WHERE user_id = $2",
            threshold, user_id
        )

async def get_stats() -> dict:
    pool = await get_pool()

    async with pool.acquire() as conn:
        users   = await conn.fetchval("SELECT COUNT(*) FROM users")
        wallets = await conn.fetchval("SELECT COUNT(*) FROM wallets")
        paid    = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE plan != 'free'"
        )
        return {"users": users, "wallets": wallets, "paid": paid}

```python id="db_full_fixed_v1"
import os
import asyncpg
import logging

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
                paid_until  TIMESTAMP,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        # WALLETS TABLE
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                address     TEXT NOT NULL,
                chain       TEXT NOT NULL,
                label       TEXT DEFAULT '',
                last_tx     TEXT,
                added_at    TIMESTAMP DEFAULT NOW(),

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE,

                UNIQUE(user_id, address, chain)
            )
        """)

        # INDEXES (performance)
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
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET plan = $1, paid_until = $2
            WHERE user_id = $3
        """, plan, paid_until, user_id)


# ─────────────────────────────────────────────
# WALLETS
# ─────────────────────────────────────────────

PLAN_LIMITS = {
    "free": 3,
    "hunter": 25,
    "apex": 9999
}


def normalize_address(address: str, chain: str) -> str:
    """
    IMPORTANT:
    EVM chains use lowercase, Solana must remain case-sensitive
    """
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
            UPDATE wallets
            SET last_tx = $1
            WHERE id = $2
        """, tx_hash, wallet_id)


async def get_stats() -> dict:
    pool = await get_pool()

    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        wallets = await conn.fetchval("SELECT COUNT(*) FROM wallets")
        paid = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE plan != 'free'"
        )

        return {
            "users": users,
            "wallets": wallets,
            "paid": paid
        }
```

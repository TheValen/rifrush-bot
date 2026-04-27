import aiosqlite

DB_PATH = "rifrush.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                plan        TEXT DEFAULT 'free',
                paid_until  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                address     TEXT,
                chain       TEXT,
                label       TEXT,
                last_tx     TEXT,
                added_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, address, chain)
            )
        """)
        await db.commit()

# ── USERS ──────────────────────────────────────────────

async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def upsert_user(user_id: int, username: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
        """, (user_id, username))
        await db.commit()

async def upgrade_user(user_id: int, plan: str, paid_until: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET plan = ?, paid_until = ? WHERE user_id = ?
        """, (plan, paid_until, user_id))
        await db.commit()

# ── WALLETS ────────────────────────────────────────────

PLAN_LIMITS = {"free": 3, "hunter": 25, "apex": 9999}

async def get_wallet_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM wallets WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def add_wallet(user_id: int, address: str, chain: str, label: str = "") -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO wallets (user_id, address, chain, label)
                VALUES (?, ?, ?, ?)
            """, (user_id, address.lower(), chain, label))
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # already exists

async def remove_wallet(user_id: int, address: str, chain: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM wallets WHERE user_id = ? AND address = ? AND chain = ?
        """, (user_id, address.lower(), chain))
        await db.commit()

async def get_user_wallets(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wallets WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_wallets() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wallets") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def update_last_tx(wallet_id: int, tx_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE wallets SET last_tx = ? WHERE id = ?", (tx_hash, wallet_id)
        )
        await db.commit()

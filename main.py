import asyncio
import logging
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from database import init_db
from handlers import router
from monitor import start_monitor

# ───────────────────────────────────────────────────────
# LOAD ENV
# ───────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env")


# ───────────────────────────────────────────────────────
# LOGGING
# ───────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────

async def main():

    logger.info("Initializing database...")

    try:
        await init_db()
        logger.info("Database initialized successfully")

    except Exception as e:
        logger.exception(f"Database initialization failed: {e}")
        return

    # Create bot

    bot = Bot(
        token=BOT_TOKEN,
        parse_mode="HTML"
    )

    # Dispatcher

    dp = Dispatcher(
        storage=MemoryStorage()
    )

    dp.include_router(router)

    # Start monitor task

    logger.info("Starting wallet monitor...")

    monitor_task = asyncio.create_task(
        start_monitor(bot)
    )

    logger.info("Wallet monitor started")

    # Start polling

    try:

        logger.info("Bot polling started")

        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query"
            ]
        )

    except Exception as e:

        logger.exception(f"Polling crashed: {e}")

    finally:

        logger.warning("Shutting down bot...")

        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()

        logger.info("Bot stopped cleanly")


# ───────────────────────────────────────────────────────
# ENTRYPOINT
# ───────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())

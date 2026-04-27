import asyncio
import logging
import os
import aiohttp
from aiogram import Bot

from database import get_all_wallets, update_last_tx
from utils import short_addr, CHAIN_EMOJI, CHAIN_LABELS

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")
HELIUS_KEY    = os.getenv("HELIUS_API_KEY", "")

# Check intervals per plan (seconds)
# Free users checked every 5 min, paid every 60s
# For simplicity we run one loop — paid users benefit from faster cycle
CHECK_INTERVAL = 60  # seconds between full scan cycles

logger = logging.getLogger(__name__)

# ── Chain API calls ────────────────────────────────────

async def get_latest_eth_tx(session: aiohttp.ClientSession, address: str) -> dict | None:
    """Get latest transaction for EVM address via Etherscan."""
    url = (
        f"https://api.etherscan.io/api"
        f"?module=account&action=txlist"
        f"&address={address}"
        f"&startblock=0&endblock=99999999"
        f"&page=1&offset=1&sort=desc"
        f"&apikey={ETHERSCAN_KEY}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("status") == "1" and data.get("result"):
                return data["result"][0]
    except Exception as e:
        logger.warning(f"Etherscan error for {address}: {e}")
    return None

async def get_latest_bsc_tx(session: aiohttp.ClientSession, address: str) -> dict | None:
    """BNB Chain via BSCScan (same API key as Etherscan)."""
    url = (
        f"https://api.bscscan.com/api"
        f"?module=account&action=txlist"
        f"&address={address}"
        f"&startblock=0&endblock=99999999"
        f"&page=1&offset=1&sort=desc"
        f"&apikey={ETHERSCAN_KEY}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("status") == "1" and data.get("result"):
                return data["result"][0]
    except Exception as e:
        logger.warning(f"BSCScan error for {address}: {e}")
    return None

async def get_latest_base_tx(session: aiohttp.ClientSession, address: str) -> dict | None:
    """Base chain via BaseScan."""
    url = (
        f"https://api.basescan.org/api"
        f"?module=account&action=txlist"
        f"&address={address}"
        f"&startblock=0&endblock=99999999"
        f"&page=1&offset=1&sort=desc"
        f"&apikey={ETHERSCAN_KEY}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("status") == "1" and data.get("result"):
                return data["result"][0]
    except Exception as e:
        logger.warning(f"BaseScan error for {address}: {e}")
    return None

async def get_latest_sol_tx(session: aiohttp.ClientSession, address: str) -> str | None:
    """Get latest Solana transaction signature via Helius."""
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={HELIUS_KEY}&limit=1"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if isinstance(data, list) and data:
                return data[0].get("signature")
    except Exception as e:
        logger.warning(f"Helius error for {address}: {e}")
    return None

# ── Alert formatter ────────────────────────────────────

def format_evm_alert(tx: dict, address: str, chain: str, label: str) -> str:
    emoji = CHAIN_EMOJI.get(chain, "🔗")
    chain_label = CHAIN_LABELS.get(chain, chain.upper())

    value_eth = int(tx.get("value", 0)) / 1e18
    is_incoming = tx.get("to", "").lower() == address.lower()
    direction = "📥 Incoming" if is_incoming else "📤 Outgoing"

    wallet_name = f"{label} · " if label else ""
    addr_short  = short_addr(address)

    explorer_links = {
        "eth":  f"https://etherscan.io/tx/{tx['hash']}",
        "bsc":  f"https://bscscan.com/tx/{tx['hash']}",
        "base": f"https://basescan.org/tx/{tx['hash']}",
    }
    link = explorer_links.get(chain, "#")

    return (
        f"{emoji} <b>{direction} · {chain_label}</b>\n\n"
        f"👛 {wallet_name}<code>{addr_short}</code>\n"
        f"💸 <b>{value_eth:.4f} {'ETH' if chain == 'eth' else 'BNB' if chain == 'bsc' else 'ETH'}</b>\n"
        f"{'From' if is_incoming else 'To'}: <code>{short_addr(tx.get('from' if is_incoming else 'to', ''))}</code>\n\n"
        f"<a href='{link}'>🔍 View on Explorer</a>"
    )

def format_sol_alert(signature: str, address: str, label: str) -> str:
    wallet_name = f"{label} · " if label else ""
    addr_short  = short_addr(address)
    link = f"https://solscan.io/tx/{signature}"

    return (
        f"◎ <b>Solana Transaction Detected</b>\n\n"
        f"👛 {wallet_name}<code>{addr_short}</code>\n"
        f"Signature: <code>{short_addr(signature)}</code>\n\n"
        f"<a href='{link}'>🔍 View on Solscan</a>"
    )

# ── Main monitor loop ──────────────────────────────────

async def start_monitor(bot: Bot):
    """Background task — checks all wallets every CHECK_INTERVAL seconds."""
    logger.info("Monitor loop started")
    await asyncio.sleep(5)  # Let bot finish startup

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                wallets = await get_all_wallets()
                logger.info(f"Checking {len(wallets)} wallets...")

                for wallet in wallets:
                    try:
                        await check_wallet(bot, session, wallet)
                        await asyncio.sleep(0.3)  # Respect API rate limits
                    except Exception as e:
                        logger.error(f"Error checking wallet {wallet['address']}: {e}")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

async def check_wallet(bot: Bot, session: aiohttp.ClientSession, wallet: dict):
    address = wallet["address"]
    chain   = wallet["chain"]
    user_id = wallet["user_id"]
    label   = wallet.get("label", "")
    last_tx = wallet.get("last_tx")
    w_id    = wallet["id"]

    new_tx_hash = None
    alert_text  = None

    if chain == "sol":
        sig = await get_latest_sol_tx(session, address)
        if sig and sig != last_tx:
            new_tx_hash = sig
            alert_text  = format_sol_alert(sig, address, label)

    elif chain in ("eth", "bsc", "base"):
        fetchers = {"eth": get_latest_eth_tx, "bsc": get_latest_bsc_tx, "base": get_latest_base_tx}
        tx = await fetchers[chain](session, address)
        if tx:
            tx_hash = tx.get("hash")
            if tx_hash and tx_hash != last_tx:
                new_tx_hash = tx_hash
                alert_text  = format_evm_alert(tx, address, chain, label)

    if new_tx_hash and alert_text:
        try:
            await bot.send_message(
                user_id, alert_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await update_last_tx(w_id, new_tx_hash)
            logger.info(f"Alert sent to {user_id} for {short_addr(address)} on {chain}")
        except Exception as e:
            logger.error(f"Failed to send alert to {user_id}: {e}")

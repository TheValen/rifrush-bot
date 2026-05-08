import asyncio
import logging
import os
import aiohttp
from aiogram import Bot

from database import get_all_wallets, update_last_tx, check_and_expire_plans
from utils import short_addr, CHAIN_EMOJI, CHAIN_LABELS

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")
HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")

CHECK_INTERVAL = 60

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────
# API HELPERS
# ───────────────────────────────────────────────────────

async def fetch_json(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:

            text = await r.text()

            # Sometimes explorer APIs return HTML errors
            if text.startswith("<"):
                logger.warning(f"Explorer returned HTML instead of JSON: {text[:120]}")
                return None

            try:
                return await r.json()
            except Exception:
                logger.warning(f"Failed parsing JSON: {text[:300]}")
                return None

    except Exception as e:
        logger.warning(f"HTTP request failed: {e}")
        return None


# ───────────────────────────────────────────────────────
# EVM FETCHERS
# ───────────────────────────────────────────────────────

async def get_latest_evm_tx(
    session: aiohttp.ClientSession,
    address: str,
    chain: str
):
    """
    Fetch latest tx from EVM explorer.
    First checks token transfers (ERC20),
    then falls back to normal native txs.
    """

    # Etherscan V2 API — single endpoint with chainid
    chain_ids = {
        "eth":  "1",
        "bsc":  "56",
        "base": "8453",
    }

    chain_id = chain_ids.get(chain)

    if not chain_id:
        return None

    base_url = f"https://api.etherscan.io/v2/api?chainid={chain_id}"

    # ── 1. TOKEN TX (USDT/USDC/ERC20/etc) ──

    token_url = (
        f"{base_url}"
        f"&module=account"
        f"&action=tokentx"
        f"&address={address}"
        f"&page=1"
        f"&offset=1"
        f"&sort=desc"
        f"&apikey={ETHERSCAN_KEY}"
    )

    data = await fetch_json(session, token_url)

    if data:
        logger.info(f"{chain.upper()} token tx response for {short_addr(address)}: {data}")

        if data.get("status") == "1" and data.get("result"):
            tx = data["result"][0]
            tx["_tx_type"] = "token"
            return tx

        else:
            logger.warning(
                f"{chain.upper()} TOKEN API issue | "
                f"status={data.get('status')} | "
                f"message={data.get('message')} | "
                f"result={str(data.get('result'))[:200]}"
            )

    # ── 2. FALLBACK TO NORMAL TX ──

    normal_url = (
        f"{base_url}"
        f"&module=account"
        f"&action=txlist"
        f"&address={address}"
        f"&startblock=0"
        f"&endblock=99999999"
        f"&page=1"
        f"&offset=1"
        f"&sort=desc"
        f"&apikey={ETHERSCAN_KEY}"
    )

    data = await fetch_json(session, normal_url)

    if data:
        logger.info(f"{chain.upper()} normal tx response for {short_addr(address)}: {data}")

        if data.get("status") == "1" and data.get("result"):
            tx = data["result"][0]
            tx["_tx_type"] = "native"
            return tx

        else:
            logger.warning(
                f"{chain.upper()} NORMAL API issue | "
                f"status={data.get('status')} | "
                f"message={data.get('message')} | "
                f"result={str(data.get('result'))[:200]}"
            )

    return None


# ───────────────────────────────────────────────────────
# SOLANA FETCHER
# ───────────────────────────────────────────────────────

async def get_latest_sol_tx(
    session: aiohttp.ClientSession,
    address: str
):
    url = (
        f"https://api.helius.xyz/v0/addresses/"
        f"{address}/transactions"
        f"?api-key={HELIUS_KEY}"
        f"&limit=1"
    )

    data = await fetch_json(session, url)

    if isinstance(data, list) and data:
        return data[0].get("signature")

    logger.warning(f"SOL no tx found for {short_addr(address)}")
    return None


# ───────────────────────────────────────────────────────
# ALERT FORMATTERS
# ───────────────────────────────────────────────────────

def format_evm_alert(tx, address, chain, label):
    emoji = CHAIN_EMOJI.get(chain, "🔗")
    chain_label = CHAIN_LABELS.get(chain, chain.upper())

    wallet_name = f"{label} · " if label else ""
    addr_short = short_addr(address)

    tx_hash = tx.get("hash")

    explorer_links = {
        "eth": f"https://etherscan.io/tx/{tx_hash}",
        "bsc": f"https://bscscan.com/tx/{tx_hash}",
        "base": f"https://basescan.org/tx/{tx_hash}",
    }

    link = explorer_links.get(chain, "#")

    tx_type = tx.get("_tx_type", "native")

    if tx_type == "token":

        token_symbol = tx.get("tokenSymbol", "TOKEN")

        try:
            decimals = int(tx.get("tokenDecimal", 18))
            value = int(tx.get("value", 0)) / (10 ** decimals)
        except Exception:
            value = 0

        is_incoming = tx.get("to", "").lower() == address.lower()

        direction = "📥 Incoming" if is_incoming else "📤 Outgoing"

        return (
            f"{emoji} <b>{direction} · {chain_label}</b>\n\n"
            f"👛 {wallet_name}<code>{addr_short}</code>\n"
            f"🪙 <b>{value:.4f} {token_symbol}</b>\n"
            f"{'From' if is_incoming else 'To'}: "
            f"<code>{short_addr(tx.get('from' if is_incoming else 'to', ''))}</code>\n\n"
            f"<a href='{link}'>🔍 View on Explorer</a>"
        )

    # Native tx

    try:
        value_eth = int(tx.get("value", 0)) / 1e18
    except Exception:
        value_eth = 0

    is_incoming = tx.get("to", "").lower() == address.lower()

    direction = "📥 Incoming" if is_incoming else "📤 Outgoing"

    native_symbol = "ETH" if chain in ("eth", "base") else "BNB"

    return (
        f"{emoji} <b>{direction} · {chain_label}</b>\n\n"
        f"👛 {wallet_name}<code>{addr_short}</code>\n"
        f"💸 <b>{value_eth:.4f} {native_symbol}</b>\n"
        f"{'From' if is_incoming else 'To'}: "
        f"<code>{short_addr(tx.get('from' if is_incoming else 'to', ''))}</code>\n\n"
        f"<a href='{link}'>🔍 View on Explorer</a>"
    )


def format_sol_alert(signature, address, label):
    wallet_name = f"{label} · " if label else ""
    addr_short = short_addr(address)

    return (
        f"◎ <b>Solana Transaction Detected</b>\n\n"
        f"👛 {wallet_name}<code>{addr_short}</code>\n"
        f"Signature: <code>{short_addr(signature)}</code>\n\n"
        f"<a href='https://solscan.io/tx/{signature}'>🔍 View on Solscan</a>"
    )


# ───────────────────────────────────────────────────────
# MAIN LOOP
# ───────────────────────────────────────────────────────

async def start_monitor(bot: Bot):
    logger.info("Monitor loop started")

    await asyncio.sleep(5)

    iteration = 0

    async with aiohttp.ClientSession() as session:

        while True:

            iteration += 1
            if iteration % 60 == 0:
                try:
                    await check_and_expire_plans()
                except Exception as e:
                    logger.error(f'expire plans error: {e}')


            try:
                wallets = await get_all_wallets()

                logger.info(f"Checking {len(wallets)} wallets...")

                for wallet in wallets:

                    try:
                        await check_wallet(bot, session, wallet)

                        await asyncio.sleep(0.5)

                    except Exception as e:
                        logger.error(
                            f"Wallet check error "
                            f"{wallet.get('address')}: {e}"
                        )

            except Exception as e:
                logger.error(f"Monitor loop fatal error: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


# ───────────────────────────────────────────────────────
# WALLET CHECKER
# ───────────────────────────────────────────────────────

async def check_wallet(
    bot: Bot,
    session: aiohttp.ClientSession,
    wallet: dict
):
    address = wallet["address"]
    chain = wallet["chain"]
    user_id = wallet["user_id"]
    label = wallet.get("label", "")

    last_tx = wallet.get("last_tx")
    wallet_id = wallet["id"]

    new_tx_hash = None
    alert_text = None

    # ── SOLANA ──

    if chain == "sol":

        signature = await get_latest_sol_tx(session, address)

        logger.info(
            f"SOL {short_addr(address)} | "
            f"latest={short_addr(signature) if signature else None} | "
            f"last={short_addr(last_tx) if last_tx else None}"
        )

        if signature and signature != last_tx:
            new_tx_hash = signature
            alert_text = format_sol_alert(
                signature,
                address,
                label
            )

    # ── EVM ──

    elif chain in ("eth", "bsc", "base"):

        tx = await get_latest_evm_tx(
            session,
            address,
            chain
        )

        if tx:

            tx_hash = tx.get("hash")

            logger.info(
                f"EVM {short_addr(address)} [{chain}] | "
                f"latest={short_addr(tx_hash)} | "
                f"last={short_addr(last_tx) if last_tx else None}"
            )

            if tx_hash and tx_hash != last_tx:

                new_tx_hash = tx_hash

                alert_text = format_evm_alert(
                    tx,
                    address,
                    chain,
                    label
                )

        else:
            logger.warning(
                f"EVM {short_addr(address)} [{chain}] "
                f"NO TX RETURNED"
            )

    # ── SEND ALERT ──

    if new_tx_hash and alert_text:

        try:

            await bot.send_message(
                chat_id=user_id,
                text=alert_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )

            await update_last_tx(
                wallet_id,
                new_tx_hash
            )

            logger.info(
                f"Alert sent to {user_id} "
                f"for {short_addr(address)}"
            )

        except Exception as e:
            logger.error(
                f"Failed sending alert "
                f"to {user_id}: {e}"
            )

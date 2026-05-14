import asyncio
import logging
import os
import aiohttp
from aiogram import Bot

from database import get_all_wallets, update_last_tx, check_and_expire_plans, get_user, get_user_threshold, get_expiring_users
from utils import short_addr, CHAIN_EMOJI, CHAIN_LABELS

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")
HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")

CHECK_INTERVAL = 60

# ── PLAN THRESHOLDS ─────────────────────────────────────
# Minimum USD value to trigger alert per plan

PLAN_THRESHOLDS = {
    "free":   500,   # $500 minimum — no spam
    "hunter": 100,   # $100 minimum — configurable later
    "apex":   1,     # $1 minimum — everything
}

# ETH/BNB price estimates (update periodically or use API later)
ETH_PRICE_USD = 3000
BNB_PRICE_USD = 600

# Known spam/airdrop token patterns — ignore these completely
SPAM_TOKEN_PATTERNS = {
    "symbols": {
        "ZACH", "CAT", "ROYAL", "CLAIM", "VISIT", "FREE",
        "AIRDROP", "REWARD", "BONUS", "GIFT", "WIN",
    },
    "name_keywords": [
        "airdrop", "claim", "visit", "reward", "bonus",
        "free", "win", "prize", "lottery", "giveaway",
    ],
    # Zero address as sender = minted from nowhere = spam airdrop
    "zero_sender": "0x0000000000000000000000000000000000000000",
}

def is_spam_token(tx: dict) -> bool:
    """Detect spam/airdrop tokens that should never trigger alerts."""
    if tx.get("_tx_type") != "token":
        return False

    symbol = tx.get("tokenSymbol", "").upper()
    name   = tx.get("tokenName",   "").lower()
    sender = tx.get("from", "").lower()

    # Minted from zero address = airdrop spam
    if sender == SPAM_TOKEN_PATTERNS["zero_sender"]:
        return True

    # Known spam symbols
    if symbol in SPAM_TOKEN_PATTERNS["symbols"]:
        return True

    # Spam keywords in token name
    for kw in SPAM_TOKEN_PATTERNS["name_keywords"]:
        if kw in name:
            return True

    return False

def get_tx_usd_value(tx: dict) -> float:
    """
    Estimate USD value of transaction.
    Returns float — compare against threshold before sending alert.

    Logic:
    - Stablecoins: exact value
    - Native ETH/BNB: value * price estimate
    - Unknown ERC20 tokens: 999999 (always pass — we can't know price)
    - Zero-value txs (approve, contract calls): return 0 — these are spam
    """
    try:
        tx_type = tx.get("_tx_type", "native")

        if tx_type == "token":
            symbol   = tx.get("tokenSymbol", "").upper()
            decimals = int(tx.get("tokenDecimal", 18))
            raw      = int(tx.get("value", 0))
            value    = raw / (10 ** decimals)

            # Zero value token tx — skip
            if raw == 0:
                return 0.0

            # Stablecoins: 1:1 USD
            if symbol in ("USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX", "USDD"):
                return value

            # WETH / WBTC approximations
            if symbol in ("WETH", "ETH"):
                return value * ETH_PRICE_USD
            if symbol in ("WBTC", "BTC"):
                return value * 60000

            # Unknown token — let through (we can't know price)
            return 999999.0

        else:
            # Native transaction
            raw_wei   = int(tx.get("value", 0))
            value_eth = raw_wei / 1e18

            # Zero value — contract call, approve, etc. — SKIP (spam source)
            if raw_wei == 0:
                return 0.0

            chain = tx.get("_chain", "eth")
            price = BNB_PRICE_USD if chain == "bsc" else ETH_PRICE_USD
            return value_eth * price

    except Exception:
        return 999999.0  # Unknown — let through



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
# RENEWAL REMINDER
# ───────────────────────────────────────────────────────

async def send_renewal_reminders(bot: Bot):
    """
    Send renewal reminder to users whose subscription
    expires in 1, 2 or 3 days. Runs once per day.
    """
    expiring = await get_expiring_users(days=3)

    if not expiring:
        return

    logger.info(f"Sending renewal reminders to {len(expiring)} users...")

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    for user in expiring:
        user_id    = user["user_id"]
        plan       = user["plan"].upper()
        paid_until = user["paid_until"]

        # Calculate days left
        from datetime import datetime, timezone
        now       = datetime.now(timezone.utc)
        delta     = paid_until - now
        days_left = max(0, delta.days)

        if days_left == 0:
            urgency = "⚠️ Your subscription expires <b>today</b>!"
        elif days_left == 1:
            urgency = "⏰ Your subscription expires <b>tomorrow</b>."
        else:
            urgency = f"📅 Your subscription expires in <b>{days_left} days</b>."

        text = (
            f"🔔 <b>Rifrush — Subscription Reminder</b>\n\n"
            f"{urgency}\n\n"
            f"Plan: <b>{plan}</b>\n"
            f"Expires: <b>{paid_until.strftime('%B %d, %Y')}</b>\n\n"
            f"Renew now to keep receiving whale alerts without interruption.\n\n"
            f"Pay in USDT, TON, BTC or ETH — no credit card needed."
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Renew {plan} Plan",
                callback_data="upgrade"
            )]
        ])

        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb
            )
            logger.info(f"Renewal reminder sent to {user_id} ({days_left} days left)")
        except Exception as e:
            logger.warning(f"Failed to send reminder to {user_id}: {e}")

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

            # Every hour — expire old plans
            if iteration % 60 == 0:
                try:
                    await check_and_expire_plans()
                except Exception as e:
                    logger.error(f'expire plans error: {e}')

            # Every 24 hours — send renewal reminders
            if iteration % 1440 == 0:
                try:
                    await send_renewal_reminders(bot)
                except Exception as e:
                    logger.error(f'renewal reminder error: {e}')


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
    address  = wallet["address"]
    chain    = wallet["chain"]
    user_id  = wallet["user_id"]
    label    = wallet.get("label", "")
    last_tx  = wallet.get("last_tx")
    wallet_id = wallet["id"]

    new_tx_hash = None
    alert_text  = None
    tx_for_threshold = None  # Store tx to check value

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
            alert_text  = format_sol_alert(signature, address, label)
            # SOL: no easy USD value, always send

    # ── EVM ──

    elif chain in ("eth", "bsc", "base"):

        tx = await get_latest_evm_tx(session, address, chain)

        if tx:
            tx_hash = tx.get("hash")

            logger.info(
                f"EVM {short_addr(address)} [{chain}] | "
                f"latest={short_addr(tx_hash)} | "
                f"last={short_addr(last_tx) if last_tx else None}"
            )

            if tx_hash and tx_hash != last_tx:
                new_tx_hash      = tx_hash
                tx["_chain"]     = chain  # Store chain for USD estimation
                alert_text       = format_evm_alert(tx, address, chain, label)
                tx_for_threshold = tx

        else:
            logger.warning(f"EVM {short_addr(address)} [{chain}] NO TX RETURNED")

    # ── SPAM CHECK ──

    if new_tx_hash and alert_text and tx_for_threshold is not None:
        if is_spam_token(tx_for_threshold):
            logger.info(
                f"Spam token skipped: {tx_for_threshold.get('tokenSymbol')} "
                f"({tx_for_threshold.get('tokenName')}) for {short_addr(address)}"
            )
            await update_last_tx(wallet_id, new_tx_hash)
            return

    # ── THRESHOLD CHECK ──

    if new_tx_hash and alert_text:

        # Get user plan and threshold
        try:
            user           = await get_user(user_id)
            plan           = user["plan"] if user else "free"
            plan_default   = PLAN_THRESHOLDS.get(plan, 500)
            # Use custom threshold if set, otherwise plan default
            user_threshold = await get_user_threshold(user_id)
            # Enforce plan minimum: Free min $500, Hunter min $100, Apex min $1
            plan_min  = {"free": 500, "hunter": 100, "apex": 1}.get(plan, 500)
            if user_threshold is not None:
                threshold = max(user_threshold, plan_min)
            else:
                threshold = plan_default
        except Exception:
            plan      = "free"
            threshold = 500

        # Check EVM transaction value against threshold
        if tx_for_threshold is not None:
            usd_value = get_tx_usd_value(tx_for_threshold)
            if usd_value < threshold:
                logger.info(
                    f"Skipped alert: ${usd_value:.0f} < threshold ${threshold} "
                    f"(plan={plan}, {short_addr(address)})"
                )
                # Still update last_tx so we don't recheck same tx
                await update_last_tx(wallet_id, new_tx_hash)
                return

    # ── SEND ALERT ──

    if new_tx_hash and alert_text:

        try:
            await bot.send_message(
                chat_id=user_id,
                text=alert_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )

            await update_last_tx(wallet_id, new_tx_hash)

            logger.info(f"Alert sent to {user_id} for {short_addr(address)}")

        except Exception as e:
            logger.error(f"Failed sending alert to {user_id}: {e}")

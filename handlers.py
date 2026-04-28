import logging
import os
import re
import aiohttp

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import (
    upsert_user, get_user, get_user_wallets,
    add_wallet, remove_wallet, get_wallet_count,
    upgrade_user, PLAN_LIMITS
)

router = Router()

# ── Constants ───────────────────────────────────────────

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))

CHAIN_EMOJI  = {"eth": "⟠", "bsc": "⬡", "base": "🔵", "sol": "◎"}
CHAIN_LABELS = {"eth": "Ethereum", "bsc": "BNB Chain", "base": "Base", "sol": "Solana"}

PLAN_PRICES = {"hunter": 19, "apex": 49}
PLAN_NAMES  = {"hunter": "🎯 HUNTER", "apex": "⚡ APEX"}

# ── Whale Watchlist ─────────────────────────────────────
# callback_data = "tw:INDEX" — stays well under Telegram's 64-byte limit

WHALES = [
    # ── Ethereum ──────────────────────────────────────────
    ("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "eth", "Vitalik Buterin"),
    ("0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8", "eth", "Binance Cold Wallet"),
    ("0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE", "eth", "Binance Hot Wallet"),
    ("0x28C6c06298d514Db089934071355E5743bf21d60", "eth", "Binance 14"),
    ("0x47ac0Fb4F2D84898e4D9E7b4DaB3C24507a6D503", "eth", "Binance 15"),
    ("0xF977814e90dA44bFA03b6295A0616a897441aceC", "eth", "Binance 8"),
    ("0xab5801a7d398351b8be11c439e05c5b3259aec9b", "eth", "Vitalik 2"),
    ("0x0716a17FBAeE714f1E6aB0f9d59edbC5f09815C0", "eth", "Justin Sun"),
    ("0x2FAF487A4414Fe77e2327F0bf4AE2a264a776AD2", "eth", "FTX Exchange"),
    ("0x6262998Ced04146fA42253a5C0AF90CA02dfd2A3", "eth", "Crypto.com"),
    # ── Solana ────────────────────────────────────────────
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "sol", "Jump Trading"),
    ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "sol", "Raydium Program"),
    ("GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ",  "sol", "Solana Foundation"),
    ("EoTcMgcDRTJVZDMZWBoU6rhYHZfkNTVAPHTsuPG9ggB3", "sol", "Alameda Research"),
    ("CakcnaRDHka2gXyfxNkB9mzMHsvQkxZVBfEHzSFyMJzK", "sol", "FTX Wallet SOL"),
]

# ── Helpers ─────────────────────────────────────────────

def detect_chain(address: str) -> str | None:
    address = address.strip()
    if re.match(r'^0x[0-9a-fA-F]{40}$', address, re.IGNORECASE):
        return "eth"
    if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
        return "sol"
    return None

def short_addr(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address

# ── FSM ─────────────────────────────────────────────────

class AddWallet(StatesGroup):
    waiting_address = State()
    waiting_chain   = State()
    waiting_label   = State()

# ── Keyboards ────────────────────────────────────────────

def main_menu(plan: str = "free", count: int = 0) -> InlineKeyboardMarkup:
    plan_emoji = {"free": "🆓", "hunter": "🎯", "apex": "⚡"}.get(plan, "🆓")
    limit = PLAN_LIMITS.get(plan, 3)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"👁 Wallets ({count}/{limit})", callback_data="my_wallets"),
            InlineKeyboardButton(text="➕ Add",                        callback_data="add_wallet"),
        ],
        [InlineKeyboardButton(text="🐋 Whale Watchlist",               callback_data="whales")],
        [InlineKeyboardButton(text=f"{plan_emoji} {plan.upper()} → Upgrade", callback_data="upgrade")],
        [InlineKeyboardButton(text="❓ Help",                          callback_data="help")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Main Menu", callback_data="back_main")]
    ])

def chain_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⟠ Ethereum",  callback_data="setchain:eth"),
            InlineKeyboardButton(text="⬡ BNB Chain", callback_data="setchain:bsc"),
        ],
        [InlineKeyboardButton(text="🔵 Base",        callback_data="setchain:base")],
        [InlineKeyboardButton(text="❌ Cancel",       callback_data="cancel")],
    ])

# ── /start ───────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username or "")
    user  = await get_user(msg.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(msg.from_user.id)

    await msg.answer(
        "🔔 <b>Rifrush</b> — On-Chain Wallet Tracker\n\n"
        "I watch wallets on ETH, SOL, BSC and Base\n"
        "and alert you the moment funds move.\n\n"
        f"Plan: <b>{plan.upper()}</b> · "
        f"Wallets: <b>{count}/{PLAN_LIMITS[plan]}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(plan, count)
    )

# ── My Wallets ───────────────────────────────────────────

@router.callback_query(F.data == "my_wallets")
async def cb_my_wallets(cb: CallbackQuery):
    wallets = await get_user_wallets(cb.from_user.id)

    if not wallets:
        await cb.message.edit_text(
            "👁 <b>Your Wallets</b>\n\nNo wallets yet. Add one to start tracking.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add Wallet",  callback_data="add_wallet")],
                [InlineKeyboardButton(text="🐋 Whale List",  callback_data="whales")],
                [InlineKeyboardButton(text="← Main Menu",   callback_data="back_main")],
            ])
        )
        await cb.answer()
        return

    lines   = []
    buttons = []
    for w in wallets:
        emoji = CHAIN_EMOJI.get(w["chain"], "🔗")
        label = f" · {w['label']}" if w.get("label") else ""
        lines.append(f"{emoji} <code>{short_addr(w['address'])}</code>{label} ({w['chain'].upper()})")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Remove {short_addr(w['address'])} ({w['chain'].upper()})",
            callback_data=f"rm:{w['id']}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet")])
    buttons.append([InlineKeyboardButton(text="← Main Menu",  callback_data="back_main")])

    await cb.message.edit_text(
        "👁 <b>Your Wallets</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("rm:"))
async def cb_remove(cb: CallbackQuery):
    try:
        wallet_id = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        await cb.answer("Error")
        return

    # Remove by id
    import aiosqlite
    async with aiosqlite.connect("rifrush.db") as db:
        await db.execute("DELETE FROM wallets WHERE id = ? AND user_id = ?",
                         (wallet_id, cb.from_user.id))
        await db.commit()

    await cb.answer("✅ Removed")
    await cb_my_wallets(cb)

# ── Add Wallet ───────────────────────────────────────────

@router.callback_query(F.data == "add_wallet")
async def cb_add_wallet(cb: CallbackQuery, state: FSMContext):
    user  = await get_user(cb.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)

    if count >= PLAN_LIMITS[plan]:
        await cb.message.edit_text(
            f"⚠️ <b>Limit reached</b>\n\n"
            f"<b>{plan.upper()}</b> allows max <b>{PLAN_LIMITS[plan]} wallets</b>.\n"
            "Upgrade to track more.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬆️ Upgrade", callback_data="upgrade")],
                [InlineKeyboardButton(text="← Back",     callback_data="back_main")],
            ])
        )
        await cb.answer()
        return

    await state.set_state(AddWallet.waiting_address)
    await cb.message.edit_text(
        "➕ <b>Add Wallet</b>\n\n"
        "Paste the address:\n"
        "· EVM (ETH/BSC/Base): <code>0x...</code>\n"
        "· Solana: base58 address",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]
        ])
    )
    await cb.answer()

@router.message(AddWallet.waiting_address)
async def process_address(msg: Message, state: FSMContext):
    address = (msg.text or "").strip()
    chain   = detect_chain(address)
    logging.info(f"[AddWallet] '{address}' → {chain}")

    if not chain:
        await msg.answer(
            "❌ <b>Invalid address</b>\n\n"
            "· EVM: <code>0x</code> + 40 hex chars\n"
            "· Solana: base58, 32–44 chars\n\nTry again:",
            parse_mode="HTML"
        )
        return

    if chain == "eth":
        await state.update_data(address=address)
        await state.set_state(AddWallet.waiting_chain)
        await msg.answer(
            f"✅ EVM address:\n<code>{address}</code>\n\nSelect chain:",
            parse_mode="HTML",
            reply_markup=chain_kb()
        )
    else:
        await state.update_data(address=address, chain="sol")
        await state.set_state(AddWallet.waiting_label)
        await msg.answer(
            f"◎ Solana address:\n<code>{address}</code>\n\n"
            "Add a label (e.g. <i>My wallet</i>) or skip:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip", callback_data="skip_label")]
            ])
        )

@router.callback_query(F.data.startswith("setchain:"))
async def cb_set_chain(cb: CallbackQuery, state: FSMContext):
    chain = cb.data.split(":")[1]
    data  = await state.get_data()
    await state.update_data(chain=chain)
    await state.set_state(AddWallet.waiting_label)
    await cb.message.edit_text(
        f"{CHAIN_EMOJI[chain]} <b>{CHAIN_LABELS[chain]}</b>\n\n"
        f"<code>{data.get('address', '')}</code>\n\n"
        "Add a label or skip:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="skip_label")]
        ])
    )
    await cb.answer()

@router.callback_query(F.data == "skip_label")
async def cb_skip_label(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _save_wallet(cb, state, data.get("address", ""), data.get("chain", "eth"), "")

@router.message(AddWallet.waiting_label)
async def process_label(msg: Message, state: FSMContext):
    label = "" if (msg.text or "").strip().lower() == "skip" else (msg.text or "").strip()[:32]
    data  = await state.get_data()
    await _save_wallet(msg, state, data.get("address", ""), data.get("chain", "eth"), label)

async def _save_wallet(event, state, address, chain, label):
    user_id = event.from_user.id
    added   = await add_wallet(user_id, address, chain, label)
    await state.clear()

    emoji     = CHAIN_EMOJI.get(chain, "🔗")
    label_str = f" · <i>{label}</i>" if label else ""
    text = (
        f"✅ <b>Wallet added!</b>\n\n"
        f"{emoji} <code>{short_addr(address)}</code>{label_str}\n"
        f"Chain: <b>{CHAIN_LABELS.get(chain, chain)}</b>\n\n"
        "I'll alert you when this wallet moves."
        if added else
        "⚠️ This wallet is already in your list."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another", callback_data="add_wallet")],
        [InlineKeyboardButton(text="← Main Menu",   callback_data="back_main")],
    ])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb)

# ── Whale Watchlist ──────────────────────────────────────

@router.callback_query(F.data == "whales")
async def cb_whales(cb: CallbackQuery):
    eth_lines = []
    sol_lines = []
    buttons   = []

    for i, (address, chain, name) in enumerate(WHALES):
        emoji = CHAIN_EMOJI[chain]
        line  = f"{emoji} <b>{name}</b> · <code>{short_addr(address)}</code>"
        if chain == "eth":
            eth_lines.append(line)
        else:
            sol_lines.append(line)
        buttons.append([InlineKeyboardButton(
            text=f"➕ {name}",
            callback_data=f"tw:{i}"
        )])

    buttons.append([InlineKeyboardButton(text="← Main Menu", callback_data="back_main")])

    text = (
        "🐋 <b>Whale Watchlist</b>\n\n"
        "<b>Ethereum</b>\n" + "\n".join(eth_lines) +
        "\n\n<b>Solana</b>\n" + "\n".join(sol_lines) +
        "\n\n<i>Tap any name to add to your tracker:</i>"
    )

    await cb.message.edit_text(text, parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await cb.answer()

@router.callback_query(F.data.startswith("tw:"))
async def cb_track_whale(cb: CallbackQuery):
    try:
        idx              = int(cb.data.split(":")[1])
        address, chain, name = WHALES[idx]
    except (IndexError, ValueError):
        await cb.answer("❌ Unknown whale", show_alert=True)
        return

    user  = await get_user(cb.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)

    if count >= PLAN_LIMITS[plan]:
        await cb.answer("⚠️ Wallet limit reached. Upgrade your plan.", show_alert=True)
        return

    added = await add_wallet(cb.from_user.id, address, chain, name)
    if added:
        await cb.answer(f"✅ Now tracking {name}", show_alert=True)
    else:
        await cb.answer("Already in your list.", show_alert=True)

# ── UPGRADE & PAYMENT ────────────────────────────────────

@router.callback_query(F.data == "upgrade")
async def cb_upgrade(cb: CallbackQuery):
    await cb.message.edit_text(
        "⬆️ <b>Upgrade Rifrush</b>\n\n"
        "🎯 <b>HUNTER — $19/mo</b>\n"
        "· 25 wallets · 60s checks · All 4 chains\n\n"
        "⚡ <b>APEX — $49/mo</b>\n"
        "· Unlimited wallets · &lt;30s alerts\n"
        "· Copy trading signals\n\n"
        "💰 Pay with crypto — USDT, TON, BTC, ETH\n"
        "No KYC · No banks · Cancel anytime",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Pay $19 — HUNTER", callback_data="pay:hunter")],
            [InlineKeyboardButton(text="⚡ Pay $49 — APEX",   callback_data="pay:apex")],
            [InlineKeyboardButton(text="← Back",              callback_data="back_main")],
        ])
    )
    await cb.answer()

@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery):
    plan = cb.data.split(":")[1]
    if plan not in PLAN_PRICES:
        await cb.answer("Unknown plan")
        return

    if not CRYPTOBOT_TOKEN:
        # CryptoBot not configured yet — fallback message
        await cb.message.edit_text(
            f"💳 <b>Pay for {PLAN_NAMES[plan]}</b>\n\n"
            f"Amount: <b>${PLAN_PRICES[plan]} USDT</b>\n\n"
            "Send payment and type /paid — your plan activates shortly.",
            parse_mode="HTML",
            reply_markup=back_kb()
        )
        await cb.answer()
        return

    # Create invoice via CryptoBot API
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                json={
                    "asset":       "USDT",
                    "amount":      str(PLAN_PRICES[plan]),
                    "description": f"Rifrush {plan.upper()} — 30 days",
                    "payload":     f"{cb.from_user.id}:{plan}",
                    "expires_in":  3600,
                }
            )
            data = await resp.json()

        if data.get("ok"):
            invoice = data["result"]
            pay_url = invoice["pay_url"]
            await cb.message.edit_text(
                f"💳 <b>{PLAN_NAMES[plan]} — ${PLAN_PRICES[plan]}/mo</b>\n\n"
                "Tap the button below to pay in USDT, TON, BTC or ETH.\n"
                "Your plan activates <b>automatically</b> after payment.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"💳 Pay ${PLAN_PRICES[plan]} USDT", url=pay_url)],
                    [InlineKeyboardButton(text="← Back", callback_data="upgrade")],
                ])
            )
        else:
            raise Exception(data.get("error", "Unknown error"))

    except Exception as e:
        logging.error(f"CryptoBot invoice error: {e}")
        await cb.message.edit_text(
            "⚠️ Payment system temporarily unavailable.\n"
            "Please try again in a few minutes.",
            parse_mode="HTML",
            reply_markup=back_kb()
        )
    await cb.answer()

# ── CryptoBot Webhook ────────────────────────────────────
# Receives payment confirmation from CryptoBot
# Set webhook in @CryptoBot → My Apps → your app → Webhooks

@router.message(Command("cryptobot_update"))
async def cmd_cryptobot_update(msg: Message):
    """
    CryptoBot sends updates via webhook to your endpoint.
    For Railway (no webhook server), poll manually or use the check below.
    """
    pass

@router.message(Command("checkpayment"))
async def cmd_check_payment(msg: Message):
    """User types /checkpayment after paying — bot verifies via CryptoBot API."""
    if not CRYPTOBOT_TOKEN:
        await msg.answer("Payment system not configured yet.")
        return

    user_id = msg.from_user.id
    await msg.answer("🔍 Checking your payment...")

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                "https://pay.crypt.bot/api/getInvoices",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                params={"status": "paid", "count": 100}
            )
            data = await resp.json()

        if not data.get("ok"):
            await msg.answer("⚠️ Could not check payment. Try again.")
            return

        invoices = data["result"].get("items", [])
        activated = False

        for inv in invoices:
            payload = inv.get("payload", "")
            if payload.startswith(f"{user_id}:"):
                plan = payload.split(":")[1]
                if plan in PLAN_PRICES:
                    from datetime import datetime, timedelta
                    paid_until = (datetime.utcnow() + timedelta(days=30)).isoformat()
                    await upgrade_user(user_id, plan, paid_until)
                    await msg.answer(
                        f"✅ <b>Payment confirmed!</b>\n\n"
                        f"Plan: <b>{PLAN_NAMES[plan]}</b>\n"
                        f"Active until: <b>{paid_until[:10]}</b>\n\n"
                        "Enjoy your upgraded limits!",
                        parse_mode="HTML",
                        reply_markup=back_kb()
                    )
                    activated = True
                    break

        if not activated:
            await msg.answer(
                "❌ No paid invoice found for your account.\n\n"
                "If you just paid, wait 1-2 minutes and try again.\n"
                "Or contact support.",
                parse_mode="HTML"
            )

    except Exception as e:
        logging.error(f"checkpayment error: {e}")
        await msg.answer("⚠️ Error checking payment. Try again.")

# ── Admin commands ───────────────────────────────────────

@router.message(Command("upgrade_user"))
async def cmd_upgrade_user(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = (msg.text or "").split()
    if len(parts) != 3 or parts[2] not in ("free", "hunter", "apex"):
        await msg.answer("Usage: /upgrade_user USER_ID PLAN")
        return
    from datetime import datetime, timedelta
    uid        = int(parts[1])
    plan       = parts[2]
    paid_until = (datetime.utcnow() + timedelta(days=30)).isoformat()
    await upgrade_user(uid, plan, paid_until)
    await msg.answer(f"✅ User {uid} → {plan.upper()} until {paid_until[:10]}")

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    import aiosqlite
    async with aiosqlite.connect("rifrush.db") as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM wallets") as c:
            wallets = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE plan != 'free'") as c:
            paid = (await c.fetchone())[0]
    await msg.answer(
        f"📊 <b>Rifrush Stats</b>\n\n"
        f"👤 Users: <b>{users}</b>\n"
        f"👁 Wallets tracked: <b>{wallets}</b>\n"
        f"💰 Paid users: <b>{paid}</b>",
        parse_mode="HTML"
    )

# ── Help ─────────────────────────────────────────────────

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "❓ <b>How Rifrush works</b>\n\n"
        "1️⃣ Add any wallet address\n"
        "2️⃣ I monitor it 24/7 on-chain\n"
        "3️⃣ Instant Telegram alert on every move\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/checkpayment — Verify your payment\n\n"
        "<b>Chains:</b>\n"
        "⟠ ETH · ◎ SOL · ⬡ BSC · 🔵 Base\n\n"
        "<b>Plans:</b>\n"
        "🆓 Free — 3 wallets\n"
        "🎯 Hunter — 25 wallets · $19/mo\n"
        "⚡ Apex — Unlimited · $49/mo",
        parse_mode="HTML",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── Navigation ────────────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user  = await get_user(cb.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)
    await cb.message.edit_text(
        f"🔔 <b>Rifrush</b> — On-Chain Wallet Tracker\n\n"
        f"Plan: <b>{plan.upper()}</b> · "
        f"Wallets: <b>{count}/{PLAN_LIMITS[plan]}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(plan, count)
    )
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_back_main(cb, state)

import logging
import os
import re
import aiohttp
from datetime import datetime, timedelta, timezone, timezone

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import (
    upsert_user, get_user, get_user_wallets,
    add_wallet, remove_wallet, get_wallet_count,
    upgrade_user, PLAN_LIMITS,
    remove_wallet_by_id, get_stats,
)

router = Router()

# ── Constants ───────────────────────────────────────────

CRYPTOBOT_TOKEN  = os.getenv("CRYPTOBOT_TOKEN", "")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
PRIVATE_GROUP_ID = os.getenv("PRIVATE_GROUP_ID", "")  # Paid members group

CHAIN_EMOJI  = {"eth": "⟠", "bsc": "⬡", "base": "🔵", "sol": "◎"}
CHAIN_LABELS = {"eth": "Ethereum", "bsc": "BNB Chain", "base": "Base", "sol": "Solana"}

PLAN_PRICES = {"hunter": 19, "apex": 49}
PLAN_NAMES  = {"hunter": "🎯 HUNTER", "apex": "⚡ APEX"}

VALID_PLANS = {"free", "hunter", "apex"}

# ── Plan helper ──────────────────────────────────────────
# Always use this instead of user["plan"] directly.
# Validates the plan value and checks subscription expiry.

def get_plan(user: dict | None) -> str:
    """Return the user's effective plan, falling back to 'free' on any issue."""
    if not user:
        return "free"
    plan = user.get("plan", "free")
    if plan not in VALID_PLANS:
        logging.warning(f"Unknown plan value '{plan}' for user {user.get('id')} — defaulting to free")
        return "free"
    if plan != "free":
        paid_until = user.get("paid_until")
        if paid_until is not None:
            # Support both datetime objects and ISO strings from DB
            if isinstance(paid_until, str):
                try:
                    paid_until = datetime.fromisoformat(paid_until)
                except ValueError:
                    logging.warning(f"Unparseable paid_until '{paid_until}' — downgrading to free")
                    return "free"
            # Make comparison timezone-aware safe
            now = datetime.now(timezone.utc) if paid_until.tzinfo else datetime.utcnow()
            if paid_until < now:
                return "free"   # subscription expired
    return plan

def plan_limit(plan: str) -> int:
    """Safe PLAN_LIMITS access — never raises KeyError."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS.get("free", 3))

# ── Whale Watchlist ─────────────────────────────────────
# callback_data = "tw:INDEX" — stays well under Telegram's 64-byte limit

WHALES = [
    # ── Ethereum — real whale wallets ─────────────────────
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
    # ── Solana — real wallets only (no smart contracts) ───
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "sol", "Jump Trading"),
    # Raydium Program removed — smart contract, not a whale
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
    is_new = await get_user(msg.from_user.id) is None
    await upsert_user(msg.from_user.id, msg.from_user.username or "")
    user  = await get_user(msg.from_user.id)
    plan  = get_plan(user)
    count = await get_wallet_count(msg.from_user.id)

    if is_new:
        # Onboarding for new users
        await msg.answer(
            "👋 <b>Welcome to Rifrush!</b>\n\n"
            "I track crypto wallets on ETH, SOL, BSC and Base\n"
            "and send you an instant Telegram alert the moment funds move.\n\n"
            "🚀 <b>Get started in 3 steps:</b>\n\n"
            "1️⃣ Tap <b>🐋 Whale Watchlist</b> — add a known whale in one tap\n"
            "2️⃣ Or tap <b>➕ Add</b> — paste any wallet address\n"
            "3️⃣ Receive instant alerts when funds move\n\n"
            "✅ <b>3 wallets free forever</b> · No signup · No KYC",
            parse_mode="HTML",
            reply_markup=main_menu(plan, count)
        )
    else:
        await msg.answer(
            "🔔 <b>Rifrush</b> — On-Chain Wallet Tracker\n\n"
            f"Plan: <b>{plan.upper()}</b> · "
            f"Wallets: <b>{count}/{plan_limit(plan)}</b>",
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
    await remove_wallet_by_id(wallet_id, cb.from_user.id)

    await cb.answer("✅ Removed")
    await cb_my_wallets(cb)

# ── Add Wallet ───────────────────────────────────────────

@router.callback_query(F.data == "add_wallet")
async def cb_add_wallet(cb: CallbackQuery, state: FSMContext):
    user  = await get_user(cb.from_user.id)
    plan  = get_plan(user)
    count = await get_wallet_count(cb.from_user.id)

    if count >= plan_limit(plan):
        await cb.message.edit_text(
            f"⚠️ <b>Limit reached</b>\n\n"
            f"<b>{plan.upper()}</b> allows max <b>{plan_limit(plan)} wallets</b>.\n"
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
    plan  = get_plan(user)
    count = await get_wallet_count(cb.from_user.id)

    if count >= plan_limit(plan):
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
                    paid_until = datetime.now(timezone.utc) + timedelta(days=30)
                    await upgrade_user(user_id, plan, paid_until)
                    await msg.answer(
                        f"✅ <b>Payment confirmed!</b>\n\n"
                        f"Plan: <b>{PLAN_NAMES.get(plan, plan.upper())}</b>\n"
                        f"Active until: <b>{paid_until.strftime('%Y-%m-%d')}</b>\n\n"
                        "Enjoy your upgraded limits! 🎉",
                        parse_mode="HTML",
                        reply_markup=back_kb()
                    )
                    # Invite to private group
                    base = PLAN_BASE.get(plan, plan)
                    await add_to_private_group(msg.bot, user_id, base)
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


# ── /threshold ───────────────────────────────────────────

@router.message(Command("threshold"))
async def cmd_threshold(msg: Message, state: FSMContext):
    """Set minimum alert threshold."""
    user = await get_user(msg.from_user.id)
    plan = get_plan(user)

    if plan == "free":
        await msg.answer(
            "🔒 <b>Alert Threshold</b>\n\n"
            "Free plan: alerts only on moves > <b>$500</b>\n"
            "This protects you from spam automatically.\n\n"
            "Upgrade to set a custom threshold:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 Hunter — from $100", callback_data="pay:hunter")],
                [InlineKeyboardButton(text="⚡ Apex — from $1",    callback_data="pay:apex")],
            ])
        )
        return

    min_amount = 100 if plan == "hunter" else 1
    presets = [100, 500, 1000] if plan == "hunter" else [1, 100, 500, 1000]
    preset_btns = [
        [InlineKeyboardButton(text=f"${p}", callback_data=f"setth:{p}")]
        for p in presets
    ]
    preset_btns.append([InlineKeyboardButton(text="✏️ Custom amount", callback_data="setth:custom")])
    preset_btns.append([InlineKeyboardButton(text="← Back", callback_data="back_main")])

    from database import get_user_threshold
    current = await get_user_threshold(msg.from_user.id)
    current_str = f"${current}" if current else "default"

    await msg.answer(
        f"🎚 <b>Alert Threshold</b>\n\n"
        f"Plan: <b>{plan.upper()}</b> · Current: <b>{current_str}</b>\n"
        f"Minimum allowed: <b>${min_amount}</b>\n\n"
        "Pick a preset or enter custom amount:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=preset_btns)
    )


class SetThreshold(StatesGroup):
    waiting_amount = State()


@router.callback_query(F.data.startswith("setth:"))
async def cb_set_threshold(cb: CallbackQuery, state: FSMContext):
    value = cb.data.split(":")[1]
    user  = await get_user(cb.from_user.id)
    plan  = get_plan(user)
    min_amount = 100 if plan == "hunter" else 1

    if value == "custom":
        await state.set_state(SetThreshold.waiting_amount)
        await cb.message.edit_text(
            f"✏️ <b>Enter threshold in USD</b>\n\n"
            f"Minimum for your plan: <b>${min_amount}</b>\n\n"
            "Just send a number, e.g. <code>250</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]
            ])
        )
        await cb.answer()
        return

    await _apply_threshold(cb, int(value), plan, min_amount)


@router.message(SetThreshold.waiting_amount)
async def process_threshold_input(msg: Message, state: FSMContext):
    user = await get_user(msg.from_user.id)
    plan = get_plan(user)
    min_amount = 100 if plan == "hunter" else 1

    try:
        amount = int(msg.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        await msg.answer("❌ Please send a number, e.g. <code>500</code>", parse_mode="HTML")
        return

    if amount < min_amount:
        await msg.answer(
            f"❌ Minimum threshold for <b>{plan.upper()}</b> is <b>${min_amount}</b>\n\nTry again:",
            parse_mode="HTML"
        )
        return

    await state.clear()
    await _apply_threshold(msg, amount, plan, min_amount)


async def _apply_threshold(event, amount: int, plan: str, min_amount: int):
    from database import set_user_threshold
    await set_user_threshold(event.from_user.id, amount)

    text = (
        f"✅ <b>Threshold set to ${amount}</b>\n\n"
        f"You'll only receive alerts on moves > <b>${amount}</b>\n"
        f"Plan: <b>{plan.upper()}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Main Menu", callback_data="back_main")]
    ])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb)

# ── /digest ──────────────────────────────────────────────

@router.message(Command("digest"))
async def cmd_digest(msg: Message):
    """Manual daily digest — summary of tracked wallets."""
    wallets = await get_user_wallets(msg.from_user.id)

    if not wallets:
        await msg.answer(
            "📊 No wallets tracked yet.\nAdd wallets first with ➕ Add.",
            parse_mode="HTML"
        )
        return

    lines = ["📊 <b>Your Wallet Summary</b>\n"]
    for w in wallets:
        emoji = {"eth": "⟠", "bsc": "⬡", "base": "🔵", "sol": "◎"}.get(w["chain"], "🔗")
        label = f" <i>{w['label']}</i>" if w.get("label") else ""
        last = f"Last tx: <code>{w['last_tx'][:10]}…</code>" if w.get("last_tx") else "No activity yet"
        lines.append(f"{emoji}{label} <code>{w['address'][:6]}…{w['address'][-4:]}</code>\n   {last}")

    lines.append(f"\n<i>Tracking {len(wallets)} wallet(s) · Updates every minute</i>")

    await msg.answer("\n\n".join(lines), parse_mode="HTML")


# ── /support — user sends message to admin ───────────────

@router.message(Command("support"))
async def cmd_support(msg: Message):
    """User types /support Your message here — forwards to admin."""
    text = (msg.text or "").replace("/support", "").strip()

    if not text:
        await msg.answer(
            "📬 <b>Contact Support</b>\n\n"
            "Type your message after the command:\n"
            "<code>/support I have a question about...</code>\n\n"
            "We respond within 2–4 hours.",
            parse_mode="HTML"
        )
        return

    user = msg.from_user
    username = f"@{user.username}" if user.username else f"ID:{user.id}"

    # Forward to admin
    if ADMIN_ID:
        try:
            await msg.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📬 <b>Support Request</b>\n\n"
                    f"From: <b>{user.full_name}</b> ({username})\n"
                    f"User ID: <code>{user.id}</code>\n\n"
                    f"Message:\n{text}\n\n"
                    f"Reply: <code>/reply {user.id} your answer here</code>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to forward support message: {e}")

    await msg.answer(
        "✅ <b>Message sent!</b>\n\n"
        "We'll get back to you within 2–4 hours.\n"
        "Thank you for reaching out! 🙏",
        parse_mode="HTML"
    )


# ── /feedback — quick feedback button ────────────────────

@router.message(Command("feedback"))
async def cmd_feedback(msg: Message):
    """Alias for /support with different prompt."""
    text = (msg.text or "").replace("/feedback", "").strip()

    if not text:
        await msg.answer(
            "💡 <b>Send Feedback</b>\n\n"
            "Found a bug? Have a feature idea? Missing a chain?\n\n"
            "Just type:\n"
            "<code>/feedback Your idea or issue here</code>\n\n"
            "We read every message and ship fast.",
            parse_mode="HTML"
        )
        return

    user = msg.from_user
    username = f"@{user.username}" if user.username else f"ID:{user.id}"

    if ADMIN_ID:
        try:
            await msg.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💡 <b>Feedback</b>\n\n"
                    f"From: <b>{user.full_name}</b> ({username})\n"
                    f"User ID: <code>{user.id}</code>\n\n"
                    f"Feedback:\n{text}\n\n"
                    f"Reply: <code>/reply {user.id} your answer here</code>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to forward feedback: {e}")

    await msg.answer(
        "✅ <b>Feedback received!</b>\n\n"
        "Thank you — this helps us improve Rifrush.\n"
        "We'll reply if we need more details.",
        parse_mode="HTML"
    )


# ── /reply — admin replies to user ───────────────────────

@router.message(Command("reply"))
async def cmd_reply(msg: Message):
    """Admin only: /reply USER_ID your message here"""
    if msg.from_user.id != ADMIN_ID:
        return

    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer(
            "Usage: <code>/reply USER_ID your message here</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_id = int(parts[1])
        reply_text = parts[2]
    except ValueError:
        await msg.answer("❌ Invalid user ID")
        return

    try:
        await msg.bot.send_message(
            chat_id=target_id,
            text=(
                f"📩 <b>Support Reply</b>\n\n"
                f"{reply_text}\n\n"
                f"<i>Need more help? Type /support your question</i>"
            ),
            parse_mode="HTML"
        )
        await msg.answer(f"✅ Reply sent to user {target_id}")
    except Exception as e:
        await msg.answer(f"❌ Failed to send: {e}")


# ── /annual — annual plans with discount ─────────────────

@router.message(Command("annual"))
async def cmd_annual(msg: Message):
    """Show annual pricing with discount."""
    await msg.answer(
        "📅 <b>Annual Plans — Save up to 35%</b>\n\n"
        "🎯 <b>HUNTER Annual</b>\n"
        "   $149/year <s>$228</s> · Save $79 · ~$12.4/mo\n\n"
        "⚡ <b>APEX Annual</b>\n"
        "   $399/year <s>$588</s> · Save $189 · ~$33.2/mo\n\n"
        "💰 Pay in USDT, TON, BTC or ETH\n"
        "No KYC · Cancel-free (one payment)\n\n"
        "To activate annual plan:\n"
        "1. Send payment to the address in /upgrade\n"
        "2. Type <code>/support Annual Hunter paid</code>\n"
        "3. We activate your 365-day plan within 1 hour",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🎯 Hunter Annual — $149",
                callback_data="annual:hunter"
            )],
            [InlineKeyboardButton(
                text="⚡ Apex Annual — $399",
                callback_data="annual:apex"
            )],
            [InlineKeyboardButton(
                text="← Back",
                callback_data="back_main"
            )],
        ])
    )


@router.callback_query(F.data.startswith("annual:"))
async def cb_annual(cb: CallbackQuery):
    plan = cb.data.split(":")[1]
    prices = {"hunter": ("$149", "HUNTER", "$228"), "apex": ("$399", "APEX", "$588")}
    price, plan_name, original = prices.get(plan, ("?", "?", "?"))

    await cb.message.edit_text(
        f"📅 <b>{plan_name} Annual Plan — {price}</b>\n\n"
        f"<s>{original}</s> → <b>{price}/year</b>\n\n"
        "To pay:\n"
        "1️⃣ Go to /upgrade and note the payment address\n"
        "2️⃣ Send exact amount in USDT/TON/BTC/ETH\n"
        f"3️⃣ Type: <code>/support Annual {plan_name} paid</code>\n\n"
        "✅ Your 365-day plan activates within 1 hour.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Go to payment", callback_data="upgrade")],
            [InlineKeyboardButton(text="← Back", callback_data="back_main")],
        ])
    )
    await cb.answer()


# ── GROUP MANAGEMENT ────────────────────────────────────

async def add_to_private_group(bot, user_id: int, plan: str):
    """Add paid user to private group and send invite."""
    if not PRIVATE_GROUP_ID:
        return
    if plan not in ("hunter", "apex"):
        return
    try:
        # Create one-time invite link
        link = await bot.create_chat_invite_link(
            chat_id=PRIVATE_GROUP_ID,
            member_limit=1,
            name=f"User {user_id}"
        )
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Welcome to Rifrush Premium!</b>\n\n"
                "You now have access to our private members group\n"
                "where we share daily whale analysis and alpha.\n\n"
                f"👥 <a href='{link.invite_link}'>Join Private Group →</a>\n\n"
                "<i>This link is single-use and expires after joining.</i>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logging.info(f"Invite sent to {user_id} for {plan}")
    except Exception as e:
        logging.warning(f"Failed to add {user_id} to group: {e}")


async def remove_from_private_group(bot, user_id: int):
    """Remove expired user from private group."""
    if not PRIVATE_GROUP_ID:
        return
    try:
        await bot.ban_chat_member(
            chat_id=PRIVATE_GROUP_ID,
            user_id=user_id
        )
        # Immediately unban so they can rejoin if they resubscribe
        await bot.unban_chat_member(
            chat_id=PRIVATE_GROUP_ID,
            user_id=user_id,
            only_if_banned=True
        )
        logging.info(f"Removed {user_id} from private group")
    except Exception as e:
        logging.warning(f"Failed to remove {user_id} from group: {e}")

# ── Admin commands ───────────────────────────────────────

@router.message(Command("upgrade_user"))
async def cmd_upgrade_user(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = (msg.text or "").split()
    if len(parts) != 3 or parts[2] not in ("free", "hunter", "apex"):
        await msg.answer("Usage: /upgrade_user USER_ID PLAN")
        return
    uid        = int(parts[1])
    plan       = parts[2]
    paid_until = datetime.now(timezone.utc) + timedelta(days=30)
    await upgrade_user(uid, plan, paid_until)
    await msg.answer(f"✅ User {uid} → {plan.upper()} until {paid_until.strftime('%Y-%m-%d')}")
    # Invite to private group if paid plan
    if plan in ("hunter", "apex"):
        await add_to_private_group(msg.bot, uid, plan)

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    s = await get_stats()
    await msg.answer(
        f"📊 <b>Rifrush Stats</b>\n\n"
        f"👤 Users: <b>{s['users']}</b>\n"
        f"👁 Wallets tracked: <b>{s['wallets']}</b>\n"
        f"💰 Paid users: <b>{s['paid']}</b>",
        parse_mode="HTML"
    )

# ── Help ─────────────────────────────────────────────────

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "❓ <b>Rifrush — Help</b>\n\n"
        "<b>How it works:</b>\n"
        "1️⃣ Add any wallet (EVM or Solana)\n"
        "2️⃣ Bot monitors it 24/7 on-chain\n"
        "3️⃣ Instant Telegram alert on every move\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/checkpayment — Verify crypto payment\n"
        "/threshold — Set alert minimum ($, Hunter+)\n"
        "/digest — Get today's activity summary\n"
        "/support — Contact support\n"
        "/feedback — Send feedback\n"
        "/annual — Annual plans (save 35%)\n"
        "/cancel — Cancel subscription\n\n"
        "<b>Alert thresholds:</b>\n"
        "🆓 Free — alerts on moves > $500\n"
        "🎯 Hunter — custom from $100\n"
        "⚡ Apex — custom from $1\n\n"
        "<b>Supported chains:</b>\n"
        "⟠ ETH · ◎ SOL · ⬡ BSC · 🔵 Base\n\n"
        "<b>Plans:</b>\n"
        "🆓 Scout — 3 wallets · Free\n"
        "🎯 Hunter — 25 wallets · $19/mo\n"
        "⚡ Apex — Unlimited · $49/mo\n\n"
        "Questions? Just message us here 👇",
        parse_mode="HTML",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── Navigation ────────────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user  = await get_user(cb.from_user.id)
    plan  = get_plan(user)
    count = await get_wallet_count(cb.from_user.id)
    await cb.message.edit_text(
        f"🔔 <b>Rifrush</b> — On-Chain Wallet Tracker\n\n"
        f"Plan: <b>{plan.upper()}</b> · "
        f"Wallets: <b>{count}/{plan_limit(plan)}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(plan, count)
    )
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_back_main(cb, state)

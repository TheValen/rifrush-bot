import logging
import os
import re

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

# ── Chain helpers ───────────────────────────────────────

CHAIN_EMOJI  = {"eth": "⟠", "bsc": "⬡", "base": "🔵", "sol": "◎"}
CHAIN_LABELS = {"eth": "Ethereum", "bsc": "BNB Chain", "base": "Base", "sol": "Solana"}

def detect_chain(address: str) -> str | None:
    address = address.strip()
    if re.match(r'^0x[0-9a-fA-F]{40}$', address, re.IGNORECASE):
        return "eth"
    if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
        return "sol"
    return None

def short_addr(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address

# ── FSM States ──────────────────────────────────────────

class AddWallet(StatesGroup):
    waiting_address = State()
    waiting_chain   = State()
    waiting_label   = State()

# ── Keyboards ───────────────────────────────────────────

def main_menu(plan: str = "free", count: int = 0) -> InlineKeyboardMarkup:
    plan_emoji = {"free": "🆓", "hunter": "🎯", "apex": "⚡"}.get(plan, "🆓")
    limit = PLAN_LIMITS.get(plan, 3)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"👁 My Wallets ({count}/{limit})", callback_data="my_wallets"),
            InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet"),
        ],
        [InlineKeyboardButton(text="🐋 Whale Watchlist", callback_data="whales")],
        [InlineKeyboardButton(text=f"{plan_emoji} Plan: {plan.upper()} → Upgrade", callback_data="upgrade")],
        [InlineKeyboardButton(text="❓ Help", callback_data="help")],
    ])

def chain_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⟠ Ethereum",  callback_data="setchain:eth"),
            InlineKeyboardButton(text="⬡ BNB Chain", callback_data="setchain:bsc"),
        ],
        [InlineKeyboardButton(text="🔵 Base",         callback_data="setchain:base")],
        [InlineKeyboardButton(text="❌ Cancel",        callback_data="cancel")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Main Menu", callback_data="back_main")]
    ])

# ── /start ──────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username or "")
    user  = await get_user(msg.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(msg.from_user.id)

    await msg.answer(
        "🔔 <b>Welcome to Rifrush</b>\n\n"
        "I watch crypto wallets on ETH, SOL, BSC and Base —\n"
        "and alert you the moment funds move.\n\n"
        f"Your plan: <b>{plan.upper()}</b>\n"
        f"Wallets tracked: <b>{count}/{PLAN_LIMITS[plan]}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(plan, count)
    )

# ── My Wallets ──────────────────────────────────────────

@router.callback_query(F.data == "my_wallets")
async def cb_my_wallets(cb: CallbackQuery):
    wallets = await get_user_wallets(cb.from_user.id)

    if not wallets:
        await cb.message.edit_text(
            "👁 <b>Your Wallets</b>\n\nNo wallets added yet.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet")],
                [InlineKeyboardButton(text="← Main Menu",  callback_data="back_main")],
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
            text=f"🗑 {short_addr(w['address'])} ({w['chain'].upper()})",
            callback_data=f"remove:{w['address']}:{w['chain']}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet")])
    buttons.append([InlineKeyboardButton(text="← Main Menu",  callback_data="back_main")])

    await cb.message.edit_text(
        "👁 <b>Your Wallets</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

# ── Remove wallet ───────────────────────────────────────

@router.callback_query(F.data.startswith("remove:"))
async def cb_remove(cb: CallbackQuery):
    parts = cb.data.split(":", 2)
    if len(parts) == 3:
        _, address, chain = parts
        await remove_wallet(cb.from_user.id, address, chain)
        await cb.answer("✅ Wallet removed")
    await cb_my_wallets(cb)

# ── Add Wallet ──────────────────────────────────────────

@router.callback_query(F.data == "add_wallet")
async def cb_add_wallet(cb: CallbackQuery, state: FSMContext):
    user  = await get_user(cb.from_user.id)
    plan  = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)

    if count >= PLAN_LIMITS[plan]:
        await cb.message.edit_text(
            f"⚠️ <b>Limit reached</b>\n\n"
            f"<b>{plan.upper()}</b> plan: max <b>{PLAN_LIMITS[plan]} wallets</b>.\n"
            "Upgrade to track more.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬆️ Upgrade", callback_data="upgrade")],
                [InlineKeyboardButton(text="← Main Menu", callback_data="back_main")],
            ])
        )
        await cb.answer()
        return

    await state.set_state(AddWallet.waiting_address)
    await cb.message.edit_text(
        "➕ <b>Add Wallet</b>\n\n"
        "Paste the wallet address:\n\n"
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

    logging.info(f"[AddWallet] address='{address}' chain={chain}")

    if not chain:
        await msg.answer(
            "❌ <b>Invalid address</b>\n\n"
            "· EVM: starts with <code>0x</code>, 42 characters total\n"
            "· Solana: base58, 32–44 characters\n\n"
            "Try again:",
            parse_mode="HTML"
        )
        return

    if chain == "eth":
        await state.update_data(address=address)
        await state.set_state(AddWallet.waiting_chain)
        await msg.answer(
            f"✅ EVM address detected:\n<code>{address}</code>\n\nSelect chain:",
            parse_mode="HTML",
            reply_markup=chain_kb()
        )
    else:
        await state.update_data(address=address, chain="sol")
        await state.set_state(AddWallet.waiting_label)
        await msg.answer(
            f"✅ Solana address:\n<code>{address}</code>\n\n"
            "Add a label (e.g. <i>Jump Trading</i>) or tap Skip:",
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
        f"{CHAIN_EMOJI[chain]} <b>{CHAIN_LABELS[chain]}</b> selected.\n\n"
        f"<code>{data.get('address', '')}</code>\n\n"
        "Add a label or tap Skip:",
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

async def _save_wallet(event, state: FSMContext, address: str, chain: str, label: str):
    user_id = event.from_user.id
    added   = await add_wallet(user_id, address, chain, label)
    await state.clear()

    emoji     = CHAIN_EMOJI.get(chain, "🔗")
    label_str = f" · <i>{label}</i>" if label else ""

    if added:
        text = (
            f"✅ <b>Wallet added!</b>\n\n"
            f"{emoji} <code>{short_addr(address)}</code>{label_str}\n"
            f"Chain: <b>{CHAIN_LABELS.get(chain, chain)}</b>\n\n"
            "I'll alert you when this wallet moves funds."
        )
    else:
        text = "⚠️ This wallet is already in your list."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another", callback_data="add_wallet")],
        [InlineKeyboardButton(text="← Main Menu",   callback_data="back_main")],
    ])

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb)

# ── Whale Watchlist ─────────────────────────────────────
# NOTE: Telegram callback_data limit = 64 bytes.
# We use whale index (tw:0, tw:1 ...) instead of full address.

WHALES = [
    ("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "eth", "Vitalik Buterin"),
    ("0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE", "eth", "Binance Hot Wallet"),
    ("0x28C6c06298d514Db089934071355E5743bf21d60", "eth", "Binance 14"),
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "sol", "Jump Trading"),
    ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "sol", "Raydium Program"),
]

@router.callback_query(F.data == "whales")
async def cb_whales(cb: CallbackQuery):
    lines   = []
    buttons = []
    for i, (address, chain, name) in enumerate(WHALES):
        emoji = CHAIN_EMOJI.get(chain, "🔗")
        lines.append(f"{emoji} <b>{name}</b>\n<code>{short_addr(address)}</code> · {chain.upper()}")
        # Use index — stays well under 64 bytes (e.g. "tw:0" = 4 bytes)
        buttons.append([InlineKeyboardButton(
            text=f"➕ Track {name}",
            callback_data=f"tw:{i}"
        )])

    buttons.append([InlineKeyboardButton(text="← Main Menu", callback_data="back_main")])

    await cb.message.edit_text(
        "🐋 <b>Whale Watchlist</b>\n\n"
        "Known on-chain whales. Tap to track:\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("tw:"))
async def cb_track_whale(cb: CallbackQuery):
    try:
        idx = int(cb.data.split(":")[1])
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

# ── Upgrade ─────────────────────────────────────────────

@router.callback_query(F.data == "upgrade")
async def cb_upgrade(cb: CallbackQuery):
    await cb.message.edit_text(
        "⬆️ <b>Upgrade Your Plan</b>\n\n"
        "🎯 <b>HUNTER — $19/mo</b>\n"
        "25 wallets · 60s checks · All 4 chains\n\n"
        "⚡ <b>APEX — $49/mo</b>\n"
        "Unlimited wallets · &lt;30s alerts · All chains\n\n"
        "💰 <b>Pay with crypto:</b> USDT · SOL · ETH\n"
        "No KYC · No banks · Cancel anytime\n\n"
        "Send payment and type /paid — your plan activates.",
        parse_mode="HTML",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── /paid ───────────────────────────────────────────────

@router.message(Command("paid"))
async def cmd_paid(msg: Message):
    await msg.answer(
        "✅ <b>Thanks! Payment received.</b>\n\n"
        "Your plan will be activated shortly.\n"
        "If not updated within 10 minutes — contact support.",
        parse_mode="HTML"
    )

# ── Admin: /upgrade_user ────────────────────────────────

@router.message(Command("upgrade_user"))
async def cmd_upgrade_user(msg: Message):
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    if msg.from_user.id != ADMIN_ID:
        return

    parts = (msg.text or "").split()
    if len(parts) != 3 or parts[2] not in ("free", "hunter", "apex"):
        await msg.answer("Usage: /upgrade_user USER_ID PLAN\nPlans: free / hunter / apex")
        return

    from datetime import datetime, timedelta
    uid        = int(parts[1])
    plan       = parts[2]
    paid_until = (datetime.utcnow() + timedelta(days=30)).isoformat()
    await upgrade_user(uid, plan, paid_until)
    await msg.answer(f"✅ User {uid} → {plan.upper()} until {paid_until[:10]}")

# ── Help ─────────────────────────────────────────────────

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "❓ <b>How Rifrush works</b>\n\n"
        "1️⃣ Add any wallet (EVM or Solana)\n"
        "2️⃣ I monitor it 24/7 on-chain\n"
        "3️⃣ Instant Telegram alert on every move\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/paid — Confirm payment\n\n"
        "<b>Chains:</b> ⟠ ETH · ◎ SOL · ⬡ BSC · 🔵 Base",
        parse_mode="HTML",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── Navigation ───────────────────────────────────────────

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

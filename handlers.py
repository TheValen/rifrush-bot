import os
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import (
    upsert_user, get_user, get_user_wallets,
    add_wallet, remove_wallet, get_wallet_count, PLAN_LIMITS
)
from utils import detect_chain, is_evm_address, short_addr, CHAIN_LABELS, CHAIN_EMOJI

router = Router()

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")

# ── FSM States ─────────────────────────────────────────

class AddWallet(StatesGroup):
    waiting_address = State()
    waiting_chain   = State()
    waiting_label   = State()

# ── Helpers ────────────────────────────────────────────

def main_menu(plan: str = "free") -> InlineKeyboardMarkup:
    plan_emoji = {"free": "🆓", "hunter": "🎯", "apex": "⚡"}.get(plan, "🆓")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 My Wallets",     callback_data="my_wallets"),
         InlineKeyboardButton(text="➕ Add Wallet",     callback_data="add_wallet")],
        [InlineKeyboardButton(text="🐋 Whale Watchlist", callback_data="whales")],
        [InlineKeyboardButton(text=f"{plan_emoji} Plan: {plan.upper()} → Upgrade", callback_data="upgrade")],
        [InlineKeyboardButton(text="❓ Help",            callback_data="help")],
    ])

def chain_keyboard(address: str) -> InlineKeyboardMarkup:
    """For EVM addresses — ask which chain"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⟠ Ethereum",  callback_data=f"chain:eth:{address}"),
         InlineKeyboardButton(text="⬡ BNB Chain", callback_data=f"chain:bsc:{address}")],
        [InlineKeyboardButton(text="🔵 Base",      callback_data=f"chain:base:{address}")],
        [InlineKeyboardButton(text="❌ Cancel",    callback_data="cancel")],
    ])

def back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Back", callback_data="back_main")]
    ])

# ── /start ─────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await upsert_user(msg.from_user.id, msg.from_user.username or "")
    user = await get_user(msg.from_user.id)
    plan = user["plan"] if user else "free"

    await msg.answer(
        "🔔 <b>Welcome to Rifrush</b>\n\n"
        "I watch crypto wallets on ETH, SOL, BSC and Base — "
        "and alert you the moment funds move.\n\n"
        f"Your plan: <b>{plan.upper()}</b> · "
        f"Wallets: <b>{await get_wallet_count(msg.from_user.id)}/{PLAN_LIMITS[plan]}</b>\n\n"
        "What would you like to do?",
        parse_mode="HTML",
        reply_markup=main_menu(plan)
    )

# ── My Wallets ─────────────────────────────────────────

@router.callback_query(F.data == "my_wallets")
async def cb_my_wallets(cb: CallbackQuery):
    wallets = await get_user_wallets(cb.from_user.id)

    if not wallets:
        await cb.message.edit_text(
            "👁 <b>Your Wallets</b>\n\nNo wallets added yet.\nTap <b>Add Wallet</b> to start tracking.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet")],
                [InlineKeyboardButton(text="← Back",       callback_data="back_main")],
            ])
        )
        await cb.answer()
        return

    lines = []
    buttons = []
    for w in wallets:
        emoji = CHAIN_EMOJI.get(w["chain"], "🔗")
        label = f" · {w['label']}" if w["label"] else ""
        lines.append(f"{emoji} <code>{short_addr(w['address'])}</code>{label} <i>({w['chain'].upper()})</i>")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Remove {short_addr(w['address'])} ({w['chain'].upper()})",
            callback_data=f"remove:{w['address']}:{w['chain']}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Add Wallet", callback_data="add_wallet")])
    buttons.append([InlineKeyboardButton(text="← Back",       callback_data="back_main")])

    await cb.message.edit_text(
        "👁 <b>Your Wallets</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

# ── Remove Wallet ──────────────────────────────────────

@router.callback_query(F.data.startswith("remove:"))
async def cb_remove(cb: CallbackQuery):
    _, address, chain = cb.data.split(":", 2)
    await remove_wallet(cb.from_user.id, address, chain)
    await cb.answer("✅ Wallet removed")
    # Refresh list
    await cb_my_wallets(cb)

# ── Add Wallet ─────────────────────────────────────────

@router.callback_query(F.data == "add_wallet")
async def cb_add_wallet(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    plan = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)
    limit = PLAN_LIMITS[plan]

    if count >= limit:
        await cb.message.edit_text(
            f"⚠️ <b>Wallet limit reached</b>\n\n"
            f"Your <b>{plan.upper()}</b> plan allows <b>{limit} wallets</b>.\n"
            f"Upgrade to track more.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬆️ Upgrade Plan", callback_data="upgrade")],
                [InlineKeyboardButton(text="← Back",         callback_data="back_main")],
            ])
        )
        await cb.answer()
        return

    await state.set_state(AddWallet.waiting_address)
    await cb.message.edit_text(
        "➕ <b>Add Wallet</b>\n\n"
        "Send me the wallet address to track.\n\n"
        "Supported formats:\n"
        "· <code>0x...</code> — ETH / BSC / Base\n"
        "· Solana address (base58)\n\n"
        "Just paste the address:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]
        ])
    )
    await cb.answer()

@router.message(AddWallet.waiting_address)
async def process_address(msg: Message, state: FSMContext):
    address = msg.text.strip()
    chain = detect_chain(address)

    if not chain:
        await msg.answer(
            "❌ <b>Invalid address</b>\n\nPlease send a valid ETH/BSC/Base or Solana address.",
            parse_mode="HTML"
        )
        return

    if chain == "eth" and is_evm_address(address):
        # EVM — ask which chain
        await state.update_data(address=address)
        await state.set_state(AddWallet.waiting_chain)
        await msg.answer(
            f"🔗 Address: <code>{address}</code>\n\nWhich chain is this on?",
            parse_mode="HTML",
            reply_markup=chain_keyboard(address)
        )
    else:
        # Solana — no need to ask
        await state.update_data(address=address, chain="sol")
        await state.set_state(AddWallet.waiting_label)
        await msg.answer(
            f"◎ <b>Solana</b> address detected.\n\n"
            f"<code>{address}</code>\n\n"
            "Give it a label (optional), or send <b>skip</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Skip →", callback_data="skip_label")]
            ])
        )

@router.callback_query(F.data.startswith("chain:"))
async def cb_chain_select(cb: CallbackQuery, state: FSMContext):
    _, chain, address = cb.data.split(":", 2)
    await state.update_data(address=address, chain=chain)
    await state.set_state(AddWallet.waiting_label)
    await cb.message.edit_text(
        f"{CHAIN_EMOJI[chain]} <b>{CHAIN_LABELS[chain]}</b> selected.\n\n"
        f"<code>{address}</code>\n\n"
        "Give it a label (e.g. <i>Vitalik</i>), or tap Skip:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Skip →", callback_data="skip_label")]
        ])
    )
    await cb.answer()

@router.callback_query(F.data == "skip_label")
async def cb_skip_label(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _save_wallet(cb.message, cb.from_user.id, data["address"], data["chain"], "", state)
    await cb.answer()

@router.message(AddWallet.waiting_label)
async def process_label(msg: Message, state: FSMContext):
    label = "" if msg.text.strip().lower() == "skip" else msg.text.strip()[:32]
    data = await state.get_data()
    await _save_wallet(msg, msg.from_user.id, data["address"], data["chain"], label, state)

async def _save_wallet(msg_or_cb, user_id: int, address: str, chain: str, label: str, state: FSMContext):
    added = await add_wallet(user_id, address, chain, label)
    await state.clear()

    if added:
        emoji = CHAIN_EMOJI.get(chain, "🔗")
        label_str = f" · <i>{label}</i>" if label else ""
        text = (
            f"✅ <b>Wallet added!</b>\n\n"
            f"{emoji} <code>{short_addr(address)}</code>{label_str}\n"
            f"Chain: <b>{CHAIN_LABELS[chain]}</b>\n\n"
            f"I'll alert you instantly when this wallet moves funds."
        )
    else:
        text = "⚠️ This wallet is already in your list."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another", callback_data="add_wallet")],
        [InlineKeyboardButton(text="← Main Menu",   callback_data="back_main")],
    ])

    if hasattr(msg_or_cb, "edit_text"):
        await msg_or_cb.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb)

# ── Whale Watchlist ────────────────────────────────────

WHALES = [
    ("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA9604", "eth",  "Vitalik Buterin"),
    ("0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE", "eth", "Binance Hot Wallet"),
    ("0x28C6c06298d514Db089934071355E5743bf21d60", "eth", "Binance 14"),
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "sol", "Jump Trading"),
    ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "sol", "Raydium"),
]

@router.callback_query(F.data == "whales")
async def cb_whales(cb: CallbackQuery):
    lines = []
    buttons = []
    for address, chain, name in WHALES:
        emoji = CHAIN_EMOJI.get(chain, "🔗")
        lines.append(f"{emoji} <b>{name}</b>\n   <code>{short_addr(address)}</code> · {chain.upper()}")
        buttons.append([InlineKeyboardButton(
            text=f"➕ Track {name}",
            callback_data=f"track_whale:{address}:{chain}:{name}"
        )])

    buttons.append([InlineKeyboardButton(text="← Back", callback_data="back_main")])

    await cb.message.edit_text(
        "🐋 <b>Whale Watchlist</b>\n\n"
        "Known on-chain whales and insiders.\nTap to add any to your tracker:\n\n"
        + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("track_whale:"))
async def cb_track_whale(cb: CallbackQuery):
    _, address, chain, name = cb.data.split(":", 3)
    user = await get_user(cb.from_user.id)
    plan = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)

    if count >= PLAN_LIMITS[plan]:
        await cb.answer("⚠️ Wallet limit reached. Upgrade your plan.", show_alert=True)
        return

    added = await add_wallet(cb.from_user.id, address, chain, name)
    if added:
        await cb.answer(f"✅ Now tracking {name}", show_alert=True)
    else:
        await cb.answer("Already in your list.", show_alert=True)

# ── Upgrade ────────────────────────────────────────────

@router.callback_query(F.data == "upgrade")
async def cb_upgrade(cb: CallbackQuery):
    text = (
        "⬆️ <b>Upgrade Your Plan</b>\n\n"
        "🎯 <b>HUNTER — $19/mo</b>\n"
        "· 25 wallets · 60s checks · All 4 chains\n\n"
        "⚡ <b>APEX — $49/mo</b>\n"
        "· Unlimited wallets · &lt;30s alerts · Copy signals\n\n"
        "💰 <b>Pay with crypto:</b> USDT · SOL · ETH\n"
        "No KYC · No banks · Cancel anytime\n\n"
        "To upgrade — send payment to:\n"
        "<code>YOUR_USDT_WALLET_ADDRESS_HERE</code>\n\n"
        "Then type /paid and your plan activates."
    )
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Back", callback_data="back_main")]
        ])
    )
    await cb.answer()

@router.message(Command("paid"))
async def cmd_paid(msg: Message):
    await msg.answer(
        "✅ <b>Payment received — thank you!</b>\n\n"
        "Your plan is being activated. "
        "If it's not updated within 10 minutes, contact support.",
        parse_mode="HTML"
    )
    # TODO: integrate CryptoBot webhook to auto-activate
    # For now — manual activation via /upgrade_user command below

@router.message(Command("upgrade_user"))
async def cmd_upgrade_user(msg: Message):
    """Admin command: /upgrade_user 123456789 hunter"""
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    if msg.from_user.id != ADMIN_ID:
        return

    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("Usage: /upgrade_user USER_ID PLAN")
        return

    _, user_id, plan = parts
    if plan not in ("hunter", "apex", "free"):
        await msg.answer("Plan must be: free / hunter / apex")
        return

    from database import upgrade_user
    from datetime import datetime, timedelta
    paid_until = (datetime.utcnow() + timedelta(days=30)).isoformat()
    await upgrade_user(int(user_id), plan, paid_until)
    await msg.answer(f"✅ User {user_id} upgraded to {plan.upper()}")

# ── Help ───────────────────────────────────────────────

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "❓ <b>How Rifrush works</b>\n\n"
        "1️⃣ Add any wallet address\n"
        "2️⃣ I monitor it 24/7 on-chain\n"
        "3️⃣ You get an instant Telegram alert on every move\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/paid — Confirm payment\n\n"
        "<b>Supported chains:</b>\n"
        "⟠ Ethereum · ◎ Solana · ⬡ BNB Chain · 🔵 Base\n\n"
        "<b>Questions?</b> Just message us here.",
        parse_mode="HTML",
        reply_markup=back_btn()
    )
    await cb.answer()

# ── Navigation ─────────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(cb.from_user.id)
    plan = user["plan"] if user else "free"
    count = await get_wallet_count(cb.from_user.id)

    await cb.message.edit_text(
        f"🔔 <b>Rifrush</b> — On-Chain Wallet Tracker\n\n"
        f"Plan: <b>{plan.upper()}</b> · "
        f"Wallets: <b>{count}/{PLAN_LIMITS[plan]}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(plan)
    )
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_back_main(cb, state)

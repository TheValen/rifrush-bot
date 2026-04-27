import re

def detect_chain(address: str) -> str | None:
    """
    Detect blockchain from address format.
    Returns: 'eth' | 'sol' | 'bsc' | 'base' | None
    """
    address = address.strip()

    # EVM address (ETH, BSC, Base all use same format)
    if re.match(r'^0x[0-9a-fA-F]{40}$', address):
        return "eth"  # user will confirm chain via button

    # Solana address (base58, 32-44 chars)
    if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
        return "sol"

    return None

def is_evm_address(address: str) -> bool:
    return bool(re.match(r'^0x[0-9a-fA-F]{40}$', address.strip()))

def is_sol_address(address: str) -> bool:
    return bool(re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address.strip()))

def short_addr(address: str) -> str:
    """0x1234…abcd"""
    if len(address) > 12:
        return f"{address[:6]}…{address[-4:]}"
    return address

CHAIN_LABELS = {
    "eth":  "Ethereum",
    "bsc":  "BNB Chain",
    "base": "Base",
    "sol":  "Solana",
}

CHAIN_EMOJI = {
    "eth":  "⟠",
    "bsc":  "⬡",
    "base": "🔵",
    "sol":  "◎",
}

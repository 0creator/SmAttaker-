"""
SmAttaker — Wallet Address Safety Helper
Single source of truth for "which crypto addresses are safe to show a
customer right now" — used by both the Telegram bot's subscription
flow and the web dashboard's payment page, so the critical safety
checks below only have to be written and tested once.

⚠️ CRITICAL: each network has its OWN address, never shared. A prior
version of this code showed one address labeled "TRC20/ERC20" — those
are different, incompatible address formats on different blockchains;
one address can never validly serve both. That mistake could destroy a
customer's payment permanently. Every address is now validated against
its network's actual format before ever being shown to anyone.
"""
import logging
import re

logger = logging.getLogger("smattaker.wallets")

# Very deliberately conservative regexes — reject anything that
# doesn't clearly match, rather than trying to be clever. A false
# negative (hiding a valid address, forcing the admin to double check)
# costs a minor inconvenience. A false positive (showing an invalid
# address as valid) risks a customer's money.
_PATTERNS = {
    "trc20": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),       # TRON base58, starts with T
    "erc20": re.compile(r"^0x[0-9a-fA-F]{40}$"),                 # Ethereum hex address
    "bep20": re.compile(r"^0x[0-9a-fA-F]{40}$"),                 # BNB Smart Chain — same format as ERC20 (both EVM)
    "btc": re.compile(r"^(1[1-9A-HJ-NP-Za-km-z]{25,34}|3[1-9A-HJ-NP-Za-km-z]{25,34}|bc1[0-9a-z]{25,62})$"),
}

_NETWORK_LABELS = {
    "trc20": "USDT (TRC20 — TRON network)",
    "erc20": "USDT (ERC20 — Ethereum network)",
    "bep20": "USDT (BEP20 — BNB Smart Chain)",
    "btc": "BTC (Bitcoin network)",
}


def _validate(network: str, address: str) -> str:
    """Returns the address if it matches its network's format, else ""."""
    if not address:
        return ""
    pattern = _PATTERNS[network]
    if not pattern.match(address.strip()):
        logger.error(
            f"{_NETWORK_LABELS[network]} address is configured but doesn't match "
            f"the expected format for that network — hiding it from users to "
            f"prevent a payment being sent to an invalid/wrong-network address. "
            f"Check the {network.upper()} address in your environment variables."
        )
        return ""
    return address.strip()


def get_safe_wallet_addresses() -> dict:
    """
    Returns a dict of {network: address} for every network that is both
    configured AND passes its format check. Networks that are empty or
    fail validation are simply absent from the result — callers should
    only ever display what's present here, nothing else.
    """
    from backend.config import settings

    result = {
        "trc20": _validate("trc20", settings.USDT_TRC20_ADDRESS),
        "erc20": _validate("erc20", settings.USDT_ERC20_ADDRESS),
        "bep20": _validate("bep20", settings.USDT_BEP20_ADDRESS),
        "btc": _validate("btc", settings.BTC_WALLET_ADDRESS),
    }
    return {k: v for k, v in result.items() if v}


def get_network_label(network: str) -> str:
    return _NETWORK_LABELS.get(network, network.upper())

from __future__ import annotations

from dataclasses import dataclass

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .settings import Settings


@dataclass
class HLClient:
    info: Info
    exchange: Exchange
    account_address: str
    base_url: str


def build_client(settings: Settings, *, skip_ws: bool = True) -> HLClient:
    base_url = (
        constants.TESTNET_API_URL
        if settings.config.network == "testnet"
        else constants.MAINNET_API_URL
    )

    secrets = settings.secrets
    if not secrets.hl_agent_private_key or not secrets.hl_account_address:
        raise RuntimeError(
            "Missing HL_AGENT_PRIVATE_KEY or HL_ACCOUNT_ADDRESS in .env. "
            "Generate an agent wallet at the Hyperliquid app and copy both values."
        )

    wallet = Account.from_key(secrets.hl_agent_private_key)
    info = Info(base_url, skip_ws=skip_ws)
    exchange = Exchange(
        wallet,
        base_url=base_url,
        account_address=secrets.hl_account_address,
    )

    return HLClient(
        info=info,
        exchange=exchange,
        account_address=secrets.hl_account_address,
        base_url=base_url,
    )


def initialize_position_leverage(
    client: HLClient, assets: list[str], leverage: int, *, is_cross: bool = True
) -> dict[str, str]:
    """Set per-asset leverage on the exchange for each tradable asset. Idempotent
    (Hyperliquid no-ops if the leverage is already at the requested value).
    Returns a per-asset status dict for logging."""
    out: dict[str, str] = {}
    for asset in assets:
        try:
            client.exchange.update_leverage(
                leverage=int(leverage),
                name=asset,
                is_cross=is_cross,
            )
            out[asset] = "ok"
        except Exception as e:
            out[asset] = f"error: {e}"
    return out

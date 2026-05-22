from __future__ import annotations

from dataclasses import dataclass

from .account import AccountState, Position
from .settings import RiskConfig


@dataclass
class RiskCheck:
    ok: bool
    reason: str = ""


def _find_position(account: AccountState, asset: str) -> Position | None:
    for p in account.positions:
        if p.asset == asset and p.size != 0:
            return p
    return None


def _drawdown_kill_switch_active(
    account: AccountState,
    starting_equity_usd: float,
    risk: RiskConfig,
) -> bool:
    if starting_equity_usd <= 0:
        return False
    drawdown = (account.equity_usd - starting_equity_usd) / starting_equity_usd
    return drawdown <= risk.daily_drawdown_kill_switch_pct


def check_open_or_increase(
    *,
    account: AccountState,
    starting_equity_usd: float,
    asset: str,
    side: str,
    usd_size: float,
    allowed_assets: list[str],
    risk: RiskConfig,
) -> RiskCheck:
    if asset not in allowed_assets:
        return RiskCheck(False, f"asset {asset!r} not in allowed list {allowed_assets}")
    if side not in ("buy", "sell"):
        return RiskCheck(False, f"side must be 'buy' or 'sell', got {side!r}")
    if usd_size < risk.min_order_usd:
        return RiskCheck(
            False,
            f"order ${usd_size:.2f} below min_order_usd ${risk.min_order_usd:.2f}",
        )
    if account.equity_usd <= 0:
        return RiskCheck(False, "account equity is zero or negative")
    if _drawdown_kill_switch_active(account, starting_equity_usd, risk):
        return RiskCheck(
            False,
            f"daily drawdown kill switch active "
            f"(equity {account.equity_usd:.2f} vs start {starting_equity_usd:.2f}); "
            f"only close_position allowed",
        )

    pos = _find_position(account, asset)
    current_notional = pos.position_value_usd if pos else 0.0
    current_signed = (pos.size * pos.entry_px) if pos else 0.0
    delta = usd_size if side == "buy" else -usd_size
    new_signed = current_signed + delta
    new_notional = abs(new_signed)

    # Per-asset cap: only enforced on increases, not on flips that net-reduce.
    if new_notional > current_notional:
        cap = account.equity_usd * risk.max_position_pct_per_asset
        if new_notional > cap:
            return RiskCheck(
                False,
                f"asset notional ${new_notional:.2f} exceeds per-asset cap ${cap:.2f} "
                f"({risk.max_position_pct_per_asset:.0%} of equity)",
            )

    other_notional = sum(
        p.position_value_usd for p in account.positions if p.asset != asset
    )
    new_total = other_notional + new_notional
    total_cap = account.equity_usd * risk.max_total_notional_pct
    if new_total > total_cap:
        return RiskCheck(
            False,
            f"total notional ${new_total:.2f} exceeds cap ${total_cap:.2f} "
            f"({risk.max_total_notional_pct:.0%} of equity)",
        )

    implied_leverage = new_total / account.equity_usd
    if implied_leverage > risk.max_leverage:
        return RiskCheck(
            False,
            f"implied leverage {implied_leverage:.2f}x exceeds cap {risk.max_leverage}x",
        )

    return RiskCheck(True)


def check_close(
    *,
    account: AccountState,
    asset: str,
) -> RiskCheck:
    pos = _find_position(account, asset)
    if not pos:
        return RiskCheck(False, f"no open position in {asset} to close")
    return RiskCheck(True)


def check_cancel(
    *,
    account: AccountState,
    asset: str,
) -> RiskCheck:
    has_orders = any(o.asset == asset for o in account.open_orders)
    if not has_orders:
        return RiskCheck(False, f"no open orders for {asset} to cancel")
    return RiskCheck(True)

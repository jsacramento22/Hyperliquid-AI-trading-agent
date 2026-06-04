from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import risk
from .account import AccountState
from .hl_client import HLClient
from .market_data import MarketSnapshot
from .settings import RiskConfig
from .storage import Storage


@dataclass
class ActionResult:
    tool: str
    args: dict
    accepted: bool
    reason: str = ""
    response: Any = None  # raw exchange response on success


PERP_MAX_DECIMALS = 6  # Hyperliquid: max price decimals = MAX_DECIMALS - sz_decimals
PRICE_SIG_FIGS = 5     # Hyperliquid: prices have at most 5 significant figures

# Correlation block — assets in this set are treated as one risk unit for
# the per-cycle stack check. BTC and ETH run 80-90% correlated intraday
# on this venue; opening same-direction entries on both in one cycle has
# been a recurring losing pattern. The prompt also discourages it as a
# soft rule; this is the hard backstop. If/when we trade more assets,
# this becomes a correlation matrix instead of a flat set.
CORRELATED_ASSETS = {"BTC", "ETH"}


def _coin_size_from_usd(usd: float, price: float, sz_decimals: int) -> float:
    if price <= 0:
        raise ValueError("price must be positive to compute coin size")
    return _round_size(usd / price, sz_decimals)


def _round_size(sz: float, sz_decimals: int) -> float:
    return round(sz, sz_decimals)


def _round_price(px: float, sz_decimals: int) -> float:
    """Round to Hyperliquid wire format: <=5 sig figs AND <=(6-sz_decimals) decimals.
    Integer prices are always allowed, regardless of sig figs."""
    if px <= 0:
        return px
    max_decimals = max(0, PERP_MAX_DECIMALS - sz_decimals)
    # First, round to allowed decimal places.
    rounded_dec = round(px, max_decimals)
    # Then, enforce 5-sig-fig cap (skip for integer prices, which are always allowed).
    if rounded_dec == int(rounded_dec):
        return float(int(rounded_dec))
    from math import floor, log10
    exponent = int(floor(log10(abs(rounded_dec))))
    sig_decimals = PRICE_SIG_FIGS - 1 - exponent
    final_decimals = min(max_decimals, max(0, sig_decimals))
    out = round(rounded_dec, final_decimals)
    if out == int(out):
        return float(int(out))
    return out


class Executor:
    def __init__(
        self,
        client: HLClient,
        storage: Storage,
        risk_config: RiskConfig,
        allowed_assets: list[str],
        *,
        cycle_id: str,
        dry_run: bool = False,
    ):
        self.client = client
        self.storage = storage
        self.risk_config = risk_config
        self.allowed_assets = allowed_assets
        self.cycle_id = cycle_id
        self.dry_run = dry_run

    def _check_correlation(
        self, asset: str, side: str, reduce_only: bool
    ) -> str | None:
        """Reject if another correlated asset already had a same-direction
        opening fill in this cycle. Returns the rejection reason or None.

        Same-cycle stack is the only case blocked — cross-cycle entries
        are allowed (matches the prompt's "wait one cycle for the other
        asset's price action to confirm independently" rule). Reduce-only
        orders bypass the check since they're closing/reducing, not
        adding correlated risk.

        Detection uses the fills table (not account.positions) because the
        first leg of a same-cycle stack hasn't reached the account
        snapshot yet — the snapshot is taken at cycle start, before any
        of this cycle's orders fired."""
        if reduce_only:
            return None
        if asset not in CORRELATED_ASSETS:
            return None
        opens = self.storage.opens_in_cycle_by_side(self.cycle_id)
        for other_asset, other_side in opens.items():
            if other_asset == asset or other_asset not in CORRELATED_ASSETS:
                continue
            if other_side != side:
                continue
            return (
                f"correlated same-direction stack rejected: "
                f"{other_asset} {other_side} was already opened in this "
                f"cycle ({self.cycle_id}). Prompt rule: wait one cycle "
                f"for {other_asset}'s price action to confirm "
                f"independently before adding {asset}. If both setups "
                f"are independently compelling next cycle, this block "
                f"will lift."
            )
        return None

    def apply(
        self,
        tool: str,
        args: dict,
        *,
        account: AccountState,
        snapshot: MarketSnapshot,
        starting_equity_usd: float,
    ) -> ActionResult:
        if tool == "hold":
            return ActionResult(tool=tool, args=args, accepted=True, reason="no-op")

        if tool == "place_market_order":
            return self._market_order(args, account, snapshot, starting_equity_usd)
        if tool == "place_limit_order":
            return self._limit_order(args, account, snapshot, starting_equity_usd)
        if tool == "cancel_all":
            return self._cancel_all(args, account)
        if tool == "close_position":
            return self._close_position(args, account)

        return ActionResult(tool=tool, args=args, accepted=False, reason=f"unknown tool {tool!r}")

    def _market_order(
        self,
        args: dict,
        account: AccountState,
        snapshot: MarketSnapshot,
        starting_equity_usd: float,
    ) -> ActionResult:
        asset = args["asset"].upper()
        side = args["side"]
        usd_size = float(args["usd_size"])
        reduce_only = bool(args.get("reduce_only", False))

        corr_reason = self._check_correlation(asset, side, reduce_only)
        if corr_reason:
            return ActionResult(
                "place_market_order", args, accepted=False, reason=corr_reason
            )

        check = risk.check_open_or_increase(
            account=account,
            starting_equity_usd=starting_equity_usd,
            asset=asset,
            side=side,
            usd_size=usd_size,
            allowed_assets=self.allowed_assets,
            risk=self.risk_config,
        )
        if not check.ok:
            return ActionResult("place_market_order", args, accepted=False, reason=check.reason)

        if asset not in snapshot.assets:
            return ActionResult(
                "place_market_order", args, accepted=False, reason=f"no market data for {asset}"
            )
        asset_info = snapshot.assets[asset]
        price = asset_info.mid
        sz = _coin_size_from_usd(usd_size, price, asset_info.sz_decimals)
        if sz <= 0:
            return ActionResult(
                "place_market_order", args, accepted=False,
                reason=f"computed size rounds to 0 (usd={usd_size}, sz_decimals={asset_info.sz_decimals})",
            )

        if self.dry_run:
            return ActionResult(
                "place_market_order",
                args,
                accepted=True,
                reason=f"DRY RUN: would market_open {asset} {side} sz={sz:g} @~{price:g}",
            )

        resp = self.client.exchange.market_open(
            name=asset,
            is_buy=(side == "buy"),
            sz=sz,
        )
        self.storage.log_fill(
            cycle_id=self.cycle_id,
            asset=asset,
            side=side,
            requested_usd=usd_size,
            raw_response=resp,
        )
        return ActionResult("place_market_order", args, accepted=True, response=resp)

    def _limit_order(
        self,
        args: dict,
        account: AccountState,
        snapshot: MarketSnapshot,
        starting_equity_usd: float,
    ) -> ActionResult:
        asset = args["asset"].upper()
        side = args["side"]
        usd_size = float(args["usd_size"])
        limit_px = float(args["limit_px"])
        tif = args.get("tif", "Gtc")
        reduce_only = bool(args.get("reduce_only", False))

        corr_reason = self._check_correlation(asset, side, reduce_only)
        if corr_reason:
            return ActionResult(
                "place_limit_order", args, accepted=False, reason=corr_reason
            )

        check = risk.check_open_or_increase(
            account=account,
            starting_equity_usd=starting_equity_usd,
            asset=asset,
            side=side,
            usd_size=usd_size,
            allowed_assets=self.allowed_assets,
            risk=self.risk_config,
        )
        if not check.ok:
            return ActionResult("place_limit_order", args, accepted=False, reason=check.reason)

        if asset not in snapshot.assets:
            return ActionResult(
                "place_limit_order", args, accepted=False, reason=f"no market data for {asset}"
            )
        asset_info = snapshot.assets[asset]
        limit_px = _round_price(limit_px, asset_info.sz_decimals)
        sz = _coin_size_from_usd(usd_size, limit_px, asset_info.sz_decimals)
        if sz <= 0:
            return ActionResult(
                "place_limit_order", args, accepted=False,
                reason=f"computed size rounds to 0 (usd={usd_size}, sz_decimals={asset_info.sz_decimals})",
            )

        if self.dry_run:
            return ActionResult(
                "place_limit_order",
                args,
                accepted=True,
                reason=f"DRY RUN: would order {asset} {side} sz={sz:g} @ {limit_px:g} tif={tif}",
            )

        resp = self.client.exchange.order(
            name=asset,
            is_buy=(side == "buy"),
            sz=sz,
            limit_px=limit_px,
            order_type={"limit": {"tif": tif}},
            reduce_only=reduce_only,
        )
        self.storage.log_fill(
            cycle_id=self.cycle_id,
            asset=asset,
            side=side,
            requested_usd=usd_size,
            raw_response=resp,
        )
        return ActionResult("place_limit_order", args, accepted=True, response=resp)

    def _cancel_all(self, args: dict, account: AccountState) -> ActionResult:
        asset = args["asset"].upper()
        check = risk.check_cancel(account=account, asset=asset)
        if not check.ok:
            return ActionResult("cancel_all", args, accepted=False, reason=check.reason)

        oids = [o.oid for o in account.open_orders if o.asset == asset]
        if self.dry_run:
            return ActionResult(
                "cancel_all", args, accepted=True, reason=f"DRY RUN: would cancel oids={oids}"
            )

        responses = [self.client.exchange.cancel(asset, oid) for oid in oids]
        return ActionResult("cancel_all", args, accepted=True, response=responses)

    def _close_position(self, args: dict, account: AccountState) -> ActionResult:
        asset = args["asset"].upper()
        check = risk.check_close(account=account, asset=asset)
        if not check.ok:
            return ActionResult("close_position", args, accepted=False, reason=check.reason)

        if self.dry_run:
            return ActionResult(
                "close_position", args, accepted=True, reason=f"DRY RUN: would market_close {asset}"
            )

        resp = self.client.exchange.market_close(coin=asset)
        self.storage.log_fill(
            cycle_id=self.cycle_id,
            asset=asset,
            side="close",
            requested_usd=None,
            raw_response=resp,
        )
        return ActionResult("close_position", args, accepted=True, response=resp)
